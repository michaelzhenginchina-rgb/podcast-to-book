import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from youtube_transcript_api import YouTubeTranscriptApi


sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_paths  # noqa: E402
from runtime_paths import ensure_importable  # noqa: E402

PODCAST_ROOT = ensure_importable()
import llm_config  # noqa: E402

from main import (  # noqa: E402
    extract_video_id,
    fetch_video_metadata,
    generate_pdf,
    group_transcript_by_chapters,
    entries_to_paragraphs,
    group_transcript_by_interval,
    normalize_caption_text,
    sanitize_filename,
    sanitize_transcript_entries,
    format_timestamp,
    save_raw_transcript,
)
from ebooklib import epub  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402


def load_local_env():
    # .env sits at the repo root, next to setup.sh - not inside runtime/
    env_file = runtime_paths.env_file()
    if env_file is None:
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Entry:
    def __init__(self, text, start, duration=0):
        self.text = text
        self.start = start
        self.duration = duration


class Timer:
    def __init__(self):
        self.started_at = time.perf_counter()
        self.steps = {}

    def mark(self, name, seconds):
        self.steps[name] = round(seconds, 2)

    def total(self):
        return round(time.perf_counter() - self.started_at, 2)


def timed(timer, name, callback):
    started_at = time.perf_counter()
    result = callback()
    timer.mark(name, time.perf_counter() - started_at)
    return result


def fetch_youtube_metadata(url, video_id):
    """Title, thumbnail and chapters from one metadata lookup."""
    metadata = fetch_video_metadata(url, video_id)
    if not metadata["title"]:
        metadata["title"] = f"YouTube Transcript {video_id}"
    return metadata


