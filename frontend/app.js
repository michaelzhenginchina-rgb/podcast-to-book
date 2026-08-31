const invoke = window.__TAURI__.core.invoke;

const urlInput = document.querySelector("#url");
const titleInput = document.querySelector("#title");
const formatInput = document.querySelector("#format");
const originalLanguageInput = document.querySelector("#original-language");
const bookLanguageInput = document.querySelector("#book-language");
const intervalInput = document.querySelector("#interval");
const autoChaptersInput = document.querySelector("#auto-chapters");
const aiCleanInput = document.querySelector("#ai-clean");
const cleanModeInput = document.querySelector("#clean-mode");
const generateButton = document.querySelector("#generate");
const documentPathInput = document.querySelector("#document-path");
const chooseDocumentButton = document.querySelector("#choose-document");
const documentTitleInput = document.querySelector("#document-title");
const documentTargetInput = document.querySelector("#document-target");
const documentModeInput = document.querySelector("#document-mode");
const translateDocumentButton = document.querySelector("#translate-document");
const openButton = document.querySelector("#open-output");
const statusBox = document.querySelector("#status");
const summaryBox = document.querySelector("#summary");
const progressPanel = document.querySelector("#progress-panel");
const progressStage = document.querySelector("#progress-stage");
const progressPercent = document.querySelector("#progress-percent");
const progressBar = document.querySelector("#progress-bar");
const progressDetail = document.querySelector("#progress-detail");
const filesBox = document.querySelector("#files");
const logBox = document.querySelector("#log");

let latestOutputDir = "";
let progressTimer = null;
let progressStartedAt = 0;
let progressValue = 0;

const progressStages = [
  { at: 4, label: "Reading YouTube link", detail: "Checking video ID and title." },
  { at: 12, label: "Fetching transcript", detail: "Looking for the best available captions." },
  { at: 24, label: "Detecting chapters", detail: "Using YouTube chapters when available." },
  { at: 38, label: "Building sections", detail: "Organizing the transcript into readable chapters." },
  { at: 54, label: "AI cleaning", detail: "Cleaning transcript with the selected mode." },
  { at: 72, label: "Translating", detail: "Preparing the requested book language if needed." },
  { at: 86, label: "Creating ebook", detail: "Writing EPUB/PDF and transcript files." },
  { at: 94, label: "Finalizing", detail: "Collecting output files." },
];

function resolveLanguageSettings() {
  const original = originalLanguageInput.value;
  const book = bookLanguageInput.value;

  if (original === "english") {
    return {
      language: "English (Original)",
      translate: book === "chinese" ? "Chinese (Simplified)" : "none",
    };
  }

  if (original === "chinese") {
    return {
      language: "中文简体 (Chinese Simplified)",
      translate: book === "english" ? "English" : "none",
    };
  }

  if (book === "chinese") {
    return {
      language: "中文简体 (Chinese Simplified)",
      translate: "none",
    };
  }

  if (book === "english") {
    return {
      language: "English (Original)",
      translate: "none",
    };
  }

  return {
    language: "English (Original)",
    translate: "none",
  };
}

function setStatus(message, state) {
  statusBox.textContent = message;
  statusBox.className = `status ${state}`;
}

function setRunning(isRunning) {
  generateButton.disabled = isRunning;
  translateDocumentButton.disabled = isRunning;
  chooseDocumentButton.disabled = isRunning;
  generateButton.textContent = isRunning ? "Generating..." : "Generate Ebook";
  translateDocumentButton.textContent = isRunning ? "Translating..." : "Translate Document";
}

function formatElapsed(seconds) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes === 0) return `${rest}s`;
  return `${minutes}m ${String(rest).padStart(2, "0")}s`;
}

function updateProgress(value, detailOverride) {
  progressValue = Math.max(0, Math.min(100, Math.round(value)));
  const stage =
    [...progressStages].reverse().find((item) => progressValue >= item.at) ??
    progressStages[0];
  const elapsed = Math.floor((Date.now() - progressStartedAt) / 1000);

  progressStage.textContent = stage.label;
  progressPercent.textContent = `${progressValue}%`;
  progressBar.style.width = `${progressValue}%`;
  progressDetail.textContent =
    detailOverride ?? `${stage.detail} Elapsed: ${formatElapsed(elapsed)}.`;
}

function startProgress() {
  progressPanel.classList.remove("hidden");
  progressStartedAt = Date.now();
  progressValue = 3;
  updateProgress(progressValue, "Starting local workflow.");

  progressTimer = window.setInterval(() => {
    const elapsed = Math.floor((Date.now() - progressStartedAt) / 1000);
    const ceiling = elapsed < 20 ? 58 : elapsed < 60 ? 78 : elapsed < 120 ? 90 : 96;
    const increment = elapsed < 15 ? 2.2 : elapsed < 60 ? 1.1 : 0.35;
    const next = Math.min(ceiling, progressValue + increment);
    updateProgress(next);
  }, 1000);
}

