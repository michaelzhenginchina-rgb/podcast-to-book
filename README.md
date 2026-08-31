# Podcast to Book

**Turn a YouTube video or podcast into a clean ebook you can actually read.**

Long videos are hard to study. This turns one into a proper EPUB or PDF: it
pulls the transcript, strips the *ums* and false starts with an LLM, splits it
into chapters, and writes a book — optionally translated between English and
Chinese.

```text
YouTube URL  →  transcript  →  AI clean  →  chapters  →  EPUB / PDF
```

A second workflow does the same for documents you already have — PDF, EPUB,
DOCX, TXT, Markdown, JSON, CSV, subtitles, HTML — extracting the text, building
a glossary, translating, and packaging the result as a book.

macOS desktop app (Tauri + Python). Everything runs locally except the LLM
calls, which go to your own OpenAI key.

---

## Setup

```bash
git clone https://github.com/michaelzhenginchina-rgb/podcast-to-book.git
cd podcast-to-book
./setup.sh
```

`setup.sh` creates `.venv/`, installs the Python dependencies, and copies
`.env.example` to `.env`. Put your OpenAI key in that `.env`, then:

```bash
cargo install tauri-cli     # once
cargo tauri dev             # run it
cargo tauri build           # or build Podcast to Book.app
```

**Requirements:** macOS 10.13+, Rust, Python 3.9+, and an
[OpenAI API key](https://platform.openai.com/api-keys).

## Configuration

`OPENAI_API_KEY` is the only thing you need. The app asks for nothing else — no
account, no email, no password.

Everything is resolved relative to the repository, so a clone works wherever you
put it. These overrides exist but are rarely needed:

| Variable | Purpose |
|---|---|
| `PODCAST_TO_BOOK_RUNTIME` | Use a Python runtime outside `runtime/` |
| `PODCAST_TO_BOOK_REPO` | Point a built app at a different checkout |
| `PODCAST_TO_BOOK_PYTHON` | Use a specific interpreter |

## Reading it

The app produces a file and stops there — it deliberately does not touch your
email or your reader account. Move it however you prefer:

- **USB** — plug the Kindle in and drop the EPUB into `documents/`.
- **Send to Kindle** — drag the file onto
  [Amazon's upload page](https://www.amazon.com/sendtokindle).
- **Email** — forward it to your own `@kindle.com` address.
- **Anything else** — it is a plain EPUB or PDF, so Apple Books, Calibre and
  every other reader open it too.

Pick **EPUB** for Kindle and most e-readers; **PDF** keeps fixed layout.

## Output

```text
~/Desktop/Retrona_Tools_Output/podcast-ebooks/
  podcast_YYYYMMDD_HHMMSS/               ebook runs
  document_translation_YYYYMMDD_HHMMSS/  translation runs
```

## Layout

| Path | What it is |
|---|---|
| `frontend/` | UI — plain HTML/CSS/JS, no framework |
| `src-tauri/` | Rust shell: resolves paths, spawns Python, collects results |
| `runtime/main.py` | The pipeline — transcript, chapters, EPUB, PDF |
| `runtime/transcript_cleaner.py` | LLM cleanup of raw transcript text |
| `scripts/` | Runners the app invokes, plus shared path resolution |
| `skills/podcast-ebook/` | Agent skill describing this system |

`scripts/` and `runtime/` are bundled into the built `.app` under
`Contents/Resources`, so a packaged build does not depend on the repository
staying where it was built.

## Cost

Transcript cleaning and translation call the OpenAI API and cost real money.
A typical hour-long video runs a few cents on `gpt-4o-mini`. The app reports
token usage and cost after each run.

## Known limits

- macOS only. Builds are ad-hoc signed, so sharing the `.app` itself needs
  proper signing and notarization — clone and build instead.
- No automated tests.
- Scanned PDFs need OCR before the translation workflow can read them.
- Needs a video that actually has a transcript. YouTube auto-captions work;
  videos with captions disabled do not.

## License

MIT — see [LICENSE](LICENSE).