def wrap_text(draw, text, font, max_width, max_lines=7):
    words = re.split(r"(\s+)", text)
    lines = []
    current = ""
    for word in words:
        candidate = current + word
        bbox = draw.textbbox((0, 0), candidate.strip(), font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current.strip():
                lines.append(current.strip())
            current = word.strip()
            if len(lines) >= max_lines:
                break
    if current.strip() and len(lines) < max_lines:
        lines.append(current.strip())
    if len(lines) == max_lines and len(" ".join(words).strip()) > len(" ".join(lines)):
        lines[-1] = lines[-1].rstrip(".") + "..."
    return lines


def load_cover_font(size):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def download_thumbnail(thumbnail_url, raw_path):
    try:
        request = urllib.request.Request(
            thumbnail_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw_path.write_bytes(response.read())
        return
    except Exception as error:
        print(f"Python thumbnail download failed: {error}")

    subprocess.run(
        ["curl", "-L", "--fail", "--silent", "--show-error", thumbnail_url, "-o", str(raw_path)],
        timeout=30,
        check=True,
    )


def build_cover_image(title, thumbnail_url, output_path):
    if not thumbnail_url:
        return None

    raw_path = Path("youtube_thumbnail")
    try:
        download_thumbnail(thumbnail_url, raw_path)
        source = Image.open(raw_path).convert("RGB")
    except Exception as error:
        print(f"Could not download thumbnail cover: {error}")
        return None
    finally:
        try:
            raw_path.unlink()
        except Exception:
            pass

    width, height = 1600, 2400
    cover = Image.new("RGB", (width, height), "#0b0b0b")

    bg = source.copy()
    bg_ratio = max(width / bg.width, height / bg.height)
    bg = bg.resize((int(bg.width * bg_ratio), int(bg.height * bg_ratio)))
    left = (bg.width - width) // 2
    top = (bg.height - height) // 2
    bg = bg.crop((left, top, left + width, top + height))
    bg = bg.filter(ImageFilter.GaussianBlur(28))
    cover.paste(bg)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 130))
    cover = Image.alpha_composite(cover.convert("RGBA"), overlay)

    thumb_width = 1280
    thumb = source.copy()
    thumb_ratio = thumb_width / thumb.width
    thumb = thumb.resize((thumb_width, int(thumb.height * thumb_ratio)))
    thumb_height = min(thumb.height, 900)
    thumb = thumb.crop((0, max(0, (thumb.height - thumb_height) // 2), thumb_width, max(0, (thumb.height - thumb_height) // 2) + thumb_height))

    thumb_layer = Image.new("RGBA", thumb.size, (0, 0, 0, 0))
    thumb_layer.paste(thumb.convert("RGBA"))
    thumb_x = (width - thumb.width) // 2
    thumb_y = 260
    shadow = Image.new("RGBA", (thumb.width + 80, thumb.height + 80), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((40, 40, thumb.width + 40, thumb.height + 40), radius=28, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    cover.alpha_composite(shadow, (thumb_x - 40, thumb_y - 20))
    mask = Image.new("L", thumb.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, thumb.width, thumb.height), radius=28, fill=255)
    cover.paste(thumb_layer, (thumb_x, thumb_y), mask)

    draw = ImageDraw.Draw(cover)
    font = load_cover_font(76)
    small_font = load_cover_font(34)
    lines = wrap_text(draw, title, font, width - 220, max_lines=6)
    y = thumb_y + thumb.height + 135
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text(((width - (bbox[2] - bbox[0])) / 2, y), line, fill=(255, 255, 255, 245), font=font)
        y += 92
    draw.text((110, height - 180), "Podcast Ebook", fill=(225, 225, 218, 200), font=small_font)
    draw.line((110, height - 225, width - 110, height - 225), fill=(255, 255, 255, 70), width=2)

    final = cover.convert("RGB")
    final.save(output_path, quality=92)
    return output_path


def sections_sample_text(sections, max_chars=3600):
    parts = []
    for index, section in enumerate(sections[:5], 1):
        title = section_title(index, section)
        entries = section[2]
        text = entries_to_text(entries[:30])
        if title:
            parts.append(title)
        if text:
            parts.append(text)
        if len("\n".join(parts)) >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


def fallback_cover_concept(title, sections):
    chapter_names = [section_title(index, section) for index, section in enumerate(sections[:3], 1)]
    return {
        "short_title": title,
        "subtitle": "A cleaned podcast ebook",
        "palette": ["#0b0b0b", "#22231f", "#f4f0e8", "#8f9b8b"],
        "motif": "minimal editorial lines",
        "keywords": chapter_names,
    }


def llm_cover_concept(title, sections, api_key):
    if not api_key:
        return fallback_cover_concept(title, sections)

    from openai import OpenAI

    client = llm_config.client(api_key=api_key, timeout=45)
    sample = sections_sample_text(sections)
    response = client.chat.completions.create(
        model=llm_config.model(),
        messages=[
            {
                "role": "system",
                "content": (
                    "You design elegant, minimalist ebook covers. Return only valid JSON "
                    "with keys: short_title, subtitle, palette, motif, keywords. "
                    "short_title should be under 8 words. subtitle should be under 12 words. "
                    "palette must be 4 hex colors. Avoid generic marketing copy."
                ),
            },
            {
                "role": "user",
                "content": f"Title: {title}\n\nTranscript/topics:\n{sample}",
            },
        ],
        temperature=0.4,
        max_tokens=450,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        concept = json.loads(raw)
    except Exception as error:
        print(f"Could not parse LLM cover concept: {error}")
        return fallback_cover_concept(title, sections)

    fallback = fallback_cover_concept(title, sections)
    return {
        "short_title": concept.get("short_title") or fallback["short_title"],
        "subtitle": concept.get("subtitle") or fallback["subtitle"],
        "palette": concept.get("palette") or fallback["palette"],
        "motif": concept.get("motif") or fallback["motif"],
        "keywords": concept.get("keywords") or fallback["keywords"],
    }


def valid_hex_color(value, fallback):
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return fallback


def build_generated_cover(title, sections, output_path, api_key=None):
    try:
        concept = llm_cover_concept(title, sections, api_key)
    except Exception as error:
        print(f"LLM cover concept failed: {error}")
        concept = fallback_cover_concept(title, sections)

    palette = concept.get("palette") or []
    bg = valid_hex_color(palette[0] if len(palette) > 0 else None, "#0b0b0b")
    panel = valid_hex_color(palette[1] if len(palette) > 1 else None, "#20211e")
    text = valid_hex_color(palette[2] if len(palette) > 2 else None, "#f4f0e8")
    accent = valid_hex_color(palette[3] if len(palette) > 3 else None, "#8f9b8b")

    width, height = 1600, 2400
    cover = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(cover)
    bg_rgb = Image.new("RGB", (1, 1), bg).getpixel((0, 0))

    for y in range(height):
        ratio = y / height
        shade = int(18 * ratio)
        draw.line((0, y, width, y), fill=tuple(max(0, value - shade) for value in bg_rgb))

    margin = 120
    draw.rounded_rectangle(
        (margin, 170, width - margin, height - 170),
        radius=34,
        fill=panel,
        outline=accent,
        width=3,
    )
    draw.line((margin + 70, 420, width - margin - 70, 420), fill=accent, width=3)
    draw.line((margin + 70, height - 390, width - margin - 70, height - 390), fill=accent, width=3)

    for index in range(9):
        x = margin + 90 + index * 150
        draw.arc((x, 520, x + 190, 710), 205, 330, fill=accent, width=3)
    for index in range(7):
        x = width - margin - 250 - index * 130
        draw.arc((x, height - 650, x + 220, height - 430), 25, 155, fill=accent, width=2)

    title_font = load_cover_font(86)
    subtitle_font = load_cover_font(42)
    meta_font = load_cover_font(30)
    keyword_font = load_cover_font(32)

    short_title = str(concept.get("short_title") or title)
    subtitle = str(concept.get("subtitle") or "A cleaned podcast ebook")
    title_lines = wrap_text(draw, short_title, title_font, width - 360, max_lines=7)
    y = 760
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        draw.text(((width - (bbox[2] - bbox[0])) / 2, y), line, fill=text, font=title_font)
        y += 104

    subtitle_lines = wrap_text(draw, subtitle, subtitle_font, width - 420, max_lines=3)
    y += 44
    for line in subtitle_lines:
        bbox = draw.textbbox((0, 0), line, font=subtitle_font)
        draw.text(((width - (bbox[2] - bbox[0])) / 2, y), line, fill=accent, font=subtitle_font)
        y += 58

    keywords = concept.get("keywords") or []
    keyword_text = " / ".join(str(keyword) for keyword in keywords[:3] if keyword)
    if keyword_text:
        keyword_lines = wrap_text(draw, keyword_text, keyword_font, width - 360, max_lines=2)
        y = height - 330
        for line in keyword_lines:
            bbox = draw.textbbox((0, 0), line, font=keyword_font)
            draw.text(((width - (bbox[2] - bbox[0])) / 2, y), line, fill=text, font=keyword_font)
            y += 44

    draw.text((margin + 70, 300), "Podcast Ebook", fill=text, font=meta_font)
    draw.text((margin + 70, height - 285), "Generated cover", fill=accent, font=meta_font)
    cover.save(output_path, quality=92)
    return output_path


def save_epub_with_cover(title, video_id, chapters, output_filename, cover_path=None):
    book = epub.EpubBook()
    book.set_identifier(title)
    book.set_title(title)
    book.set_language("zh" if re.search(r"[\u4e00-\u9fff]", title) else "en")
    book.add_author("Podcast Ebook")

    if cover_path and Path(cover_path).exists():
        book.set_cover("cover.jpg", Path(cover_path).read_bytes())

    style = epub.EpubItem(
        uid="style_nav",
        file_name="style/nav.css",
        media_type="text/css",
        content="""
        body { font-family: serif; font-size: 1.15em; line-height: 1.75; margin: 2em; }
        h1, h2 { text-align: center; line-height: 1.25; }
        p.block { margin: 0 0 1.15em 0; text-indent: 0; }
        """,
    )
    book.add_item(style)

    title_page = epub.EpubHtml(title="Title Page", file_name="title.xhtml", lang="en")
    escaped_title = html.escape(title)
    escaped_video_id = html.escape(video_id)
    title_page.content = f"""
    <html><head></head><body>
    <h1>{escaped_title}</h1>
    <h3 style='text-align:center;margin-top:1em;'>Podcast Ebook</h3>
    <p style='text-align:center;margin-top:2em;'>Video ID: {escaped_video_id}</p>
    </body></html>
    """
    title_page.add_item(style)
    book.add_item(title_page)

    toc = [epub.Link("title.xhtml", "Title Page", "title_page")]
    spine = ["nav", title_page]
    for section_title, chapter in chapters:
        chapter.add_item(style)
        book.add_item(chapter)
        toc.append(epub.Link(chapter.file_name, section_title, section_title.replace(" ", "_")))
        spine.append(chapter)

    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(output_filename, book)
    print(f"Saved transcript as {output_filename}")


def normalize_language(language):
    language_map = {
        "English (Original)": "en",
        "中文简体 (Chinese Simplified)": "zh-Hans",
        "中文繁體 (Chinese Traditional)": "zh-Hant",
    }
    return language_map.get(language, language)


def fetch_transcript_smart(video_id, language):
    """Fetch transcript with practical fallbacks for YouTube's real language codes.

    YouTube often exposes captions as en-US/en-GB rather than plain en. Its web
    player may also show "Chinese" through auto-translate even when zh-Hans is
    not directly listed. This mirrors that behavior more closely than the older
    CLI path.
    """
    target = normalize_language(language)
    transcript_list = YouTubeTranscriptApi().list(video_id)
    transcripts = list(transcript_list)

    def find_by_codes(codes):
        for code in codes:
            try:
                return transcript_list.find_transcript([code])
            except Exception:
                pass
        for transcript in transcripts:
            if transcript.language_code in codes:
                return transcript
        return None

    if target == "en":
        transcript = find_by_codes(["en", "en-US", "en-GB"])
        if transcript:
            print(f"Using transcript language: {transcript.language_code}")
            return transcript.fetch(), transcript.language_code
    elif target in ("zh-Hans", "zh-Hant"):
        transcript = find_by_codes([target])
        if transcript:
            print(f"Using transcript language: {transcript.language_code}")
            return transcript.fetch(), transcript.language_code

        base = find_by_codes(["zh-Hans", "zh-Hant", "en", "en-US", "en-GB"])
        if base and base.is_translatable:
            translation_targets = [target]
            if target == "zh-Hans":
                translation_targets.append("zh-Hant")
            for translation_target in translation_targets:
                try:
                    print(
                        f"Using YouTube translation: {base.language_code} -> {translation_target}"
                    )
                    return base.translate(translation_target).fetch(), translation_target
                except Exception as error:
                    print(f"YouTube translation to {translation_target} failed: {error}")
            print(
                f"Falling back to {base.language_code}; LLM translation will be used if Chinese output is requested."
            )
            return base.fetch(), base.language_code

    transcript = find_by_codes([target])
    if transcript:
        print(f"Using transcript language: {transcript.language_code}")
        return transcript.fetch(), transcript.language_code

    available = ", ".join(
        f"{transcript.language_code} ({transcript.language})" for transcript in transcripts
    )
    raise RuntimeError(
        f"No usable transcript for language '{language}'. Available: {available}"
    )


def entries_to_text(entries):
    return " ".join(entry.text.replace("\n", " ").strip() for entry in entries if entry.text)


def chunk_text(text, max_chars=8000):
    paragraphs = re.split(r"(?<=[。！？.!?])\s+", text)
    chunks = []
    current = []
    current_len = 0

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        if len(paragraph) > max_chars:
            if current:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
            for start in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[start : start + max_chars])
            continue

        if current and current_len + len(paragraph) > max_chars:
            chunks.append(" ".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += len(paragraph)

    if current:
        chunks.append(" ".join(current))
    return chunks


def looks_chinese(entries):
    sample = entries_to_text(entries[:80])
    if not sample:
        return False
    chinese_chars = sum(1 for char in sample if "\u4e00" <= char <= "\u9fff")
    return chinese_chars >= 20 and chinese_chars / max(len(sample), 1) > 0.15


def split_cleaned_text_into_entries(text, start, end, max_chars=420):
    # Second pass: the model sometimes echoes a caption tag back or answers in
    # markdown, and neither belongs in the book
    normalized = normalize_caption_text(text)
    if not normalized:
        return []

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？.!?])\s*", normalized)
        if sentence.strip()
    ]
    if not sentences:
        sentences = [normalized]

    paragraphs = []
    current = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)
        if current and current_len + sentence_len > max_chars:
            paragraphs.append(" ".join(current))
            current = [sentence]
            current_len = sentence_len
        else:
            current.append(sentence)
            current_len += sentence_len

    if current:
        paragraphs.append(" ".join(current))

    duration = max(0, end - start)
    step = duration / max(len(paragraphs), 1)
    return [
        Entry(paragraph, start + (index * step), step)
        for index, paragraph in enumerate(paragraphs)
    ]


def translate_sections_to_chinese(sections, api_key, target):
    if not api_key:
        raise RuntimeError(
            "Chinese transcript was not available from YouTube, and no API key is set for LLM translation."
        )

    from openai import OpenAI

    target_name = "Simplified Chinese" if target == "zh-Hans" else "Traditional Chinese"
    client = llm_config.client(api_key=api_key)
    translated_sections = []

    for index, section in enumerate(sections, 1):
        if len(section) == 4:
            start, end, entries, title = section
        else:
            start, end, entries = section
            title = None

        source_text = entries_to_text(entries)
        if not source_text:
            translated_entries = entries
        else:
            print(f"LLM translating section {index}/{len(sections)} to {target_name}...")
            response = client.chat.completions.create(
                model=llm_config.model(),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are a professional transcript editor and translator. "
                            f"Translate the transcript into fluent {target_name}. Remove filler words, "
                            "false starts, and repeated verbal tics. Preserve the original meaning, "
                            "names, technical terms, and chapter context. Output only polished reading text."
                        ),
                    },
                    {"role": "user", "content": source_text},
                ],
                temperature=0.3,
            )
            translated_entries = split_cleaned_text_into_entries(
                response.choices[0].message.content.strip(),
                start,
                end,
            )

        if title is not None:
            translated_sections.append((start, end, translated_entries, title))
        else:
            translated_sections.append((start, end, translated_entries))

    return translated_sections


