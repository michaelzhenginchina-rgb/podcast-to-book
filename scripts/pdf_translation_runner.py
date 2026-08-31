#!/usr/bin/env python3
import argparse
import csv
import html
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from io import StringIO
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_paths import ensure_importable, env_file  # noqa: E402

ensure_importable()
import llm_config  # noqa: E402

try:
    from dotenv import load_dotenv

    _env = env_file()
    if _env:
        load_dotenv(_env)
except ImportError:
    pass

try:
    from openai import OpenAI
except ImportError as error:
    raise RuntimeError("Missing dependency: openai. Install requirements in the Podcast Ebook venv.") from error

TARGETS = {
    "zh": {
        "label": "Chinese Simplified",
        "native": "中文简体",
        "suffix": "zh",
        "system": (
            "You are a senior English-to-Chinese book translator. Translate faithfully "
            "into fluent Simplified Chinese. Preserve meaning, argument structure, "
            "names, numbers, and technical terms. Avoid translationese and stiff "
            "literal English word order. Return only the translated text."
        ),
        "analysis_focus": "English-to-Chinese terminology, tone, proper nouns, and stable Chinese renderings.",
    },
    "en": {
        "label": "English",
        "native": "English",
        "suffix": "en",
        "system": (
            "You are a senior Chinese-to-English book translator. Translate faithfully "
            "into polished, natural English. Preserve meaning, argument structure, "
            "names, numbers, and technical terms. Avoid stiff literal phrasing. "
            "Return only the translated text."
        ),
        "analysis_focus": "Chinese-to-English terminology, tone, proper nouns, and stable English renderings.",
    },
}

MODES = {
    "quick": "Translate directly. Keep formatting simple and readable.",
    "normal": "Use the analysis and glossary to keep terms consistent in a book-like style.",
    "refined": "Translate carefully, then run a second chunk-level polish pass.",
}


class Usage:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self):
        return (self.input_tokens / 1_000_000 * 0.15) + (
            self.output_tokens / 1_000_000 * 0.60
        )

    @property
    def cost_cny(self):
        return self.cost_usd * 7.2

    def add(self, response):
        if not getattr(response, "usage", None):
            return
        self.input_tokens += response.usage.prompt_tokens
        self.output_tokens += response.usage.completion_tokens

    def as_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 4),
            "cost_cny": round(self.cost_cny, 2),
        }


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".srt",
    ".vtt",
    ".html",
    ".htm",
    ".xml",
}


def sanitize_filename(name):
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "translated-document"


def normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_text(pdf_path):
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError(
            "Missing dependency: pypdf. Install requirements in the Podcast Ebook venv."
        ) from error

    reader = PdfReader(str(pdf_path))
    pages = []
    for index, page in enumerate(reader.pages, 1):
        text = normalize_text(page.extract_text() or "")
        if text:
            pages.append(f"\n\n<!-- Page {index} -->\n\n{text}")
    extracted = "\n".join(pages).strip()
    if not extracted:
        raise RuntimeError("No selectable text found. Scanned PDFs need OCR first.")
    return extracted


def read_text_file(path):
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Could not read text from {path.name}. Unsupported encoding.")


def strip_markup(text):
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def flatten_json(value, prefix=""):
    if isinstance(value, dict):
        lines = []
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            lines.extend(flatten_json(child, child_prefix))
        return lines
    if isinstance(value, list):
        lines = []
        for index, child in enumerate(value, 1):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            lines.extend(flatten_json(child, child_prefix))
        return lines
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool)):
        label = f"{prefix}: " if prefix else ""
        return [f"{label}{value}"]
    return [f"{prefix}: {value}" if prefix else str(value)]


def extract_json_text(path):
    content = read_text_file(path)
    lines = []
    try:
        data = json.loads(content)
        lines = flatten_json(data)
    except json.JSONDecodeError:
        for raw_line in content.splitlines():
            if not raw_line.strip():
                continue
            try:
                lines.extend(flatten_json(json.loads(raw_line)))
            except json.JSONDecodeError:
                lines.append(raw_line)
    extracted = normalize_text("\n".join(lines))
    if not extracted:
        raise RuntimeError("No translatable text found in JSON file.")
    return extracted


