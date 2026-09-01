import sys
import re
import json
import subprocess
import urllib.parse
import urllib.request
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from ebooklib import epub
import argparse
import os

# Try to load .env file for API keys
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, will use environment variables directly
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
import llm_config
from transcript_cleaner import TranscriptCleaner

# Try to import playwright for better transcript fetching
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False



# Try to import pytube for fetching video title
try:
    from pytube import YouTube
except ImportError:
    YouTube = None

def extract_video_id(url):
    """Extract the video ID from a YouTube URL."""
    regex = r"(?:v=|youtu\.be/|embed/|v/|shorts/)([\w-]{11})"
    match = re.search(regex, url)
    if match:
        return match.group(1)
    else:
        return None

def fetch_video_title(url):
    """Real title from YouTube's oembed endpoint, with fallbacks.

    pytube is tried second because it breaks whenever YouTube changes its
    player; oembed is a documented endpoint and needs no dependency.
    """
    video_id = extract_video_id(url)
    if video_id:
        oembed = (
            "https://www.youtube.com/oembed?url="
            + urllib.parse.quote(
                f"https://www.youtube.com/watch?v={video_id}", safe=""
            )
            + "&format=json"
        )
        try:
            with urllib.request.urlopen(oembed, timeout=10) as response:
                title = json.loads(response.read().decode("utf-8")).get("title")
            if title:
                return title
        except Exception:
            pass

    if YouTube:
        try:
            title = YouTube(url).title
            if title:
                return title
        except Exception as e:
            print(f"Could not fetch video title automatically: {e}")

    return f"YouTube Transcript {video_id}" if video_id else "YouTube Transcript"


def sanitize_filename(name):
    # Remove or replace characters not allowed in filenames
    return re.sub(r'[\\/*?\:"<>|]', '', name)