def cleaning_system_prompt(clean_mode):
    if clean_mode == "faithful":
        return (
            "You are a conservative podcast transcript cleaner. Lightly clean the "
            "transcript while preserving the speaker's original wording, sentence "
            "order, tone, and meaning. Do not paraphrase. Do not rewrite sentences "
            "for style. Do not reorganize ideas. Do not add transitions. Do not "
            "combine separate ideas into newly polished prose. Keep informal wording, "
            "imperfect grammar, repeated words, and speaker personality unless they "
            "block understanding. Remove only obvious transcription artifacts, "
            "excessive filler, repeated stutters, and broken false starts. For "
            "Chinese, remove words like 嗯、啊、呃、这个、那个、就是、然后 only when "
            "they are meaningless fillers; keep them when they carry rhythm, "
            "emphasis, hesitation, or natural spoken tone. Preserve names, numbers, "
            "technical terms, examples, jokes, and nuance exactly. Do not add new "
            "ideas. Drop caption artifacts such as '>>' speaker markers and "
            "bracketed sound tags like [Music] or [laughter]. Output plain prose "
            "only: no markdown, no headings, no bullets, no speaker labels."
        )

    return (
        "You are an expert podcast transcript editor. Clean this transcript into "
        "readable prose. Remove filler words, false starts, duplicated phrases, "
        "verbal tics, and excessive laughter markers. For Chinese, remove words "
        "like 嗯、啊、呃、这个、那个、就是、然后 when they are filler. Preserve "
        "meaning, speaker intent, names, technical terms, and important nuance. "
        "Drop caption artifacts such as '>>' speaker markers and bracketed sound "
        "tags like [Music] or [laughter]. Do not add new ideas. Output plain "
        "prose only: no markdown, no headings, no bullets, no speaker labels."
    )