def extract_csv_text(path, delimiter):
    content = read_text_file(path)
    rows = []
    reader = csv.reader(StringIO(content), delimiter=delimiter)
    for row in reader:
        values = [cell.strip() for cell in row if cell.strip()]
        if values:
            rows.append(" | ".join(values))
    extracted = normalize_text("\n".join(rows))
    if not extracted:
        raise RuntimeError("No translatable text found in table file.")
    return extracted


def extract_epub_text(path):
    sections = []
    with zipfile.ZipFile(path) as book:
        names = [
            name
            for name in book.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm"))
        ]
        for name in sorted(names):
            raw = book.read(name).decode("utf-8", errors="ignore")
            text = normalize_text(strip_markup(raw))
            if text:
                sections.append(f"\n\n<!-- {name} -->\n\n{text}")
    extracted = "\n".join(sections).strip()
    if not extracted:
        raise RuntimeError("No translatable text found in EPUB.")
    return extracted


def extract_docx_text(path):
    paragraphs = []
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as document:
        names = [
            name
            for name in document.namelist()
            if name == "word/document.xml"
            or name.startswith("word/header")
            or name.startswith("word/footer")
        ]
        for name in names:
            root = ElementTree.fromstring(document.read(name))
            for paragraph in root.findall(".//w:p", namespace):
                text = "".join(
                    node.text or ""
                    for node in paragraph.findall(".//w:t", namespace)
                ).strip()
                if text:
                    paragraphs.append(text)
    extracted = normalize_text("\n\n".join(paragraphs))
    if not extracted:
        raise RuntimeError("No translatable text found in DOCX.")
    return extracted


def extract_document_text(source_path):
    extension = source_path.suffix.lower()
    if extension == ".pdf":
        return extract_pdf_text(source_path), "PDF"
    if extension == ".epub":
        return extract_epub_text(source_path), "EPUB"
    if extension == ".docx":
        return extract_docx_text(source_path), "DOCX"
    if extension == ".json" or extension == ".jsonl":
        return extract_json_text(source_path), "JSON"
    if extension == ".csv":
        return extract_csv_text(source_path, ","), "CSV"
    if extension == ".tsv":
        return extract_csv_text(source_path, "\t"), "TSV"
    if extension in {".html", ".htm", ".xml"}:
        return normalize_text(strip_markup(read_text_file(source_path))), extension[1:].upper()
    if extension in TEXT_EXTENSIONS:
        return normalize_text(read_text_file(source_path)), extension[1:].upper()
    raise RuntimeError(
        "Unsupported file type. Try PDF, EPUB, DOCX, TXT, Markdown, JSON, CSV, TSV, SRT, VTT, HTML, or XML."
    )


def split_long_paragraph(paragraph, max_chars):
    sentences = re.split(r"(?<=[.!?。！？])\s+", paragraph)
    chunks = []
    current = []
    current_size = 0
    for sentence in sentences:
        if current and current_size + len(sentence) + 1 > max_chars:
            chunks.append(" ".join(current))
            current = []
            current_size = 0
        current.append(sentence)
        current_size += len(sentence) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def split_text(text, max_chars=6000):
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks = []
    current = []
    current_size = 0
    for paragraph in paragraphs:
        if current and current_size + len(paragraph) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_size = 0
        if len(paragraph) > max_chars:
            chunks.extend(split_long_paragraph(paragraph, max_chars))
            continue
        current.append(paragraph)
        current_size += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