def fetch_video_metadata(url, video_id=None):
    """Title, thumbnail, duration and chapters in one yt-dlp call.

    yt-dlp already parses YouTube's own chapter markers, so there is no reason
    to open a browser and scrape the description for them. oembed backs up the
    title because it keeps working when yt-dlp is missing or gets bot-checked.
    """
    video_id = video_id or extract_video_id(url)
    metadata = {
        "title": None,
        "thumbnail": None,
        "duration": None,
        "chapters": [],
        "description": "",
        "source": None,
    }

    data = None
    for command in (
        [sys.executable, "-m", "yt_dlp"],
        ["yt-dlp"],
    ):
        try:
            result = subprocess.run(
                command + ["--dump-single-json", "--skip-download", "--no-playlist", url],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            data = json.loads(result.stdout)
            break
        except FileNotFoundError:
            continue
        except Exception as error:
            print(f"yt-dlp metadata lookup failed: {error}")
            break

    if data:
        metadata["source"] = "yt-dlp"
        metadata["title"] = data.get("title") or None
        metadata["duration"] = data.get("duration")
        metadata["description"] = data.get("description") or ""
        metadata["thumbnail"] = data.get("thumbnail")
        thumbnails = data.get("thumbnails") or []
        if thumbnails:
            best = sorted(
                thumbnails,
                key=lambda item: (item.get("width") or 0) * (item.get("height") or 0),
            )[-1]
            metadata["thumbnail"] = best.get("url") or metadata["thumbnail"]
        metadata["chapters"] = chapters_from_ytdlp(data.get("chapters"))
        if not metadata["chapters"]:
            metadata["chapters"] = parse_chapters_from_description(metadata["description"])

    if not metadata["title"]:
        metadata["title"] = fetch_video_title(url)
        metadata["source"] = metadata["source"] or "oembed"
    if not metadata["thumbnail"] and video_id:
        metadata["thumbnail"] = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

    return metadata


PLACEHOLDER_CHAPTER_TITLE = re.compile(r"^<untitled chapter\s*\d*>$", re.IGNORECASE)


def chapters_from_ytdlp(raw_chapters):
    """Normalise yt-dlp's chapter list into {'title', 'timestamp'} dicts."""
    chapters = []
    for chapter in raw_chapters or []:
        start = chapter.get("start_time")
        if start is None:
            continue
        title = (chapter.get("title") or "").strip()
        if PLACEHOLDER_CHAPTER_TITLE.match(title):
            title = ""
        chapters.append({"title": title, "timestamp": int(start)})
    return chapters


TIMESTAMP_PATTERN = r"(\d{1,3}:\d{2}(?::\d{2})?)"
CHAPTER_LINE_LEADING = re.compile(
    r"^[\s\-–—•*#>]*[\(\[]?" + TIMESTAMP_PATTERN + r"[\)\]]?\s*[\-–—:|.)]*\s*(.+)$"
)
CHAPTER_LINE_TRAILING = re.compile(
    r"^(.+?)\s*[\-–—:|]?\s*[\(\[]?" + TIMESTAMP_PATTERN + r"[\)\]]?[\s\-–—]*$"
)


def timestamp_to_seconds(timestamp):
    parts = timestamp.split(":")
    try:
        parts = [int(part) for part in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def parse_chapters_from_description(description):
    """Timestamp lines in a description, for videos YouTube itself did not chapter.

    Handles the common shapes: "0:00 Intro", "0:00 - Intro", "(0:00) Intro",
    and — only when no leading-timestamp lines exist at all — "Intro 0:00".
    """
    if not description:
        return []

    leading = []
    trailing = []
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = CHAPTER_LINE_LEADING.match(line)
        if match:
            seconds = timestamp_to_seconds(match.group(1))
            title = match.group(2).strip(" -–—:|")
            if seconds is not None and title:
                leading.append({"title": title, "timestamp": seconds})
                continue

        match = CHAPTER_LINE_TRAILING.match(line)
        if match:
            seconds = timestamp_to_seconds(match.group(2))
            title = match.group(1).strip(" -–—:|")
            if seconds is not None and title:
                trailing.append({"title": title, "timestamp": seconds})

    chapters = leading if len(leading) >= 2 else trailing
    return chapters if len(chapters) >= 2 else []


def normalize_chapters(chapters, total_duration=None, min_chapter_seconds=60):
    """Sort, dedupe, cover the opening, and merge away micro-chapters.

    A marker less than min_chapter_seconds after the previous one is worth about
    a paragraph, so it is folded into it rather than becoming its own chapter.
    The threshold is deliberately low: creator-written chapters are usually good
    and should survive intact.
    """
    if not chapters:
        return []

    by_timestamp = {}
    for chapter in chapters:
        timestamp = max(0, int(chapter.get("timestamp") or 0))
        title = (chapter.get("title") or "").strip()
        if timestamp not in by_timestamp or (title and not by_timestamp[timestamp]):
            by_timestamp[timestamp] = title

    ordered = [
        {"timestamp": timestamp, "title": by_timestamp[timestamp]}
        for timestamp in sorted(by_timestamp)
        if total_duration is None or timestamp < total_duration
    ]
    if not ordered:
        return []

    if ordered[0]["timestamp"] > 5:
        ordered.insert(0, {"timestamp": 0, "title": ""})

    kept = [ordered[0]]
    for chapter in ordered[1:]:
        if chapter["timestamp"] - kept[-1]["timestamp"] < min_chapter_seconds:
            continue
        kept.append(chapter)

    if len(kept) < 2:
        return []

    for index, chapter in enumerate(kept):
        if not chapter["title"]:
            chapter["title"] = "Introduction" if index == 0 else f"Chapter {index + 1}"
    return kept


def fetch_transcript_with_playwright(video_id):
    """Fetch transcript using Playwright to bypass IP blocks."""
    if not PLAYWRIGHT_AVAILABLE:
        raise Exception("Playwright not available. Please install it with: pip install playwright")
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            # Navigate to YouTube video
            url = f"https://www.youtube.com/watch?v={video_id}"
            page.goto(url)
            
            # Wait for page to load
            page.wait_for_load_state("networkidle")
            
            # Try to find and click the "Show transcript" button
            try:
                # Look for transcript button with multiple selectors
                transcript_button_selectors = [
                    'button[aria-label*="transcript"]',
                    'button[aria-label*="Transcript"]',
                    'button[aria-label*="字幕"]',
                    'button[aria-label*="字幕"]',
                    '[data-testid="transcript-button"]',
                    'button:has-text("Show transcript")',
                    'button:has-text("字幕")'
                ]
                
                transcript_button = None
                for selector in transcript_button_selectors:
                    try:
                        button = page.locator(selector)
                        if button.count() > 0:
                            transcript_button = button.first
                            break
                    except:
                        continue
                
                if transcript_button:
                    transcript_button.click()
                    page.wait_for_timeout(3000)  # Wait for transcript to load
                
                # Extract transcript text with multiple selectors
                transcript_selectors = [
                    '[data-testid="transcript-segment"]',
                    '.ytd-transcript-segment-renderer',
                    '.ytd-transcript-segment',
                    '[data-testid="transcript-text"]'
                ]
                
                transcript_elements = None
                for selector in transcript_selectors:
                    try:
                        elements = page.locator(selector)
                        if elements.count() > 0:
                            transcript_elements = elements
                            break
                    except:
                        continue
                
                if not transcript_elements or transcript_elements.count() == 0:
                    # Try to find any text that looks like a transcript
                    page_text = page.text_content('body')
                    if 'transcript' in page_text.lower() or '字幕' in page_text:
                        raise Exception("Transcript button found but transcript content not accessible")
                    else:
                        raise Exception("No transcript found on page")
                
                transcript = []
                for i in range(transcript_elements.count()):
                    element = transcript_elements.nth(i)
                    text = element.text_content()
                    if text and text.strip():
                        # Parse timestamp and text
                        parts = text.split('\n')
                        if len(parts) >= 2:
                            timestamp = parts[0].strip()
                            content = ' '.join(parts[1:]).strip()
                            
                            # Skip if no content
                            if not content:
                                continue
                                
                            # Convert timestamp to seconds
                            try:
                                time_parts = timestamp.split(':')
                                if len(time_parts) == 2:
                                    seconds = int(time_parts[0]) * 60 + int(time_parts[1])
                                elif len(time_parts) == 3:
                                    seconds = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
                                else:
                                    seconds = 0
                            except:
                                seconds = 0
                            
                            transcript.append({
                                'start': seconds,
                                'text': content
                            })
                
                browser.close()
                if not transcript:
                    raise Exception("No transcript content could be extracted")
                return transcript
            except Exception as e:
                browser.close()
                raise Exception(f"Failed to extract transcript: {e}")
                
    except Exception as e:
        raise Exception(f"Playwright error: {e}")

def fetch_transcript(video_id, language='en'):
    """Try API first, then fallback to Playwright if needed. Supports translated transcripts.

    Args:
        video_id: YouTube video ID
        language: Language code ('en', 'zh-Hans', 'zh-Hant', etc.)
    """
    # Language mapping
    language_map = {
        'English (Original)': 'en',
        '中文简体 (Chinese Simplified)': 'zh-Hans',
        '中文繁體 (Chinese Traditional)': 'zh-Hant'
    }

    # If language is already a code, use it directly
    if language in language_map:
        target_lang = language_map[language]
    else:
        target_lang = language

    try:
        # Get the transcript list to find available transcripts
        transcript_list = YouTubeTranscriptApi().list(video_id)

        # Try to find transcript in the requested language
        try:
            if target_lang == 'en':
                # For English, try to get original transcript
                transcript = transcript_list.find_transcript(['en'])
                return transcript.fetch()
            else:
                # For other languages, try to get English transcript and translate
                en_transcript = transcript_list.find_transcript(['en'])
                translated_transcript = en_transcript.translate(target_lang)
                print(f"Fetched {target_lang} translated transcript")
                return translated_transcript.fetch()
        except Exception as translate_error:
            print(f"Translation failed for {target_lang}: {translate_error}")
            # Fallback: try to fetch directly in target language
            try:
                transcript = YouTubeTranscriptApi().fetch(video_id, languages=[target_lang])
                return transcript
            except:
                # Final fallback: use English transcript and let AI handle translation/cleaning
                print(f"Primary method failed for {target_lang}, falling back to English transcript")
                en_transcript = transcript_list.find_transcript(['en'])
                return en_transcript.fetch()

    except Exception as api_error:
        # If API fails, try Playwright as fallback (only for English)
        if target_lang == 'en' and PLAYWRIGHT_AVAILABLE:
            try:
                transcript = fetch_transcript_with_playwright(video_id)
                return transcript
            except Exception as playwright_error:
                return _handle_transcript_error(api_error, playwright_error)
        else:
            return _handle_transcript_error(api_error, None)

def _handle_transcript_error(api_error, playwright_error=None):
    """Handle transcript fetch errors with user-friendly messages"""
    error_str = str(api_error) if playwright_error is None else f"{api_error}. Playwright failed: {playwright_error}"

    if "Subtitles are disabled" in error_str or "Transcripts are disabled" in error_str:
        raise Exception("⚠️ **Subtitles Disabled**\n\nThis YouTube video has subtitles disabled by the creator. Unfortunately, we cannot generate a transcript for this video.\n\n**Try a different video** that has subtitles enabled, or contact the video creator to enable subtitles.")
    elif "No transcript found" in error_str:
        raise Exception("⚠️ **No Transcript Available**\n\nThis YouTube video doesn't have any transcript available. This could be because:\n\n• The creator hasn't enabled subtitles\n• The video is very old and never had transcripts generated\n• There are copyright restrictions\n\n**Try a different video** that has subtitles enabled.")
    elif "YouTube is blocking requests" in error_str or "IP has been blocked" in error_str or "IpBlocked" in error_str:
        raise Exception("🚫 **YouTube Blocking Issue**\n\nThis is a common issue on cloud platforms. Here are some solutions:\n\n**Option 1: Try a different video**\n• Some videos work better than others\n• Try videos with popular creators\n\n**Option 2: Wait a few minutes**\n• Sometimes the issue resolves itself\n• Try again in 5-10 minutes\n\n**Option 3: Use Local Deployment**\n• Run the app locally on your computer\n• Uses your IP instead of cloud IP\n• Usually works better than cloud deployment\n\n**Technical Note**: YouTube blocks requests from cloud provider IPs. This is a limitation of the YouTube API.")
    else:
        raise Exception(f"Error fetching transcript: {error_str}")

# Keep the old fetch_transcript function for backward compatibility
def fetch_transcript_legacy(video_id):
    """Legacy function - use fetch_transcript(video_id, language) instead."""
    return fetch_transcript(video_id, 'en')

def format_timestamp(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"


# YouTube's auto-captions carry markup that means nothing in a book: ">>" for a
# speaker change, "[Music]" / "[laughter]" for sounds, "[ __ ]" for a bleeped
# word. The LLM pass does not reliably strip these, so do it deterministically —
# it then works with no API key at all.
CAPTION_SPEAKER_MARKER = re.compile(r"(?:^|\s)>{2,}\s*")
CAPTION_SOUND_TAG = re.compile(r"\[\s*([^\[\]\n]{0,30}?)\s*\]")
CAPTION_KNOWN_TAGS = {
    "music", "applause", "laughter", "laughs", "laughing", "laugh", "chuckles",
    "chuckling", "sighs", "sighing", "snorts", "coughs", "coughing", "gasps",
    "clears throat", "throat clearing", "inaudible", "unintelligible",
    "crosstalk", "silence", "noise", "cheering", "clapping", "singing",
    "sniffs", "exhales", "inhales", "beep", "foreign",
}
# Only ever strip the tag vocabulary auto-captions actually emit — a bracket
# holding real speech, like "[revenue minus cost]", has to survive
# Only paired emphasis, so censored profanity like "f***" is left alone
MARKDOWN_EMPHASIS = re.compile(r"\*\*(\S.*?\S|\S)\*\*|`([^`\n]+)`")
MARKDOWN_LEFTOVERS = re.compile(r"^\s*(?:#{1,6}\s+|[-*]\s+)", re.MULTILINE)


def _drop_sound_tag(match):
    inner = re.sub(r"\s+", " ", match.group(1)).strip()
    if not inner or set(inner) <= {"_"}:
        return " "
    normalized = inner.lower()
    if normalized in CAPTION_KNOWN_TAGS:
        return " "
    # "[inaudible 01:12]" and friends
    if re.fullmatch(r"(?:%s)[\s\d:]*" % "|".join(CAPTION_KNOWN_TAGS), normalized):
        return " "
    # Compound tags: "[sighs and gasps]", "[music, applause]"
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:,|&|\+|\band\b)\s*", normalized)
        if part.strip()
    ]
    if len(parts) > 1 and all(part in CAPTION_KNOWN_TAGS for part in parts):
        return " "
    return match.group(0)


def normalize_caption_text(text):
    """Strip caption markup and stray markdown from transcript text."""
    if not text:
        return ""
    text = MARKDOWN_LEFTOVERS.sub(" ", text)
    text = MARKDOWN_EMPHASIS.sub(lambda m: m.group(1) or m.group(2), text)
    text = text.replace("\n", " ")
    text = CAPTION_SPEAKER_MARKER.sub(" ", text)
    text = CAPTION_SOUND_TAG.sub(_drop_sound_tag, text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:，。！？])", r"\1", text)
    return text.strip()