def cleaning_mode_label(clean_mode):
    if clean_mode == "faithful":
        return "Faithful cleaning"
    if clean_mode == "deep":
        return "Deep cleaning"
    return "Fast cleaning"


def fast_clean_sections(sections, api_key, clean_mode="fast"):
    if not api_key:
        raise RuntimeError("AI cleaning requested, but no API key is set.")

    from openai import OpenAI

    client = llm_config.client(api_key=api_key, timeout=120)
    cleaned_sections = []
    section_timings = []
    usage_stats = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0,
        "cost_cny": 0,
    }

    for index, section in enumerate(sections, 1):
        section_started_at = time.perf_counter()
        if len(section) == 4:
            start, end, entries, title = section
        else:
            start, end, entries = section
            title = None

        source_text = entries_to_text(entries)
        if not source_text:
            cleaned_entries = entries
        else:
            chunks = chunk_text(source_text, max_chars=8000)
            cleaned_chunks = []
            chunk_timings = []
            clean_label = cleaning_mode_label(clean_mode)
            print(
                f"{clean_label} section {index}/{len(sections)} in {len(chunks)} chunk(s)...",
                flush=True,
            )

            for chunk_index, chunk in enumerate(chunks, 1):
                chunk_started_at = time.perf_counter()
                print(
                    f"{clean_label} section {index}/{len(sections)}, chunk {chunk_index}/{len(chunks)}...",
                    flush=True,
                )
                response = client.chat.completions.create(
                    model=llm_config.model(),
                    messages=[
                        {
                            "role": "system",
                            "content": cleaning_system_prompt(clean_mode),
                        },
                        {"role": "user", "content": chunk},
                    ],
                    temperature=0.3,
                    max_tokens=4000,
                )
                cleaned_chunks.append(response.choices[0].message.content.strip())
                chunk_timings.append(
                    {
                        "chunk": chunk_index,
                        "seconds": round(time.perf_counter() - chunk_started_at, 2),
                        "chars": len(chunk),
                    }
                )
                if response.usage:
                    usage_stats["input_tokens"] += response.usage.prompt_tokens
                    usage_stats["output_tokens"] += response.usage.completion_tokens
                    usage_stats["total_tokens"] += response.usage.total_tokens

            cleaned_text = "\n\n".join(cleaned_chunks)
            cleaned_entries = split_cleaned_text_into_entries(cleaned_text, start, end)

        if title is not None:
            cleaned_sections.append((start, end, cleaned_entries, title))
        else:
            cleaned_sections.append((start, end, cleaned_entries))

        section_timings.append(
            {
                "section": index,
                "title": title or f"Section {index}",
                "chunks": len(chunk_timings) if source_text else 0,
                "seconds": round(time.perf_counter() - section_started_at, 2),
                "chunk_timings": chunk_timings if source_text else [],
            }
        )

    input_cost = (usage_stats["input_tokens"] / 1_000_000) * 0.15
    output_cost = (usage_stats["output_tokens"] / 1_000_000) * 0.60
    total_cost = input_cost + output_cost
    usage_stats["cost_usd"] = round(total_cost, 4)
    usage_stats["cost_cny"] = round(total_cost * 7.2, 2)
    return cleaned_sections, usage_stats, section_timings


