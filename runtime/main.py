import sys
import re
import json
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from ebooklib import epub
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
    # Try to fetch title from YouTube using pytube
    if YouTube:
        try:
            yt = YouTube(url)
            title = yt.title
            if title:
                return title
        except Exception as e:
            print(f"Could not fetch video title automatically: {e}")
    
    # Fallback: use video ID or default
    video_id = extract_video_id(url)
    if video_id:
        return f"YouTube Transcript {video_id}"
    return "YouTube Transcript"

def sanitize_filename(name):
    # Remove or replace characters not allowed in filenames
    return re.sub(r'[\\/*?\:"<>|]', '', name)


def fetch_youtube_chapters(video_id):
    """Fetch chapter timestamps from YouTube video description using Playwright.

    Args:
        video_id: YouTube video ID

    Returns:
        List of dicts with 'title' and 'timestamp' keys, or None if no chapters found
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("Playwright not available, cannot fetch chapters")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # Navigate to YouTube video page
            url = f"https://www.youtube.com/watch?v={video_id}"
            page.goto(url, timeout=30000)

            # Wait for page to load
            page.wait_for_load_state("networkidle", timeout=10000)

            # Try to get description from the page
            try:
                description = None

                # Method 1: Use ytInitialData which contains the FULL description
                # This is more reliable than clicking buttons
                try:
                    initial_data_script = page.query_selector('script:has-text("ytInitialData")')
                    if initial_data_script:
                        script_content = initial_data_script.text_content()
                        # Extract the JSON data
                        match = re.search(r'ytInitialData\s*=\s*({.+?})\s*;</script>', script_content, re.DOTALL)
                        if not match:
                            match = re.search(r'ytInitialData\s*=\s*({.+});', script_content, re.DOTALL)
                        if match:
                            try:
                                data = json.loads(match.group(1))
                                # Navigate the YouTube data structure to find description
                                contents = data.get('contents', {}).get('twoColumnWatchNextResults', {}).get('results', {}).get('results', {}).get('contents', [])

                                for content in contents:
                                    if 'videoSecondaryInfoRenderer' in content:
                                        desc_data = content['videoSecondaryInfoRenderer'].get('description', {})
                                        if 'runs' in desc_data:
                                            # Build description from text runs
                                            description_parts = []
                                            for run in desc_data['runs']:
                                                if 'text' in run:
                                                    description_parts.append(run['text'])
                                            description = '\n'.join(description_parts)
                                            break
                            except Exception as e:
                                print(f"Error parsing ytInitialData JSON: {e}")
                                pass
                except Exception as e:
                    print(f"Error extracting ytInitialData: {e}")
                    pass

                # Method 2: Fallback - try clicking "Show more" button
                if not description or len(description) < 500:
                    try:
                        # Scroll to make description visible
                        page.evaluate('window.scrollTo(0, 500)')
                        page.wait_for_timeout(500)

                        # Try multiple selectors for the "Show more" button
                        show_more_selectors = [
                            '#expand',
                            '#expand-button',
                            'tp-yt-paper-button#expand',
                            'ytd-text-inline-expander #expand',
                            'button[aria-label="Show more"]',
                        ]

                        for selector in show_more_selectors:
                            try:
                                show_more_button = page.query_selector(selector)
                                if show_more_button:
                                    show_more_button.click()
                                    page.wait_for_timeout(1000)
                                    break
                            except:
                                continue

                        # Try to get description from various containers
                        desc_selectors = [
                            'ytd-text-inline-expander#description',
                            '#description-inner',
                            'yt-attributed-string#description',
                            'ytd-video-secondary-info-renderer #description',
                        ]

                        for selector in desc_selectors:
                            try:
                                desc_container = page.query_selector(selector)
                                if desc_container:
                                    description = desc_container.text_content()
                                    if len(description) >= 500:
                                        break
                            except:
                                continue
                    except:
                        pass

                if not description or len(description) < 100:
                    print("Could not extract full description from page")
                    return None

                # Parse chapters from description
                chapters = []
                timestamp_pattern = r'^(\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)$'

                lines = description.split('\n')
                for line in lines:
                    line = line.strip()
                    match = re.match(timestamp_pattern, line)
                    if match:
                        timestamp_str = match.group(1)
                        title = match.group(2).strip()

                        # Convert timestamp to seconds
                        parts = timestamp_str.split(':')
                        if len(parts) == 2:  # MM:SS
                            minutes, seconds = int(parts[0]), int(parts[1])
                            total_seconds = minutes * 60 + seconds
                        elif len(parts) == 3:  # HH:MM:SS
                            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
                            total_seconds = hours * 3600 + minutes * 60 + seconds
                        else:
                            continue

                        chapters.append({
                            'title': title,
                            'timestamp': total_seconds
                        })

                # Only return if we found at least 2 chapters
                if len(chapters) >= 2:
                    print(f"Found {len(chapters)} chapters in video description")
                    return chapters
                else:
                    print(f"No valid chapters found (detected {len(chapters)} chapters, need at least 2)")
                    return None

            except Exception as e:
                print(f"Error parsing chapters: {e}")
                return None
            finally:
                browser.close()

    except Exception as e:
        print(f"Error fetching chapters with Playwright: {e}")
        return None

    return None



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

def group_transcript_by_interval(transcript, interval_seconds=2400, use_cleaner=False, api_key=None, progress_callback=None):
    """Group transcript entries into sections of interval_seconds (default 40 min - double the size).
    Returns: (sections, usage_stats) tuple"""
    sections = []
    current_section = []
    current_start = 0
    total_usage_stats = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0, "cost_cny": 0}

    for entry in transcript:
        if entry.start >= current_start + interval_seconds and current_section:
            # Clean the section if cleaner is enabled
            cleaned_section, usage_stats = clean_transcript_entries(current_section, use_cleaner, api_key, progress_callback)
            # Accumulate usage stats
            if usage_stats:
                total_usage_stats["input_tokens"] += usage_stats["input_tokens"]
                total_usage_stats["output_tokens"] += usage_stats["output_tokens"]
                total_usage_stats["total_tokens"] += usage_stats["total_tokens"]
                total_usage_stats["cost_usd"] += usage_stats["cost_usd"]
                total_usage_stats["cost_cny"] += usage_stats["cost_cny"]
            # Try to end at a complete sentence
            cleaned_section = end_section_at_sentence(cleaned_section)
            sections.append((current_start, current_start + interval_seconds, cleaned_section))
            current_start += interval_seconds
            current_section = []
        current_section.append(entry)
    if current_section:
        # Clean the last section if cleaner is enabled
        cleaned_section, usage_stats = clean_transcript_entries(current_section, use_cleaner, api_key, progress_callback)
        # Accumulate usage stats
        if usage_stats:
            total_usage_stats["input_tokens"] += usage_stats["input_tokens"]
            total_usage_stats["output_tokens"] += usage_stats["output_tokens"]
            total_usage_stats["total_tokens"] += usage_stats["total_tokens"]
            total_usage_stats["cost_usd"] += usage_stats["cost_usd"]
            total_usage_stats["cost_cny"] += usage_stats["cost_cny"]
        # Add the last section
        cleaned_section = end_section_at_sentence(cleaned_section)
        sections.append((current_start, current_start + interval_seconds, cleaned_section))

    # Return both sections and usage stats
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
    if not chapters or len(chapters) < 2:
        # Fallback to interval-based grouping if no chapters
        return group_transcript_by_interval(transcript, interval_seconds=2400, use_cleaner=use_cleaner, api_key=api_key, progress_callback=progress_callback)

    sections = []
    total_usage_stats = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0, "cost_cny": 0}

    # Sort chapters by timestamp
    sorted_chapters = sorted(chapters, key=lambda x: x['timestamp'])

    for i in range(len(sorted_chapters)):
        current_chapter = sorted_chapters[i]
        start_time = current_chapter['timestamp']
        title = current_chapter['title']

        # Determine end time (start of next chapter, or end of transcript)
        if i < len(sorted_chapters) - 1:
            end_time = sorted_chapters[i + 1]['timestamp']
        else:
            # Last chapter - use the end of the transcript
            if transcript:
                end_time = transcript[-1].start + transcript[-1].duration
            else:
                end_time = start_time + 3600  # Default 1 hour

        # Collect all transcript entries within this chapter's time range
        chapter_entries = []
        for entry in transcript:
            # Include entries that fall within this chapter
            if start_time <= entry.start < end_time:
                chapter_entries.append(entry)

        if chapter_entries:
            # Clean the section if cleaner is enabled
            cleaned_section, usage_stats = clean_transcript_entries(chapter_entries, use_cleaner, api_key, progress_callback)

            # Accumulate usage stats
            if usage_stats:
                total_usage_stats["input_tokens"] += usage_stats["input_tokens"]
                total_usage_stats["output_tokens"] += usage_stats["output_tokens"]
                total_usage_stats["total_tokens"] += usage_stats["total_tokens"]
                total_usage_stats["cost_usd"] += usage_stats["cost_usd"]
                total_usage_stats["cost_cny"] += usage_stats["cost_cny"]

            # Try to end at a complete sentence
            cleaned_section = end_section_at_sentence(cleaned_section)

            # Append section with title: (start, end, text, title)
            sections.append((start_time, end_time, cleaned_section, title))

    return sections, total_usage_stats if use_cleaner else None

def end_section_at_sentence(entries):
    """Try to end the section at a complete sentence."""
    if not entries:
        return entries
    
    # Look for sentence endings in the last few entries
    for i in range(len(entries) - 1, max(0, len(entries) - 5), -1):
        text = entries[i].text.strip()
        if text.endswith('.') or text.endswith('!') or text.endswith('?'):
            return entries[:i+1]
    
    # If no sentence ending found, return all entries
    return entries

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
            for idx, (start, end, entries) in enumerate(sections, 1):
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
    if len(sys.argv) != 2:
        print("Usage: python main.py <YouTube URL>")
        sys.exit(1)
    url = sys.argv[1]
    video_id = extract_video_id(url)
    if not video_id:
        print("Invalid YouTube URL.")
        sys.exit(1)
    video_title = fetch_video_title(url)
    safe_title = sanitize_filename(video_title)
    transcript = fetch_transcript(video_id)

    # Save raw transcript data
    save_raw_transcript(transcript, video_id, video_title)

    # Enable AI cleaning with OpenAI API
    print("🤖 Using AI to clean transcript (removing filler words, fixing grammar)...")
    api_key = llm_config.api_key()
    sections, usage_stats = group_transcript_by_interval(
        transcript,
        interval_seconds=1200,  # 20 min
        use_cleaner=True,
        api_key=api_key
    )

    # Show token usage if available
    if usage_stats and usage_stats.get('total_tokens', 0) > 0:
        print(f"\n📊 AI Cleaning Statistics:")
        print(f"  • Input tokens: {usage_stats['input_tokens']:,}")
        print(f"  • Output tokens: {usage_stats['output_tokens']:,}")
        print(f"  • Total tokens: {usage_stats['total_tokens']:,}")
        print(f"💰 Cost: ${usage_stats['cost_usd']:.4f} (≈ ¥{usage_stats['cost_cny']:.2f} CNY)\n")

    chapters = transcript_sections_to_epub_chapters(sections)
    output_filename = f"{safe_title}.epub"
    save_epub(video_title, video_id, chapters, output_filename)

    # Also save as TXT for easy viewing
    txt_filename = save_txt(transcript, video_title, video_id, sections, f"{safe_title}.txt")


if __name__ == "__main__":
    try:
        main()
    except ImportError as e:
        print("Missing dependencies. Please install them with:")
        print("  pip install youtube-transcript-api ebooklib pytube")
        sys.exit(1)