def sanitize_transcript_entries(entries):
    """Apply normalize_caption_text across a transcript, dropping emptied entries."""
    cleaned = []
    for entry in entries:
        text = normalize_caption_text(entry.text)
        if not text:
            continue
        entry.text = text
        cleaned.append(entry)
    return cleaned

def clean_transcript_entries(entries, use_cleaner=False, api_key=None, progress_callback=None):
    """
    Clean transcript entries using LLM if enabled

    Args:
        entries: List of transcript entries
        use_cleaner: Whether to use LLM cleaner
        api_key: OpenAI API key
        progress_callback: Optional callback for progress updates

    Returns:
        Tuple of (cleaned entries, usage stats dict or None)
    """
    if not use_cleaner:
        return entries, None

    try:
        print("Using LLM to clean transcript...")
        cleaner = TranscriptCleaner(api_key=api_key)
        cleaned_entries = cleaner.clean_transcript_entries(entries, batch_size=30, progress_callback=progress_callback)
        print(f"Cleaned {len(entries)} entries -> {len(cleaned_entries)} entries")

        # Get usage statistics
        usage_stats = cleaner.get_usage_stats()
        print(cleaner.get_usage_summary())

        return cleaned_entries, usage_stats
    except Exception as e:
        print(f"Warning: LLM cleaning failed ({e}), using original transcript")
        return entries, None