def section_title(index, section):
    if len(section) == 4:
        return section[3]
    start, end, _entries = section
    return f"Section {index}: {format_timestamp(start)}-{format_timestamp(end)}"


def format_timestamp(seconds):
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"


def sections_to_epub_chapters(sections):
    chapters = []
    for index, section in enumerate(sections, 1):
        if len(section) == 4:
            start, end, entries, title = section
            section_title = title
        else:
            start, end, entries = section
            section_title = f"Section {index}: {format_timestamp(start)}-{format_timestamp(end)}"

        html_content = f"<h2>{html.escape(section_title)}</h2>\n"
        for paragraph in entries_to_paragraphs(entries):
            html_content += f"<p class='block'>{html.escape(paragraph)}</p>\n"

        chapter = epub.EpubHtml(
            title=section_title,
            file_name=f"section_{index:02d}.xhtml",
            lang="zh" if looks_chinese(entries) else "en",
        )
        chapter.content = html_content
        chapters.append((section_title, chapter))

    return chapters


def write_clean_txt(title, video_id, sections, output_filename):
    with open(output_filename, "w", encoding="utf-8") as file:
        file.write("=" * 60 + "\n")
        file.write(f"{title}\n")
        file.write("YouTube Transcript\n")
        file.write(f"Video ID: {video_id}\n")
        file.write("=" * 60 + "\n\n")

        for index, section in enumerate(sections, 1):
            if len(section) == 4:
                _start, _end, entries, chapter_title = section
                heading = chapter_title
            else:
                start, end, entries = section
                heading = f"Section {index}: {format_timestamp(start)}-{format_timestamp(end)}"

            file.write(f"\n{'-' * 60}\n")
            file.write(f"{heading}\n")
            file.write(f"{'-' * 60}\n\n")

            for paragraph in entries_to_paragraphs(entries):
                file.write(paragraph + "\n\n")