class Translator:
    def __init__(self, api_key, model):
        if not api_key:
            raise RuntimeError(
                "No API key. Add LLM_API_KEY (or OPENAI_API_KEY) to .env "
                "(see .env.example) or export it in your shell."
            )
        self.client = llm_config.client(api_key=api_key, timeout=90, max_retries=2)
        self.model = model
        self.usage = Usage()

    def chat(self, system, user, temperature=0.25, max_tokens=None):
        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        response = self.client.chat.completions.create(**kwargs)
        self.usage.add(response)
        return response.choices[0].message.content.strip()

    def analyze(self, source_text, title, target, source_kind):
        target_info = TARGETS[target]
        sample = source_text[:12000]
        return self.chat(
            "You are a translation project editor preparing a book translation.",
            f"""Analyze this {source_kind} document before translation.

Title: {title}
Source format: {source_kind}
Target language: {target_info['label']} ({target_info['native']})
Focus: {target_info['analysis_focus']}

Return Markdown with:
1. Short content summary
2. Translation style guide
3. Glossary table with source term, target term, and note
4. Risks: ambiguity, formatting, terminology, or names to watch

DOCUMENT SAMPLE:
{sample}
""",
            temperature=0.2,
        )

    def translate_chunk(self, chunk, analysis, target, mode, index, total, source_kind):
        target_info = TARGETS[target]
        return self.chat(
            target_info["system"],
            f"""Translate this {source_kind} document chunk.

Target language: {target_info['label']} ({target_info['native']})
Source format: {source_kind}
Chunk: {index}/{total}
Mode: {mode}
Mode instruction: {MODES[mode]}

Use this project analysis and glossary for consistency:
{analysis}

Rules:
- Preserve headings, bullets, numbered lists, names, dates, and figures.
- Do not summarize unless the source itself summarizes.
- Do not add commentary.
- If a line appears to be a page marker, omit it from the translation.

SOURCE CHUNK:
{chunk}
""",
            temperature=0.25,
        )

    def refine_chunk(self, translated_chunk, analysis, target, index, total):
        target_info = TARGETS[target]
        return self.chat(
            target_info["system"],
            f"""Polish this translated chunk.

Target language: {target_info['label']} ({target_info['native']})
Chunk: {index}/{total}

Use this analysis and glossary:
{analysis}

Fix omissions, mistranslations, inconsistent terms, awkward literal phrasing,
and paragraph/heading formatting. Return only the final polished chunk.

TRANSLATED CHUNK:
{translated_chunk}
""",
            temperature=0.2,
        )


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def paragraph_blocks(text):
    return [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]


def block_to_html(block):
    if block.startswith("#"):
        level = min(len(block) - len(block.lstrip("#")), 3)
        heading = html.escape(block[level:].strip() or block)
        return f"<h{level}>{heading}</h{level}>"
    if re.match(r"^[-*]\s+", block):
        items = []
        for line in block.splitlines():
            line = line.strip()
            if line:
                item_text = re.sub(r"^[-*]\s+", "", line).strip()
                items.append(f"<li>{html.escape(item_text)}</li>")
        return f"<ul>{''.join(items)}</ul>"
    return f"<p class='block'>{html.escape(block).replace(chr(10), '<br/>')}</p>"


def write_translation_epub(title, target_label, translated_chunks, output_file):
    try:
        from ebooklib import epub
    except ImportError as error:
        raise RuntimeError("Missing dependency: ebooklib. Install requirements in the Podcast Ebook venv.") from error

    book = epub.EpubBook()
    book.set_identifier(title)
    book.set_title(title)
    book.set_language("zh" if target_label.startswith("Chinese") else "en")
    book.add_author("Podcast Ebook")

    style = epub.EpubItem(
        uid="style_nav",
        file_name="style/nav.css",
        media_type="text/css",
        content="""
        body { font-family: serif; font-size: 1.15em; line-height: 1.78; margin: 2em; }
        h1, h2, h3 { text-align: center; line-height: 1.25; margin: 1.4em 0 .9em; }
        p.block { margin: 0 0 1.15em 0; text-indent: 0; }
        ul { margin: .8em 0 1.2em 1.4em; }
        li { margin: .35em 0; }
        .meta { text-align: center; color: #666; margin-top: 1em; }
        """,
    )
    book.add_item(style)

    title_page = epub.EpubHtml(title="Title Page", file_name="title.xhtml", lang="en")
    title_page.content = f"""
    <html><head></head><body>
    <h1>{html.escape(title)}</h1>
    <p class="meta">Translated book / {html.escape(target_label)}</p>
    </body></html>
    """
    title_page.add_item(style)
    book.add_item(title_page)

    toc = [epub.Link("title.xhtml", "Title Page", "title_page")]
    spine = ["nav", title_page]

    for index, chunk in enumerate(translated_chunks, 1):
        chapter_title = "Translation" if len(translated_chunks) == 1 else f"Part {index}"
        chapter = epub.EpubHtml(
            title=chapter_title,
            file_name=f"part_{index:03d}.xhtml",
            lang="zh" if target_label.startswith("Chinese") else "en",
        )
        body = "\n".join(block_to_html(block) for block in paragraph_blocks(chunk))
        chapter.content = f"<h2>{html.escape(chapter_title)}</h2>\n{body}"
        chapter.add_item(style)
        book.add_item(chapter)
        toc.append(epub.Link(chapter.file_name, chapter_title, f"part_{index:03d}"))
        spine.append(chapter)

    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(output_file, book)