def accumulate_usage(total_usage_stats, usage_stats):
    if not usage_stats:
        return
    total_usage_stats["input_tokens"] += usage_stats["input_tokens"]
    total_usage_stats["output_tokens"] += usage_stats["output_tokens"]
    total_usage_stats["total_tokens"] += usage_stats["total_tokens"]
    total_usage_stats["cost_usd"] += usage_stats["cost_usd"]
    total_usage_stats["cost_cny"] += usage_stats["cost_cny"]
    total_usage_stats["cost_known"] &= usage_stats.get("cost_known", True)


def empty_usage_stats():
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0,
        "cost_cny": 0,
        "cost_known": True,
    }


SENTENCE_ENDINGS = (".", "!", "?", "。", "！", "？", "…")


def split_at_sentence_end(entries, max_lookback=5):
    """Split entries into (kept, carry) at the last sentence end near the tail.

    The carry belongs to the next section — dropping it, as this used to, quietly
    deleted a few lines of transcript at every single section boundary.
    """
    if not entries:
        return entries, []

    for index in range(len(entries) - 1, max(-1, len(entries) - 1 - max_lookback), -1):
        if entries[index].text.strip().endswith(SENTENCE_ENDINGS):
            return entries[: index + 1], entries[index + 1 :]

    return entries, []