def translate_text_file(source_file, title, target_language, api_key):
    if not api_key:
        return None

    from openai import OpenAI

    content = Path(source_file).read_text(encoding="utf-8")
    if target_language == "Chinese (Simplified)":
        system_prompt = (
            "You are a professional translator. Translate the following text to fluent "
            "Chinese (Simplified). Keep the meaning accurate and natural to read."
        )
        suffix = "Chinese_Simplified"
    else:
        system_prompt = (
            "You are a professional translator. Translate the following text to fluent "
            "English. Keep the meaning accurate and natural to read."
        )
        suffix = "English"

    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    chunks = []
    current = []
    current_length = 0
    for paragraph in paragraphs:
        if current and current_length + len(paragraph) > 2500:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_length = len(paragraph)
        else:
            current.append(paragraph)
            current_length += len(paragraph)
    if current:
        chunks.append("\n\n".join(current))

    client = llm_config.client(api_key=api_key)
    translated_chunks = []
    for index, chunk in enumerate(chunks, 1):
        print(f"Translating chunk {index}/{len(chunks)}...")
        response = client.chat.completions.create(
            model=llm_config.model(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": chunk},
            ],
            temperature=0.3,
        )
        translated_chunks.append(response.choices[0].message.content.strip())

    safe_title = sanitize_filename(title)
    output_file = f"{safe_title}_{suffix}.txt"
    Path(output_file).write_text(
        f"{title}\nYouTube Transcript ({target_language})\n\n"
        + "\n\n".join(translated_chunks),
        encoding="utf-8",
    )
    return output_file


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--format", choices=["epub", "pdf"], default="epub")
    parser.add_argument("--language", default="English (Original)")
    parser.add_argument("--translate", default="none")
    parser.add_argument("--interval-seconds", type=int, default=1200)
    parser.add_argument("--auto-chapters", action="store_true")
    parser.add_argument("--ai-clean", action="store_true")
    parser.add_argument(
        "--clean-mode",
        choices=["faithful", "fast", "deep"],
        default="faithful",
        help="faithful lightly cleans close to source; fast cleans by section; deep uses the original small-batch cleaner",
    )
    return parser.parse_args()


