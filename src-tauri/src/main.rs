use chrono::Local;
use serde::Serialize;
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const RUNTIME_ENV: &str = "PODCAST_TO_BOOK_RUNTIME";
const REPO_ENV: &str = "PODCAST_TO_BOOK_REPO";
const OUTPUT_ENV: &str = "PODCAST_TO_BOOK_OUTPUT";

/// Root of this repository (or of the installed .app bundle's Resources).
///
/// Checked in order: the `PODCAST_TO_BOOK_REPO` override, the bundle's
/// `Contents/Resources` (packaged .app), the repo above `target/{debug,release}`
/// (`cargo tauri dev`), then the compile-time crate location.
fn repo_root() -> Result<PathBuf, String> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Ok(dir) = std::env::var(REPO_ENV) {
        candidates.push(PathBuf::from(dir));
    }

    if let Some(exe_dir) = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(PathBuf::from))
    {
        candidates.push(exe_dir.join("../Resources"));
        let mut up = exe_dir;
        for _ in 0..4 {
            up = match up.parent() {
                Some(parent) => parent.to_path_buf(),
                None => break,
            };
            candidates.push(up.clone());
        }
    }

    candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(".."));

    for candidate in candidates {
        if candidate.join("scripts/podcast_desktop_runner.py").exists() {
            return Ok(candidate);
        }
    }

    Err(format!(
        "Cannot find the repository files. Set {REPO_ENV} to your podcast-to-book checkout."
    ))
}

fn scripts_dir() -> Result<PathBuf, String> {
    Ok(repo_root()?.join("scripts"))
}

/// Where `.env` and `.venv` live: a real checkout, never the packaged bundle.
///
/// A bundled .app carries `scripts/` and `runtime/` in `Contents/Resources`, but
/// deliberately not the API key or a virtualenv — a key inside an .app travels
/// to anyone the app is copied to, and a venv is not relocatable. So the app
/// falls back to the checkout it was built from.
fn checkout_root() -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Ok(dir) = std::env::var(REPO_ENV) {
        candidates.push(PathBuf::from(dir));
    }
    if let Ok(repo) = repo_root() {
        candidates.push(repo);
    }
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(PathBuf::from(home).join("podcast-to-book"));
    }

    candidates
        .into_iter()
        .find(|dir| dir.join(".env").exists() || dir.join(".venv").exists())
}

/// Directory of the Python runtime. It ships in-repo under `runtime/`.
fn podcast_root() -> Result<PathBuf, String> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Ok(dir) = std::env::var(RUNTIME_ENV) {
        candidates.push(PathBuf::from(dir));
    }
    if let Ok(repo) = repo_root() {
        candidates.push(repo.join("runtime"));
    }

    for candidate in candidates {
        if candidate.join("main.py").exists() {
            return Ok(candidate);
        }
    }

    Err(format!(
        "Cannot find the Python runtime. It should be at <repo>/runtime; \
         re-clone the repository, or set {RUNTIME_ENV}=/path/to/runtime."
    ))
}

#[derive(Serialize)]
struct OutputFile {
    name: String,
    path: String,
    kind: String,
}

#[derive(Serialize)]
struct GenerateResult {
    output_dir: String,
    files: Vec<OutputFile>,
    log: String,
    metadata: Option<serde_json::Value>,
}

fn desktop_output_root() -> Result<PathBuf, String> {
    if let Ok(dir) = std::env::var(OUTPUT_ENV) {
        return Ok(PathBuf::from(dir));
    }
    let desktop = dirs::desktop_dir().ok_or_else(|| "Could not find Desktop folder".to_string())?;
    Ok(desktop.join("PodcastToBook"))
}

fn read_env_file(path: &Path) -> HashMap<String, String> {
    let mut values = HashMap::new();
    let Ok(content) = fs::read_to_string(path) else {
        return values;
    };

    for raw_line in content.lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let cleaned = value
            .trim()
            .trim_matches('"')
            .trim_matches('\'')
            .to_string();
        values.insert(key.trim().to_string(), cleaned);
    }

    values
}