def pdf_font_name():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    for font_name in ("STSong-Light", "HeiseiMin-W3"):
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
            return font_name
        except Exception:
            pass

    for font_name, font_path in (
        ("ArialUnicode", "/System/Library/Fonts/Arial Unicode MS.ttf"),
        ("PingFang", "/System/Library/Fonts/PingFang.ttc"),
    ):
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path))
            return font_name
        except Exception:
            pass

    return "Helvetica"


def write_translation_pdf(title, target_label, translated_chunks, output_file):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as error:
        raise RuntimeError("Missing dependency: reportlab. Install requirements in the Podcast Ebook venv.") from error

    font_name = pdf_font_name()
    doc = SimpleDocTemplate(
        output_file,
        pagesize=letter,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BookTitle",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=20,
        leading=26,
        alignment=1,
        spaceAfter=14,
    )
    subtitle_style = ParagraphStyle(
        "BookSubtitle",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=11,
        leading=15,
        alignment=1,
        textColor="#666666",
        spaceAfter=36,
    )
    heading_style = ParagraphStyle(
        "BookHeading",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=16,
        leading=21,
        spaceBefore=18,
        spaceAfter=14,
    )
    body_style = ParagraphStyle(
        "BookBody",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=12.5,
        leading=19,
        spaceAfter=12,
        firstLineIndent=0,
    )
    bullet_style = ParagraphStyle(
        "BookBullet",
        parent=body_style,
        leftIndent=18,
        firstLineIndent=-10,
    )

    story = [
        Paragraph(html.escape(title), title_style),
        Paragraph(html.escape(f"Translated book / {target_label}"), subtitle_style),
    ]

    for index, chunk in enumerate(translated_chunks, 1):
        if len(translated_chunks) > 1:
            story.append(Paragraph(f"Part {index}", heading_style))
        for block in paragraph_blocks(chunk):
            if block.startswith("#"):
                heading = html.escape(block.lstrip("#").strip() or block)
                story.append(Paragraph(heading, heading_style))
            elif re.match(r"^[-*]\s+", block):
                for line in block.splitlines():
                    item = re.sub(r"^[-*]\s+", "", line.strip()).strip()
                    if item:
                        story.append(Paragraph(f"- {html.escape(item)}", bullet_style))
            else:
                story.append(Paragraph(html.escape(block).replace("\n", "<br/>"), body_style))
        if len(translated_chunks) > 1 and index < len(translated_chunks):
            story.append(PageBreak())

    doc.build(story)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="")
    parser.add_argument("--pdf", default="", help=argparse.SUPPRESS)
    parser.add_argument("--title", default="")
    parser.add_argument("--target", choices=["zh", "en"], default="zh")
    parser.add_argument("--mode", choices=["quick", "normal", "refined"], default="normal")
    parser.add_argument("--model", default=None)
    return parser.parse_args()