def main():
    timer = Timer()
    ai_clean_section_timings = []
    load_local_env()
    args = parse_args()
    video_id = extract_video_id(args.url)
    if not video_id:
        raise ValueError("Invalid YouTube URL.")

    print("Fetching YouTube metadata...")
    metadata = timed(
        timer,
        "fetch_metadata_seconds",
        lambda: fetch_youtube_metadata(args.url, video_id),
    )
    title = args.title.strip() or metadata["title"]
    safe_title = sanitize_filename(title)
    cover_path = timed(
        timer,
        "build_cover_seconds",
        lambda: build_cover_image(title, metadata.get("thumbnail"), "cover.jpg"),
    )
    cover_source = "youtube_thumbnail" if cover_path else "pending_generated_fallback"

    print(f"Book title: {title}")
    target_language_code = normalize_language(args.language)
    print(f"Fetching transcript: {args.language}")
    transcript, source_language_code = timed(
        timer,
        "fetch_transcript_seconds",
        lambda: fetch_transcript_smart(video_id, args.language),
    )
    transcript = sanitize_transcript_entries(transcript)
    if looks_chinese(transcript):
        print(
            f"Transcript text appears to be Chinese, despite YouTube language code {source_language_code}."
        )
        source_language_code = "zh-Hans"
    needs_llm_chinese_translation = (
        target_language_code in ("zh-Hans", "zh-Hant")
        and source_language_code not in ("zh-Hans", "zh-Hant")
    )
    if needs_llm_chinese_translation:
        print(
            f"YouTube Chinese transcript unavailable. Will use LLM translation from {source_language_code}."
        )
    print(f"Transcript entries: {len(transcript)}")

    raw_json, raw_txt = timed(
        timer,
        "save_raw_transcript_seconds",
        lambda: save_raw_transcript(transcript, video_id, title),
    )

    chapters = None
    if args.auto_chapters:
        # Already fetched with the title — YouTube's own chapter markers
        chapters = metadata.get("chapters") or None
        if chapters:
            print(f"Found {len(chapters)} YouTube chapters.")
        else:
            print("No YouTube chapters found. Falling back to interval sections.")

    api_key = llm_config.api_key()
    use_cleaner = args.ai_clean and bool(api_key)
    if args.ai_clean and not api_key:
        print("AI cleaning requested, but no API key is set. Using raw transcript.")
    elif use_cleaner:
        print(f"AI {args.clean_mode} cleaning enabled with GPT-4o-mini.")

    if chapters:
        sections, usage_stats = timed(
            timer,
            "build_sections_seconds",
            lambda: group_transcript_by_chapters(
                transcript,
                chapters,
                use_cleaner=use_cleaner and args.clean_mode == "deep",
                api_key=api_key,
            ),
        )
        section_mode = "youtube_chapters"
    else:
        sections, usage_stats = timed(
            timer,
            "build_sections_seconds",
            lambda: group_transcript_by_interval(
                transcript,
                interval_seconds=args.interval_seconds,
                use_cleaner=use_cleaner and args.clean_mode == "deep",
                api_key=api_key,
            ),
        )
        section_mode = "time_intervals"

    if use_cleaner and args.clean_mode in ("faithful", "fast"):
        clean_started_at = time.perf_counter()
        sections, usage_stats, ai_clean_section_timings = fast_clean_sections(
            sections,
            api_key,
            clean_mode=args.clean_mode,
        )
        timer.mark("ai_clean_total_seconds", time.perf_counter() - clean_started_at)
    elif use_cleaner and args.clean_mode == "deep":
        timer.mark("ai_clean_total_seconds", timer.steps.get("build_sections_seconds", 0))

    if needs_llm_chinese_translation:
        sections = timed(
            timer,
            "llm_translate_to_chinese_seconds",
            lambda: translate_sections_to_chinese(
                sections,
                api_key=llm_config.api_key(),
                target=target_language_code,
            ),
        )

    print(f"Sections created: {len(sections)} ({section_mode})")

    if not cover_path:
        print("Thumbnail cover unavailable. Generating fallback ebook cover from title and transcript topics...")
        cover_path = timed(
            timer,
            "build_generated_cover_seconds",
            lambda: build_generated_cover(title, sections, "cover.jpg", api_key=api_key),
        )
        cover_source = "generated_from_title_and_topics" if cover_path else "none"

    txt_file = f"{safe_title}.txt"
    timed(
        timer,
        "write_txt_seconds",
        lambda: write_clean_txt(title, video_id, sections, txt_file),
    )

    if args.format == "pdf":
        output_file = f"{safe_title}.pdf"
        timed(timer, "write_book_seconds", lambda: generate_pdf(title, sections, output_file))
    else:
        output_file = f"{safe_title}.epub"
        def write_epub():
            epub_chapters = sections_to_epub_chapters(sections)
            save_epub_with_cover(title, video_id, epub_chapters, output_file, cover_path)

        timed(timer, "write_book_seconds", write_epub)

    translated_file = None
    if args.translate != "none":
        translated_file = timed(
            timer,
            "translate_output_seconds",
            lambda: translate_text_file(txt_file, title, args.translate, api_key),
        )

    result = {
        "title": title,
        "video_id": video_id,
        "thumbnail_url": metadata.get("thumbnail"),
        "cover_source": cover_source,
        "format": args.format,
        "section_mode": section_mode,
        "sections_count": len(sections),
        "chapters_count": len(chapters or []),
        # Titles as they ended up in the book, not as YouTube wrote them
        "chapter_titles": [
            section[3] for section in sections[:12] if len(section) == 4
        ],
        "ai_clean_requested": args.ai_clean,
        "ai_clean_used": use_cleaner,
        "clean_mode": args.clean_mode if use_cleaner else "off",
        "transcript_source_language": source_language_code,
        "llm_translated_to_chinese": needs_llm_chinese_translation,
        "usage_stats": usage_stats,
        "timing": {
            **timer.steps,
            "ai_clean_sections": ai_clean_section_timings,
            "total_seconds": timer.total(),
        },
        "files": {
            "main": output_file,
            "txt": txt_file,
            "raw_json": raw_json,
            "raw_txt": raw_txt,
            "translated": translated_file,
            "cover": cover_path,
        },
    }
    Path("desktop_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Desktop result saved: desktop_result.json")


if __name__ == "__main__":
    main()