/// The interpreter that has the dependencies: the one `setup.sh` created.
///
/// It builds `<repo>/.venv`, so that is checked first. `runtime/venv` is the
/// older layout, kept so an existing checkout keeps working. Falling through to
/// a bare `python3` almost always means a crash on the first import, so say so.
fn python_bin(podcast_root: &Path) -> PathBuf {
    if let Ok(explicit) = std::env::var("PODCAST_TO_BOOK_PYTHON") {
        return PathBuf::from(explicit);
    }

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(checkout) = checkout_root() {
        candidates.push(checkout.join(".venv").join("bin").join("python"));
    }
    candidates.push(podcast_root.join("venv").join("bin").join("python"));

    for candidate in candidates {
        if candidate.exists() {
            return candidate;
        }
    }

    eprintln!(
        "No virtualenv found - falling back to python3, which is missing this \
         project's dependencies. Run ./setup.sh in your checkout."
    );
    PathBuf::from("python3")
}

fn classify_file(path: &Path) -> String {
    match path.extension().and_then(|ext| ext.to_str()).unwrap_or_default() {
        "epub" => "EPUB",
        "pdf" => "PDF",
        "txt" => "Text",
        "json" => "Transcript JSON",
        "zip" => "Package",
        other if !other.is_empty() => other,
        _ => "File",
    }
    .to_string()
}

fn collect_files(output_dir: &Path) -> Result<Vec<OutputFile>, String> {
    let mut files = Vec::new();
    for entry in fs::read_dir(output_dir).map_err(|err| err.to_string())? {
        let entry = entry.map_err(|err| err.to_string())?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        files.push(OutputFile {
            name: name.to_string(),
            path: path.to_string_lossy().to_string(),
            kind: classify_file(&path),
        });
    }
    files.sort_by(|left, right| left.name.cmp(&right.name));
    Ok(files)
}

fn read_json_metadata(output_dir: &Path, file_name: &str) -> Option<serde_json::Value> {
    let result_path = output_dir.join(file_name);
    let content = fs::read_to_string(result_path).ok()?;
    serde_json::from_str(&content).ok()
}

fn read_metadata(output_dir: &Path) -> Option<serde_json::Value> {
    read_json_metadata(output_dir, "desktop_result.json")
}

fn generate_podcast_ebook_impl(
    url: String,
    title: String,
    output_format: String,
    language: String,
    translate: String,
    ai_clean: bool,
    clean_mode: String,
    auto_chapters: bool,
    interval_minutes: u32,
) -> Result<GenerateResult, String> {
    let trimmed_url = url.trim();
    if trimmed_url.is_empty() {
        return Err("Paste a YouTube URL first.".to_string());
    }

    let podcast_root = podcast_root()?;
    let runner = scripts_dir()?.join("podcast_desktop_runner.py");

    let output_root = desktop_output_root()?;
    fs::create_dir_all(&output_root).map_err(|err| err.to_string())?;

    let folder_name = format!("podcast_{}", Local::now().format("%Y%m%d_%H%M%S"));
    let output_dir = output_root.join(folder_name);
    fs::create_dir_all(&output_dir).map_err(|err| err.to_string())?;

    let mut command = Command::new(python_bin(&podcast_root));
    command
        .arg(runner)
        .arg("--url")
        .arg(trimmed_url)
        .arg("--title")
        .arg(title.trim())
        .arg("--format")
        .arg(if output_format == "pdf" { "pdf" } else { "epub" })
        .arg("--language")
        .arg(language)
        .arg("--translate")
        .arg(translate)
        .arg("--interval-seconds")
        .arg((interval_minutes.max(1) * 60).to_string())
        .current_dir(&output_dir)
        .env("PYTHONPATH", &podcast_root)
        .env(RUNTIME_ENV, &podcast_root);

    if ai_clean {
        command.arg("--ai-clean").arg("--clean-mode").arg(clean_mode);
    }
    if auto_chapters {
        command.arg("--auto-chapters");
    }

    // .env lives at the checkout root, next to setup.sh - not inside runtime/,
    // and not inside a packaged .app
    if let Some(checkout) = checkout_root() {
        for (key, value) in read_env_file(&checkout.join(".env")) {
            command.env(key, value);
        }
    }

    let output = command.output().map_err(|err| err.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let log = format!("{}{}", stdout, stderr);

    if !output.status.success() {
        return Err(if log.trim().is_empty() {
            "Podcast Ebook failed without producing logs.".to_string()
        } else {
            log
        });
    }

    Ok(GenerateResult {
        output_dir: output_dir.to_string_lossy().to_string(),
        files: collect_files(&output_dir)?,
        log,
        metadata: read_metadata(&output_dir),
    })
}

#[tauri::command]
async fn generate_podcast_ebook(
    url: String,
    title: String,
    output_format: String,
    language: String,
    translate: String,
    ai_clean: bool,
    clean_mode: String,
    auto_chapters: bool,
    interval_minutes: u32,
) -> Result<GenerateResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        generate_podcast_ebook_impl(
            url,
            title,
            output_format,
            language,
            translate,
            ai_clean,
            clean_mode,
            auto_chapters,
            interval_minutes,
        )
    })
    .await
    .map_err(|err| err.to_string())?
}