function finishProgress(success) {
  if (progressTimer) {
    window.clearInterval(progressTimer);
    progressTimer = null;
  }

  if (success) {
    updateProgress(100, "Done. Output files are ready.");
  } else {
    progressStage.textContent = "Stopped";
    progressDetail.textContent = "The run failed. Check the process log below.";
  }
}

function waitForPaint() {
  return new Promise((resolve) => {
    requestAnimationFrame(() => {
      window.setTimeout(resolve, 0);
    });
  });
}

function renderFiles(files) {
  if (!files || files.length === 0) {
    filesBox.className = "files empty";
    filesBox.textContent = "No output files were found.";
    return;
  }

  filesBox.className = "files";
  filesBox.replaceChildren(
    ...files.map((file) => {
      const row = document.createElement("div");
      row.className = "file-row";

      const kind = document.createElement("div");
      kind.className = "file-kind";
      kind.textContent = file.kind;

      const name = document.createElement("div");
      name.className = "file-name";
      name.title = file.path;
      name.textContent = file.name;

      row.append(kind, name);
      return row;
    }),
  );
}

function summaryRow(key, value) {
  const row = document.createElement("div");
  row.className = "summary-row";

  const keyNode = document.createElement("div");
  keyNode.className = "summary-key";
  keyNode.textContent = key;

  const valueNode = document.createElement("div");
  valueNode.className = "summary-value";
  valueNode.textContent = value;

  row.append(keyNode, valueNode);
  return row;
}

function cleanModeLabel(mode) {
  const labels = {
    faithful: "faithful",
    fast: "fast",
    deep: "deep",
  };
  return labels[mode] ?? mode ?? "faithful";
}

