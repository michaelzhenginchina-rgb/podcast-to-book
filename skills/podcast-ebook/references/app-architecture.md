# Podcast Ebook Architecture

## Product Surface

The user-facing product is a Tauri desktop app:

```text
Podcast Ebook.app
```

The desktop app source is this repository.

Important files:

```text
frontend/index.html        UI structure
frontend/app.js            Browser-side app logic and Tauri invoke calls
frontend/styles.css        UI styling
src-tauri/src/main.rs      Tauri commands and process orchestration
scripts/podcast_desktop_runner.py
scripts/pdf_translation_runner.py
README.md
```

`src-tauri/src/main.rs` calls Python scripts through `Command`.

## Python Runtime

The desktop app expects a separate Python runtime repository. A common local path is:

```text
~/podcast-to-ebook-repo
```

The app usually uses:

```text
~/podcast-to-ebook-repo/venv/bin/python
~/podcast-to-ebook-repo/.env
```

The app sets `PYTHONPATH` to the runtime repo so app-side scripts can import runtime modules.

Do not assume dependencies installed in a different project venv are available to the desktop app.

## App Output

Default output:

```text
~/Desktop/PodcastToBook
```

YouTube ebook runs:

```text
podcast_YYYYMMDD_HHMMSS/
```

Document translation runs:

```text
document_translation_YYYYMMDD_HHMMSS/
```

## Verification

Run from the desktop app repo:

```bash
node --check frontend/app.js
python -m py_compile scripts/*.py
cd src-tauri && cargo check
```

If the runtime venv exists, use it for Python checks:

```bash
~/podcast-to-ebook-repo/venv/bin/python -m py_compile scripts/pdf_translation_runner.py scripts/podcast_desktop_runner.py
```

Install a local app binary after a release build:

```bash
cd src-tauri
cargo build --release
cp target/release/podcast-ebook-desktop "$HOME/Applications/Podcast Ebook.app/Contents/MacOS/podcast-ebook-desktop"
```
