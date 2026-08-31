---
name: podcast-ebook
description: Work with the Podcast Ebook desktop system. Use when an AI coding agent needs to understand, debug, modify, build, package, or document the Tauri macOS app, Python podcast-to-ebook runtime, YouTube transcript to EPUB/PDF workflow, document translation workflow, Cuimao-style translation process, output folders, or related GitHub changes.
---

# Podcast Ebook

## Core Rule

Treat **Podcast Ebook.app** as the product surface, and treat this repository as the desktop app source. Do not assume an old Streamlit prototype is the product unless the user explicitly asks for Streamlit.

Typical local layout:

```text
Desktop app source:
this repository

Python runtime used by the app:
~/podcast-to-ebook-repo

Installed macOS app:
~/Applications/Podcast Ebook.app

Default app output:
~/Desktop/PodcastToBook
```

These paths can vary by machine. Prefer environment variables when running helper scripts:

```bash
PODCAST_TO_BOOK_REPO=/path/to/podcast-ebook-desktop
PODCAST_TO_BOOK_RUNTIME=/path/to/podcast-to-ebook-repo
PODCAST_TO_BOOK_APP_BIN="/path/to/Podcast Ebook.app/Contents/MacOS/podcast-ebook-desktop"
```

## Decision Tree

If the user asks about the local app, UI, buttons, packaging, or “Podcast Ebook.app”:

1. Work in the desktop app repo.
2. Inspect `frontend/`, `src-tauri/src/main.rs`, and `scripts/`.
3. Build or install the app only after verification.

If the user asks about Python transcript processing, YouTube transcript logic, or runtime dependencies:

1. Check the Python runtime repo.
2. Remember the desktop app calls this repo through its venv and `PYTHONPATH`.
3. Do not commit unrelated local experiments, generated transcripts, venvs, or secrets.

If the user asks about document translation in the app:

1. Use desktop app source first.
2. The app-side runner is `scripts/pdf_translation_runner.py`.
3. Translation output lives under `Desktop/PodcastToBook/document_translation_*`.
4. Supported inputs include PDF, EPUB, DOCX, TXT, Markdown, JSON/JSONL, CSV/TSV, SRT/VTT, HTML, and XML.
5. The runtime repo must have `pypdf` available for PDF input.

If the user asks for a one-off “mirror PDF” matching a specific source PDF:

1. Treat it as an artifact task unless asked to productize it.
2. Do not add source-specific mirror layout code to the app.
3. Generate the artifact in the translation output folder or the location the user requests.

## Standard Engineering Workflow

Before edits:

```bash
git status --short
git branch --show-current
rg --files
```

Preserve unrelated local changes. This project may have dirty worktrees.

For desktop app changes:

```bash
node --check frontend/app.js
python -m py_compile scripts/*.py
cd src-tauri && cargo check
```

If the local runtime repo is available, prefer its venv for Python checks:

```bash
~/podcast-to-ebook-repo/venv/bin/python -m py_compile scripts/*.py
```

To install an updated local app after a release build:

```bash
cd src-tauri
cargo build --release
cp target/release/podcast-ebook-desktop "$HOME/Applications/Podcast Ebook.app/Contents/MacOS/podcast-ebook-desktop"
# runner scripts must ship with the bundle too, or it falls back to the build-time repo path
mkdir -p "$HOME/Applications/Podcast Ebook.app/Contents/Resources/scripts"
cp scripts/*.py "$HOME/Applications/Podcast Ebook.app/Contents/Resources/scripts/"
codesign --force --sign - "$HOME/Applications/Podcast Ebook.app"
open "$HOME/Applications/Podcast Ebook.app"
```

## Git Expectations

Use `codex/` branches for new AI-agent work unless the repo has a different branch convention. Keep changes scoped:

```text
podcast-ebook-desktop: app UI, Tauri commands, desktop runners, app build docs
podcast-to-ebook-repo: runtime dependencies and core Python transcript code
```

Do not stage generated books, translations, `.env`, API keys, venvs, transcripts, or large media unless the user explicitly asks.

When a change spans both repos, create separate commits/PRs per repo.

## Translation Workflow

Document translation should follow the Cuimao-inspired flow:

```text
extract source document text
analyze content, tone, terms, and risks
build glossary/style guidance
translate in chunks
optionally refine chunk-by-chunk
write traceable outputs
```

Use `normal` mode by default. Use `refined` only when the user wants higher quality and accepts longer runtime/cost.

Typical output files:

```text
analysis.md
source_extracted.md
chunks/chunk_001.md ...
*_zh_translation.epub or *_en_translation.epub
*_zh_translation.pdf or *_en_translation.pdf
*_zh_translation.md or *_en_translation.md
translation_result.json
manifest.md
```

## References

Read only when needed:

- `references/app-architecture.md`: app/source/runtime map and command flow.
- `references/pdf-translation.md`: translation modes, output contract, and one-off mirror PDF guidance.

## Scripts

Use these lightweight checks before deeper work:

```bash
skills/podcast-ebook/scripts/check_app.sh
skills/podcast-ebook/scripts/check_runtime.sh
```