def group_transcript_by_interval(transcript, interval_seconds=2400, use_cleaner=False, api_key=None, progress_callback=None):
    """Group transcript entries into sections of interval_seconds (default 40 min - double the size).
    Returns: (sections, usage_stats) tuple"""
    sections = []
    current_section = []
    current_start = 0
    total_usage_stats = empty_usage_stats()

    for entry in transcript:
        if entry.start >= current_start + interval_seconds and current_section:
            # Break at a sentence end and hand the remainder to the next section
            kept, carry = split_at_sentence_end(current_section)
            cleaned_section, usage_stats = clean_transcript_entries(kept, use_cleaner, api_key, progress_callback)
            accumulate_usage(total_usage_stats, usage_stats)
            sections.append((current_start, current_start + interval_seconds, cleaned_section))
            current_start += interval_seconds
            current_section = list(carry)
        current_section.append(entry)

    if current_section:
        cleaned_section, usage_stats = clean_transcript_entries(current_section, use_cleaner, api_key, progress_callback)
        accumulate_usage(total_usage_stats, usage_stats)
        sections.append((current_start, current_start + interval_seconds, cleaned_section))

    return sections, total_usage_stats if use_cleaner else None


def group_transcript_by_chapters(transcript, chapters, use_cleaner=False, api_key=None, progress_callback=None):
    """Group transcript entries by YouTube chapters.

    Args:
        transcript: List of transcript entries with start, duration, text
        chapters: List of dicts with 'title' and 'timestamp' keys
        use_cleaner: Whether to use AI cleaning
        api_key: OpenAI API key for cleaning
        progress_callback: Optional callback for progress updates

    Returns:
        (sections, usage_stats) tuple where sections is list of (start, end, text, title) tuples
    """
    total_duration = None
    if transcript:
        total_duration = transcript[-1].start + transcript[-1].duration

    sorted_chapters = normalize_chapters(chapters, total_duration=total_duration)
    if len(sorted_chapters) < 2:
        # Fallback to interval-based grouping if no usable chapters
        return group_transcript_by_interval(transcript, interval_seconds=2400, use_cleaner=use_cleaner, api_key=api_key, progress_callback=progress_callback)

    sections = []
    total_usage_stats = empty_usage_stats()

    # Bucket entries by chapter time range first, so nothing falls between chapters
    buckets = [[] for _ in sorted_chapters]
    boundaries = [chapter["timestamp"] for chapter in sorted_chapters]
    for entry in transcript:
        index = 0
        for candidate in range(len(boundaries) - 1, -1, -1):
            if entry.start >= boundaries[candidate]:
                index = candidate
                break
        buckets[index].append(entry)

    # A chapter marker often lands mid-sentence; move the dangling tail forward
    for index in range(len(buckets) - 1):
        kept, carry = split_at_sentence_end(buckets[index])
        buckets[index] = kept
        buckets[index + 1] = list(carry) + buckets[index + 1]

    for index, chapter_entries in enumerate(buckets):
        if not chapter_entries:
            continue

        start_time = chapter_entries[0].start
        if index + 1 < len(sorted_chapters):
            end_time = sorted_chapters[index + 1]["timestamp"]
        else:
            end_time = total_duration if total_duration else start_time + 3600

        cleaned_section, usage_stats = clean_transcript_entries(chapter_entries, use_cleaner, api_key, progress_callback)
        accumulate_usage(total_usage_stats, usage_stats)

        # Append section with title: (start, end, text, title)
        sections.append((start_time, end_time, cleaned_section, sorted_chapters[index]["title"]))

    return sections, total_usage_stats if use_cleaner else None


def create_title_page(title, video_id):
    html = f"""
    <html><head></head><body>
    <h1 style='text-align:center;margin-top:2em;font-size:2.5em;'>{title}</h1>
    <h3 style='text-align:center;margin-top:1em;'>YouTube Transcript</h3>
    <p style='text-align:center;margin-top:2em;'>Video ID: {video_id}</p>
    </body></html>
    """
    title_page = epub.EpubHtml(title='Title Page', file_name='title.xhtml', lang='en')
    title_page.content = html
    return title_page