fn translate_document_impl(
    file_path: String,
    title: String,
    target: String,
    mode: String,
) -> Result<GenerateResult, String> {
    let source_file = PathBuf::from(file_path.trim());
    if !source_file.exists() || !source_file.is_file() {
        return Err("Choose a document file first.".to_string());
    }

    let podcast_root = podcast_root()?;
    let runner = scripts_dir()?.join("pdf_translation_runner.py");

    let output_root = desktop_output_root()?;
    fs::create_dir_all(&output_root).map_err(|err| err.to_string())?;

    let folder_name = format!(
        "document_translation_{}",
        Local::now().format("%Y%m%d_%H%M%S")
    );
    let output_dir = output_root.join(folder_name);
    fs::create_dir_all(&output_dir).map_err(|err| err.to_string())?;

    let normalized_target = if target == "en" { "en" } else { "zh" };
    let normalized_mode = match mode.as_str() {
        "quick" => "quick",
        "refined" => "refined",
        _ => "normal",
    };

    let mut command = Command::new(python_bin(&podcast_root));
    command
        .arg(runner)
        .arg("--file")
        .arg(&source_file)
        .arg("--title")
        .arg(title.trim())
        .arg("--target")
        .arg(normalized_target)
        .arg("--mode")
        .arg(normalized_mode)
        .current_dir(&output_dir)
        .env("PYTHONPATH", &podcast_root)
        .env(RUNTIME_ENV, &podcast_root);

    // .env lives at the checkout root, next to setup.sh - not inside runtime/,
    // and not inside a packaged .app
    if let Some(checkout) = checkout_root() {
        for (key, value) in read_env_file(&checkout.join(".env")) {
            command.env(key, value);
        }
    }

    let output = command.output().map_err(|err| err.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let log = format!("{}{}", stdout, stderr);

    if !output.status.success() {
        return Err(if log.trim().is_empty() {
            "Document translation failed without producing logs.".to_string()
        } else {
            log
        });
    }

    Ok(GenerateResult {
        output_dir: output_dir.to_string_lossy().to_string(),
        files: collect_files(&output_dir)?,
        log,
        metadata: read_json_metadata(&output_dir, "translation_result.json"),
    })
}

#[tauri::command]
async fn translate_document(
    file_path: String,
    title: String,
    target: String,
    mode: String,
) -> Result<GenerateResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        translate_document_impl(file_path, title, target, mode)
    })
    .await
    .map_err(|err| err.to_string())?
}

#[tauri::command]
fn choose_document_file() -> Result<String, String> {
    let script = r#"set chosenFile to choose file with prompt "Choose a document to translate"
return POSIX path of chosenFile"#;
    let output = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
        .map_err(|err| err.to_string())?;

    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

#[tauri::command]
fn open_output_location(path: String) -> Result<(), String> {
    let requested = PathBuf::from(path);
    let output_root = desktop_output_root()?;
    let target = if requested.is_file() {
        requested.parent().unwrap_or(&requested).to_path_buf()
    } else {
        requested
    };

    if !target.starts_with(&output_root) {
        return Err("This app can only open its own output folder.".to_string());
    }

    Command::new("open")
        .arg(target)
        .status()
        .map_err(|err| err.to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            generate_podcast_ebook,
            translate_document,
            choose_document_file,
            open_output_location
        ])
        .run(tauri::generate_context!())
        .expect("error while running Podcast Ebook");
}