def main():
    started_at = time.perf_counter()
    args = parse_args()
    source_arg = args.file or args.pdf
    if not source_arg:
        raise RuntimeError("Choose a document to translate.")
    source_path = Path(source_arg).expanduser().resolve()
    if not source_path.exists():
        raise RuntimeError(f"Document not found: {source_path}")

    title = args.title.strip() or source_path.stem
    safe_title = sanitize_filename(title)
    target_info = TARGETS[args.target]
    api_key = llm_config.api_key()

    print(f"Source file: {source_path}", flush=True)
    print(f"Title: {title}", flush=True)
    print(f"Target: {target_info['label']}", flush=True)
    print(f"Mode: {args.mode}", flush=True)

    source_text, source_kind = extract_document_text(source_path)
    chunks = split_text(source_text)
    print(
        f"Extracted {len(source_text):,} characters from {source_kind} into {len(chunks)} chunks.",
        flush=True,
    )

    Path("source_extracted.md").write_text(source_text, encoding="utf-8")
    translator = Translator(api_key=api_key, model=args.model or llm_config.model())

    print("Analyzing style and glossary...", flush=True)
    analysis = translator.analyze(source_text, title, args.target, source_kind)
    Path("analysis.md").write_text(analysis, encoding="utf-8")

    chunks_dir = Path("chunks")
    chunks_dir.mkdir(exist_ok=True)
    translated_chunks = []
    for index, chunk in enumerate(chunks, 1):
        print(f"Translating chunk {index}/{len(chunks)}...", flush=True)
        translated = translator.translate_chunk(
            chunk,
            analysis,
            args.target,
            args.mode,
            index,
            len(chunks),
            source_kind,
        )
        translated_chunks.append(translated)
        (chunks_dir / f"chunk_{index:03d}.md").write_text(translated, encoding="utf-8")

    if args.mode == "refined":
        refined_chunks = []
        for index, chunk in enumerate(translated_chunks, 1):
            print(f"Refining chunk {index}/{len(translated_chunks)}...", flush=True)
            refined = translator.refine_chunk(chunk, analysis, args.target, index, len(translated_chunks))
            refined_chunks.append(refined)
            (chunks_dir / f"chunk_{index:03d}_refined.md").write_text(refined, encoding="utf-8")
        translated_chunks = refined_chunks

    translated_text = "\n\n".join(translated_chunks)
    final_file = f"{safe_title}_{target_info['suffix']}_translation.md"
    epub_file = f"{safe_title}_{target_info['suffix']}_translation.epub"
    pdf_file = f"{safe_title}_{target_info['suffix']}_translation.pdf"
    Path(final_file).write_text(translated_text, encoding="utf-8")
    write_translation_epub(title, target_info["label"], translated_chunks, epub_file)
    write_translation_pdf(title, target_info["label"], translated_chunks, pdf_file)

    manifest = {
        "kind": "document_translation",
        "title": title,
        "source_file": str(source_path),
        "source_format": source_kind,
        "target": args.target,
        "target_label": target_info["label"],
        "mode": args.mode,
        "model": args.model,
        "chunks_count": len(chunks),
        "characters": len(source_text),
        "usage_stats": translator.usage.as_dict(),
        "timing": {"total_seconds": round(time.perf_counter() - started_at, 2)},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": {
            "translation": final_file,
            "epub": epub_file,
            "pdf": pdf_file,
            "analysis": "analysis.md",
            "source": "source_extracted.md",
            "manifest": "translation_result.json",
        },
    }
    write_json(Path("translation_result.json"), manifest)
    Path("manifest.md").write_text(
        f"""# Document Translation Manifest

Title: {title}
Source file: {source_path}
Source format: {source_kind}
Target: {target_info['label']}
Mode: {args.mode}
Model: {args.model}
Chunks: {len(chunks)}
Characters: {len(source_text):,}
Estimated cost: ${translator.usage.cost_usd:.4f}

## Files

- {final_file}
- {epub_file}
- {pdf_file}
- analysis.md
- source_extracted.md
- chunks/
- translation_result.json
""",
        encoding="utf-8",
    )
    print("Translation complete.", flush=True)
    print(f"Final translation: {final_file}", flush=True)
    print(f"EPUB book: {epub_file}", flush=True)
    print(f"PDF book: {pdf_file}", flush=True)


if __name__ == "__main__":
    main()