def transcript_sections_to_epub_chapters(sections):
    chapters = []
    for idx, section in enumerate(sections, 1):
        # Handle both 3-tuple (start, end, entries) and 4-tuple (start, end, entries, title)
        if len(section) == 4:
            start, end, entries, title = section
            section_title = title  # Use YouTube chapter title
        else:
            start, end, entries = section
            section_title = f"Section {idx}: {format_timestamp(start)}–{format_timestamp(end)}"

        html = f"<h2>{section_title}</h2>\n"
        # Check if this video has punctuation (sample first 50 entries)
        has_punctuation = False
        for entry in entries[:50]:
            if entry.text.strip().endswith(('.', '!', '?')):
                has_punctuation = True
                break
        
        # Group transcript into paragraphs based on punctuation availability
        paragraph = []
        for i, entry in enumerate(entries):
            paragraph.append(entry.text.replace('\n', ' '))
            
            if has_punctuation:
                # Use original logic: end paragraph at sentence boundaries
                current_text = entry.text.strip()
                if current_text.endswith(('.', '!', '?')):
                    # Create paragraph text
                    paragraph_text = " ".join(paragraph)
                    html += f"<p class='block'>{paragraph_text}</p>\n"
                    paragraph = []
            else:
                # Use new logic: end paragraph every 6 entries if no sentence ending
                current_text = entry.text.strip()
                if current_text.endswith(('.', '!', '?')):
                    # Create paragraph text
                    paragraph_text = " ".join(paragraph)
                    html += f"<p class='block'>{paragraph_text}</p>\n"
                    paragraph = []
                elif len(paragraph) >= 6:  # Create paragraph every 6 entries if no sentence ending
                    # Create paragraph text
                    paragraph_text = " ".join(paragraph)
                    html += f"<p class='block'>{paragraph_text}</p>\n"
                    paragraph = []
            
            if i == len(entries) - 1:
                # End of entries, add remaining content
                paragraph_text = " ".join(paragraph)
                html += f"<p class='block'>{paragraph_text}</p>\n"
        chapter = epub.EpubHtml(title=section_title, file_name=f'section_{idx:02d}.xhtml', lang='en')
        chapter.content = html
        chapters.append((section_title, chapter))
    return chapters

def create_custom_css():
    css = '''
    body { font-family: serif; font-size: 1.2em; line-height: 1.7; margin: 2em; }
    h1, h2 { text-align: center; }
    p.block { margin-bottom: 1.2em; }
    '''
    style = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=css)
    return style

def save_epub(title, video_id, chapters, output_filename):
    book = epub.EpubBook()
    book.set_identifier(title)
    book.set_title(title)
    book.set_language('en')
    book.add_author('YouTube Transcript Script')

    # Add custom CSS
    style = create_custom_css()
    book.add_item(style)

    # Add title page
    title_page = create_title_page(title, video_id)
    book.add_item(title_page)

    # Add chapters and build TOC
    toc = [epub.Link('title.xhtml', 'Title Page', 'title_page')]
    spine = ['nav', title_page]
    for section_title, chapter in chapters:
        chapter.add_item(style)
        book.add_item(chapter)
        toc.append(epub.Link(chapter.file_name, section_title, section_title.replace(' ', '_')))
        spine.append(chapter)
    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(output_filename, book)
    print(f"Saved transcript as {output_filename}")