function renderSummary(metadata) {
  if (!metadata) {
    summaryBox.className = "summary empty";
    summaryBox.textContent = "No metadata returned.";
    return;
  }

  if (metadata.kind === "document_translation" || metadata.kind === "pdf_translation") {
    const rows = [
      summaryRow("Title", metadata.title),
      summaryRow("Source", metadata.source_format ?? "PDF"),
      summaryRow("Workflow", `${metadata.target_label} / ${metadata.mode}`),
      summaryRow("Chunks", `${metadata.chunks_count} chunks / ${metadata.characters.toLocaleString()} chars`),
    ];

    if (metadata.usage_stats && metadata.usage_stats.total_tokens > 0) {
      rows.push(
        summaryRow(
          "Token cost",
          `${metadata.usage_stats.total_tokens.toLocaleString()} tokens, $${metadata.usage_stats.cost_usd}`,
        ),
      );
    }

    if (metadata.timing && typeof metadata.timing.total_seconds === "number") {
      const seconds = metadata.timing.total_seconds;
      rows.push(summaryRow("Timing", seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`));
    }

    summaryBox.className = "summary";
    summaryBox.replaceChildren(...rows);
    return;
  }

  const mode =
    metadata.section_mode === "youtube_chapters"
      ? `YouTube chapters (${metadata.chapters_count})`
      : `${metadata.sections_count} time sections`;

  const ai = metadata.ai_clean_used
    ? `GPT-4o-mini ${cleanModeLabel(metadata.clean_mode)} cleaning used`
    : metadata.ai_clean_requested
      ? "Requested, but key missing or unavailable"
      : "Off";

  const rows = [
    summaryRow("Title", metadata.title),
    summaryRow("Organization", mode),
    summaryRow("AI cleaning", ai),
  ];

  const secondsText = (seconds) => {
    if (typeof seconds !== "number") return null;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  };

  if (metadata.transcript_source_language) {
    rows.push(summaryRow("Transcript source", metadata.transcript_source_language));
  }

  if (metadata.cover_source) {
    const coverSource =
      metadata.cover_source === "youtube_thumbnail"
        ? "YouTube thumbnail"
        : metadata.cover_source === "generated_from_title_and_topics"
          ? "Generated from title and transcript topics"
          : metadata.cover_source;
    rows.push(summaryRow("Cover", coverSource));
  }

  if (metadata.llm_translated_to_chinese) {
    rows.push(
      summaryRow(
        "Chinese output",
        "YouTube Chinese captions were unavailable to the API, so OpenAI translated the transcript.",
      ),
    );
  }

  if (metadata.usage_stats && metadata.usage_stats.total_tokens > 0) {
    rows.push(
      summaryRow(
        "Token cost",
        `${metadata.usage_stats.total_tokens.toLocaleString()} tokens, $${metadata.usage_stats.cost_usd}`,
      ),
    );
  }

  if (metadata.timing) {
    const timing = metadata.timing.values ?? metadata.timing;
    const timingParts = [];
    const addTiming = (label, key) => {
      const value = secondsText(timing[key]);
      if (value) timingParts.push(`${label}: ${value}`);
    };
    addTiming("Metadata", "fetch_metadata_seconds");
    addTiming("Cover", "build_cover_seconds");
    addTiming("Generated cover", "build_generated_cover_seconds");
    addTiming("Transcript", "fetch_transcript_seconds");
    addTiming("Chapters", "detect_chapters_seconds");
    addTiming("AI clean", "ai_clean_total_seconds");
    addTiming("Book", "write_book_seconds");
    addTiming("Total", "total_seconds");
    if (timingParts.length > 0) {
      rows.push(summaryRow("Timing", timingParts.join(" / ")));
    }
  }

  if (metadata.chapter_titles && metadata.chapter_titles.length > 0) {
    rows.push(summaryRow("First chapters", metadata.chapter_titles.join(" / ")));
  }

  summaryBox.className = "summary";
  summaryBox.replaceChildren(...rows);
}

generateButton.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Paste a YouTube URL first.", "error");
    return;
  }

  setRunning(true);
  setStatus("Generating ebook. This can take a few minutes.", "running");
  startProgress();
  summaryBox.className = "summary empty";
  summaryBox.textContent =
    "Fetching title, transcript, chapters, and AI cleaning settings...";
  filesBox.className = "files empty";
  filesBox.textContent = "Working...";
  logBox.textContent = "Starting Podcast Ebook pipeline...";
  openButton.disabled = true;
  latestOutputDir = "";

  try {
    const languageSettings = resolveLanguageSettings();
    await waitForPaint();
    const result = await invoke("generate_podcast_ebook", {
      url,
      title: titleInput.value.trim(),
      outputFormat: formatInput.value,
      language: languageSettings.language,
      translate: languageSettings.translate,
      aiClean: aiCleanInput.checked,
      cleanMode: cleanModeInput.value,
      autoChapters: autoChaptersInput.checked,
      intervalMinutes: Number(intervalInput.value),
    });
    latestOutputDir = result.output_dir;
    finishProgress(true);
    setStatus("Done. Ebook package generated.", "success");
    renderSummary(result.metadata);
    renderFiles(result.files);
    logBox.textContent = result.log || "Finished.";
    openButton.disabled = false;
  } catch (error) {
    const message = String(error || "Unknown error");
    finishProgress(false);
    setStatus("Failed. Check the process log.", "error");
    filesBox.className = "files empty";
    filesBox.textContent = "Generation failed.";
    logBox.textContent = message;
  } finally {
    setRunning(false);
  }
});

chooseDocumentButton.addEventListener("click", async () => {
  try {
    const path = await invoke("choose_document_file");
    if (path) {
      documentPathInput.value = path;
      setStatus("Document selected.", "success");
    }
  } catch (error) {
    setStatus("Document selection cancelled.", "idle");
  }
});

translateDocumentButton.addEventListener("click", async () => {
  const filePath = documentPathInput.value.trim();
  if (!filePath) {
    setStatus("Choose a document file first.", "error");
    return;
  }

  setRunning(true);
  setStatus("Translating document. This can take a few minutes.", "running");
  startProgress();
  summaryBox.className = "summary empty";
  summaryBox.textContent = "Extracting document text, analyzing style, and preparing glossary...";
  filesBox.className = "files empty";
  filesBox.textContent = "Working...";
  logBox.textContent = "Starting document translation pipeline...";
  openButton.disabled = true;
  latestOutputDir = "";

  try {
    await waitForPaint();
    const result = await invoke("translate_document", {
      filePath,
      title: documentTitleInput.value.trim(),
      target: documentTargetInput.value,
      mode: documentModeInput.value,
    });
    latestOutputDir = result.output_dir;
    finishProgress(true);
    setStatus("Done. Document translation package generated.", "success");
    renderSummary(result.metadata);
    renderFiles(result.files);
    logBox.textContent = result.log || "Finished.";
    openButton.disabled = false;
  } catch (error) {
    const message = String(error || "Unknown error");
    finishProgress(false);
    setStatus("Failed. Check the process log.", "error");
    filesBox.className = "files empty";
    filesBox.textContent = "Document translation failed.";
    logBox.textContent = message;
  } finally {
    setRunning(false);
  }
});

openButton.addEventListener("click", async () => {
  if (!latestOutputDir) return;
  await invoke("open_output_location", { path: latestOutputDir });
});