def generate_pdf(title, sections, output_filename):
    """Generate a PDF file from transcript sections with Chinese font support."""
    doc = SimpleDocTemplate(output_filename, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    
    # Use UnicodeCIDFont which supports Chinese characters
    try:
        # Register Unicode font that supports Chinese
        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
        chinese_font = 'STSong-Light'
    except:
        try:
            # Try alternative Chinese fonts
            pdfmetrics.registerFont(UnicodeCIDFont('HeiseiMin-W3'))
            chinese_font = 'HeiseiMin-W3'
        except:
            try:
                # Try system fonts as fallback
                pdfmetrics.registerFont(TTFont('ArialUnicode', '/System/Library/Fonts/Arial Unicode MS.ttf'))
                chinese_font = 'ArialUnicode'
            except:
                try:
                    pdfmetrics.registerFont(TTFont('PingFang', '/System/Library/Fonts/PingFang.ttc'))
                    chinese_font = 'PingFang'
                except:
                    chinese_font = 'Helvetica'
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=25,
        spaceBefore=20,
        alignment=1,  # Center alignment
        fontName=chinese_font
    )
    story.append(Paragraph(title, title_style))
    story.append(Spacer(1, 15))
    
    # Add subtitle
    subtitle_style = ParagraphStyle(
        'CustomSubtitle',
        parent=styles['Heading2'],
        fontSize=13,
        spaceAfter=30,
        alignment=1,
        fontName=chinese_font
    )
    story.append(Paragraph("YouTube Transcript", subtitle_style))
    story.append(Spacer(1, 35))
    
    # Content
    for idx, section in enumerate(sections, 1):
        # Handle both 3-tuple (start, end, entries) and 4-tuple (start, end, entries, title)
        if len(section) == 4:
            start, end, entries, title = section
            section_title = title  # Use YouTube chapter title
        else:
            start, end, entries = section
            section_title = f"Section {idx}: {format_timestamp(start)}–{format_timestamp(end)}"

        # Section header
        header_style = ParagraphStyle(
            'SectionHeader',
            parent=styles['Heading2'],
            fontName=chinese_font,
            fontSize=16,
            spaceAfter=20,
            spaceBefore=30
        )
        story.append(Paragraph(section_title, header_style))
        
        # Check if this video has punctuation (sample first 50 entries)
        has_punctuation = False
        for entry in entries[:50]:
            if entry.text.strip().endswith(('.', '!', '?')):
                has_punctuation = True
                break
        
        # Group transcript into paragraphs based on punctuation availability
        paragraph = []
        for i, entry in enumerate(entries):
            paragraph.append(entry.text.replace('\n', ' '))
            
            if has_punctuation:
                # Use original logic: end paragraph at sentence boundaries
                current_text = entry.text.strip()
                if current_text.endswith(('.', '!', '?')):
                    # Create paragraph text
                    paragraph_text = " ".join(paragraph)
                    
                    # Add paragraph with better formatting
                    text_style = ParagraphStyle(
                        'NormalText',
                        parent=styles['Normal'],
                        fontName=chinese_font,
                        fontSize=13,
                        leading=18,
                        spaceAfter=12,
                        firstLineIndent=0,  # No indent first line
                        leftIndent=0,
                        rightIndent=0
                    )
                    story.append(Paragraph(paragraph_text, text_style))
                    story.append(Spacer(1, 8))  # Space between paragraphs
                    paragraph = []
            else:
                # Use new logic: end paragraph every 6 entries if no sentence ending
                current_text = entry.text.strip()
                if current_text.endswith(('.', '!', '?')):
                    # Create paragraph text
                    paragraph_text = " ".join(paragraph)
                    
                    # Add paragraph with better formatting
                    text_style = ParagraphStyle(
                        'NormalText',
                        parent=styles['Normal'],
                        fontName=chinese_font,
                        fontSize=13,
                        leading=18,
                        spaceAfter=12,
                        firstLineIndent=0,  # No indent first line
                        leftIndent=0,
                        rightIndent=0
                    )
                    story.append(Paragraph(paragraph_text, text_style))
                    story.append(Spacer(1, 8))  # Space between paragraphs
                    paragraph = []
                elif len(paragraph) >= 6:  # Create paragraph every 6 entries if no sentence ending
                    # Create paragraph text
                    paragraph_text = " ".join(paragraph)
                    
                    # Add paragraph with better formatting
                    text_style = ParagraphStyle(
                        'NormalText',
                        parent=styles['Normal'],
                        fontName=chinese_font,
                        fontSize=13,
                        leading=18,
                        spaceAfter=12,
                        firstLineIndent=0,  # No indent first line
                        leftIndent=0,
                        rightIndent=0
                    )
                    story.append(Paragraph(paragraph_text, text_style))
                    story.append(Spacer(1, 8))  # Space between paragraphs
                    paragraph = []
        
        story.append(Spacer(1, 25))  # Extra space after each section
    
    doc.build(story)
    print(f"Saved PDF as {output_filename} using font: {chinese_font}")

def save_raw_transcript(transcript, video_id, title=""):
    """Save raw transcript data to JSON and TXT formats"""
    import json

    # Convert to list of dicts for JSON serialization
    transcript_data = []
    for entry in transcript:
        transcript_data.append({
            'start': entry.start,
            'text': entry.text,
            'duration': entry.duration if hasattr(entry, 'duration') else 0
        })

    # Save as JSON (with all metadata)
    json_filename = f"{video_id}_transcript.json"
    with open(json_filename, 'w', encoding='utf-8') as f:
        json.dump(transcript_data, f, ensure_ascii=False, indent=2)
    print(f"✅ Saved raw transcript to {json_filename}")

    # Save as TXT (human-readable)
    txt_filename = f"{video_id}_transcript.txt"
    with open(txt_filename, 'w', encoding='utf-8') as f:
        f.write(f"YouTube Transcript: {title}\n")
        f.write(f"Video ID: {video_id}\n")
        f.write(f"Total entries: {len(transcript_data)}\n")
        f.write("="*50 + "\n\n")

        for entry in transcript_data:
            timestamp = format_timestamp(entry['start'])
            f.write(f"[{timestamp}] {entry['text']}\n")
    print(f"✅ Saved readable transcript to {txt_filename}")

    return json_filename, txt_filename

def save_txt(transcript, title, video_id, sections=None, output_filename=None):
    """Generate a well-formatted TXT file from transcript"""
    if not output_filename:
        safe_title = sanitize_filename(title)
        output_filename = f"{safe_title}.txt"

    with open(output_filename, 'w', encoding='utf-8') as f:
        # Header
        f.write("="*60 + "\n")
        f.write(f"{title}\n")
        f.write(f"YouTube Transcript\n")
        f.write(f"Video ID: {video_id}\n")
        f.write("="*60 + "\n\n")

        # Content by sections if provided
        if sections:
            for idx, section in enumerate(sections, 1):
                # Chapter sections carry a title; interval sections do not
                if len(section) == 4:
                    start, end, entries, section_title = section
                else:
                    start, end, entries = section
                    section_title = f"Section {idx}: {format_timestamp(start)}–{format_timestamp(end)}"
                f.write(f"\n{'─'*60}\n")
                f.write(f"{section_title}\n")
                f.write(f"{'─'*60}\n\n")

                # Group transcript into paragraphs
                paragraph = []
                for i, entry in enumerate(entries):
                    paragraph.append(entry.text)

                    # End paragraph at sentence boundaries
                    current_text = entry.text.strip()
                    if current_text.endswith(('.', '!', '?')):
                        para_text = " ".join(paragraph)
                        f.write(f"{para_text}\n\n")
                        paragraph = []
                    elif len(paragraph) >= 6:
                        para_text = " ".join(paragraph)
                        f.write(f"{para_text}\n\n")
                        paragraph = []

                # Add remaining content
                if paragraph:
                    para_text = " ".join(paragraph)
                    f.write(f"{para_text}\n\n")

                f.write("\n")
        else:
            # Raw transcript without sections
            for entry in transcript:
                timestamp = format_timestamp(entry.start)
                f.write(f"[{timestamp}] {entry.text}\n")

    print(f"✅ Saved TXT to {output_filename}")
    return output_filename

def main():
    parser = argparse.ArgumentParser(
        prog="python runtime/main.py",
        description="Turn a YouTube video into an EPUB you can read.",
    )
    parser.add_argument("url", help="YouTube URL")
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="skip the LLM pass - no API key needed, but keeps the ums",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=20,
        metavar="MINUTES",
        help="minutes of transcript per chapter when the video has no chapters (default: 20)",
    )
    parser.add_argument(
        "--no-auto-chapters",
        action="store_true",
        help="ignore the video's own chapter markers and use fixed intervals",
    )
    args = parser.parse_args()

    video_id = extract_video_id(args.url)
    if not video_id:
        print("Invalid YouTube URL.")
        sys.exit(1)

    metadata = fetch_video_metadata(args.url, video_id)
    video_title = metadata["title"]
    safe_title = sanitize_filename(video_title)
    transcript = sanitize_transcript_entries(fetch_transcript(video_id))
    save_raw_transcript(transcript, video_id, video_title)

    api_key = llm_config.api_key()
    clean = not args.no_clean
    if clean and not api_key:
        print("No API key set - writing the raw transcript. "
              "Set LLM_API_KEY to clean it, or pass --no-clean to silence this.")
        clean = False
    if clean:
        print(f"🤖 Cleaning with {llm_config.provider_label()}...")

    if metadata["chapters"] and not args.no_auto_chapters:
        print(f"Using {len(metadata['chapters'])} YouTube chapters.")
        sections, usage_stats = group_transcript_by_chapters(
            transcript,
            metadata["chapters"],
            use_cleaner=clean,
            api_key=api_key,
        )
    else:
        sections, usage_stats = group_transcript_by_interval(
            transcript,
            interval_seconds=max(1, args.interval) * 60,
            use_cleaner=clean,
            api_key=api_key,
        )

    if usage_stats and usage_stats.get("total_tokens", 0) > 0:
        print("\n📊 AI Cleaning Statistics:")
        print(f"  • Input tokens: {usage_stats['input_tokens']:,}")
        print(f"  • Output tokens: {usage_stats['output_tokens']:,}")
        print(f"  • Total tokens: {usage_stats['total_tokens']:,}")
        if usage_stats.get("cost_known", True):
            print(f"💰 Cost: ${usage_stats['cost_usd']:.4f} "
                  f"(≈ ¥{usage_stats['cost_cny']:.2f} CNY)\n")
        else:
            print("💰 Cost: unknown for this model "
                  "(set LLM_PRICE_INPUT / LLM_PRICE_OUTPUT to see it)\n")

    chapters = transcript_sections_to_epub_chapters(sections)
    output_filename = f"{safe_title}.epub"
    save_epub(video_title, video_id, chapters, output_filename)
    save_txt(transcript, video_title, video_id, sections, f"{safe_title}.txt")


if __name__ == "__main__":
    try:
        main()
    except ImportError as e:
        print("Missing dependencies. Please install them with:")
        print("  pip install youtube-transcript-api ebooklib pytube")
        sys.exit(1)
