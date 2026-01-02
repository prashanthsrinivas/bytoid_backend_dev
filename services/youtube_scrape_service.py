import logging
from queue import Queue
import re
import requests
import os
import time
from datetime import datetime
from dotenv import load_dotenv
from datetime import timezone
import yt_dlp
import asyncio
import tempfile
import random
from agent_route.s_t_s import Speech2TextService
from utils.fireworkzz import get_firework_embedding

# Keep youtube_transcript_api as fallback
try:
    from youtube_transcript_api import YouTubeTranscriptApi

    YOUTUBE_TRANSCRIPT_AVAILABLE = True
except ImportError:
    YOUTUBE_TRANSCRIPT_AVAILABLE = False

# Add PyTube for fallback
try:
    from pytube import YouTube

    PYTUBE_AVAILABLE = True
except ImportError:
    PYTUBE_AVAILABLE = False
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger(__name__)


class YouTubeScrapingClient:
    def __init__(self, user_id: str):
        load_dotenv()
        self.lancedb_url = os.getenv("LANCE_DB_IP")
        self.user_id = user_id
        self.dimension = 2880
        # self.embeddings = OpenAIEmbeddings(
        #     model="text-embedding-3-large",
        #     openai_api_key=os.getenv("OPENAI_API_KEY"),
        #     dimensions=self.dimension,
        # )
        self.embeddings = None
        # asyncio.create_task(self._load_embeddings())
        self.speech_service = Speech2TextService(user_id)

        # Proxy list for rotation (add your proxy servers here)
        self.proxies = [
            # Add your proxy servers here
            # "http://proxy1:port",
            # "http://proxy2:port",
        ]
        self.driver_pool = Queue()

    async def _load_embeddings(self):
        self.embeddings = await get_firework_embedding()

    async def _ensure_embeddings(self):
        if self.embeddings is None:
            await self._load_embeddings()

    def get_rotating_proxy(self):
        """Get a rotating proxy from available proxies"""
        if self.proxies:
            return random.choice(self.proxies)
        return None

    def _borrow_driver(self):
        return self.driver_pool.get()

    def _return_driver(self, driver):
        self.driver_pool.put(driver)

    def extract_video_id(self, youtube_url):
        """Extract video ID from various YouTube URL formats"""
        patterns = [
            r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]+)",
            r"(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]+)",
            r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]+)",
            r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/v\/([a-zA-Z0-9_-]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, youtube_url)
            if match:
                return match.group(1)
        return None

    def _setup_selenium_driver(self) -> webdriver.Chrome:
        """Setup Chrome driver with optimized settings for speed"""
        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument(
                "--disable-images"
            )  # Disable image loading for speed
            chrome_options.add_argument("--blink-settings=imagesEnabled=false")
            chrome_options.add_argument("--window-size=1920,1080")
            # Don't disable JavaScript for dynamic sites like YouTube
            chrome_options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            chrome_options.add_argument("--start-maximized")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-plugins")
            chrome_options.add_argument("--disable-popup-blocking")
            # Add header to bypass some blocking
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option(
                "excludeSwitches", ["enable-automation"]
            )
            chrome_options.add_experimental_option("useAutomationExtension", False)

            chrome_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/snap/bin/chromium",
            ]

            for chrome_path in chrome_paths:
                if os.path.exists(chrome_path):
                    chrome_options.binary_location = chrome_path
                    logger.info(f"[FAST] Using Chrome: {chrome_path}")
                    break

            driver = webdriver.Chrome(options=chrome_options)
            logger.info("[FAST] Chrome driver initialized")
            return driver

        except Exception as e:
            logger.error(f"[FAST] Chrome setup failed: {e}")
            raise

    def start_driver(self):
        for _ in range(self.max_workers):
            driver = self._setup_selenium_driver()
            self.driver_pool.put(driver)

    def extract_with_pytube(self, youtube_url):
        """Extract metadata and audio using PyTube"""
        if not PYTUBE_AVAILABLE:
            print(f"[YOUTUBE] PyTube not available")
            return None, None

        try:
            print(f"[YOUTUBE] Trying PyTube extraction for: {youtube_url}")
            yt = YouTube(youtube_url)

            # Get metadata
            metadata = {
                "title": yt.title or "YouTube Video",
                "author": yt.author or "Unknown",
                "duration": yt.length,
                "description": yt.description or "",
                "view_count": yt.views or 0,
                "upload_date": "",
            }

            print(
                f"[YOUTUBE] PyTube metadata: {metadata['title']} by {metadata['author']}"
            )

            # Download audio
            audio_stream = yt.streams.filter(only_audio=True).first()
            if not audio_stream:
                raise Exception("No audio stream available")

            # Download to temporary location
            temp_dir = tempfile.gettempdir()
            audio_file = audio_stream.download(output_path=temp_dir)

            # Rename to a clean name
            file_ext = os.path.splitext(audio_file)[1]
            clean_audio_path = os.path.join(
                temp_dir, f"youtube_audio_pytube_{os.getpid()}{file_ext}"
            )

            import shutil

            shutil.move(audio_file, clean_audio_path)

            print(f"[YOUTUBE] PyTube audio downloaded: {clean_audio_path}")
            return metadata, clean_audio_path

        except Exception as e:
            print(f"[YOUTUBE] PyTube extraction failed: {e}")
            return None, None

    def get_video_metadata_and_audio_with_proxy(self, youtube_url):
        """Get video metadata and extract audio using yt-dlp with proxy"""
        try:
            print(f"[YOUTUBE] Starting yt-dlp with proxy extraction for: {youtube_url}")
            proxy = self.get_rotating_proxy()

            with tempfile.TemporaryDirectory() as temp_dir:
                output_template = os.path.join(temp_dir, "audio.%(ext)s")

                ydl_opts = {
                    "format": "bestaudio",
                    "outtmpl": output_template,
                    "quiet": False,
                    "no_warnings": False,
                    # Enhanced headers to avoid bot detection
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-us,en;q=0.5",
                        "Accept-Encoding": "gzip,deflate",
                        "Accept-Charset": "ISO-8859-1,utf-8;q=0.7,*;q=0.7",
                        "Keep-Alive": "300",
                        "Connection": "keep-alive",
                    },
                    "extractor_args": {
                        "youtube": {
                            "skip": ["hls", "dash"],
                            "player_skip": ["configs"],
                        }
                    },
                    "retries": 5,
                    "fragment_retries": 5,
                    "sleep_interval": 2,
                    "max_sleep_interval": 10,
                }

                # Add proxy if available
                if proxy:
                    ydl_opts["proxy"] = proxy
                    print(f"[YOUTUBE] Using proxy: {proxy}")

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    print(f"[YOUTUBE] Extracting video info with proxy...")
                    info = ydl.extract_info(youtube_url, download=False)

                    metadata = {
                        "title": info.get("title", "YouTube Video"),
                        "author": info.get("uploader", info.get("channel", "Unknown")),
                        "duration": info.get("duration", None),
                        "description": info.get("description", ""),
                        "view_count": info.get("view_count", 0),
                        "upload_date": info.get("upload_date", ""),
                    }

                    print(
                        f"[YOUTUBE] Proxy extracted metadata: {metadata['title']} by {metadata['author']}"
                    )

                    print(f"[YOUTUBE] Starting audio download with proxy...")
                    ydl.download([youtube_url])

                    # Find downloaded file
                    audio_file = None
                    for file in os.listdir(temp_dir):
                        file_path = os.path.join(temp_dir, file)
                        if os.path.isfile(file_path):
                            print(
                                f"[YOUTUBE] Found file: {file} ({os.path.getsize(file_path)} bytes)"
                            )
                            audio_file = file_path
                            break

                    if not audio_file:
                        raise Exception("No audio file found after download")

                    # Copy to permanent location
                    import shutil

                    file_ext = os.path.splitext(audio_file)[1] or ".webm"
                    clean_audio_path = os.path.join(
                        tempfile.gettempdir(),
                        f"youtube_audio_proxy_{os.getpid()}{file_ext}",
                    )
                    shutil.copy2(audio_file, clean_audio_path)
                    print(f"[YOUTUBE] Proxy audio copied to: {clean_audio_path}")

                    return metadata, clean_audio_path

        except Exception as e:
            print(f"[YOUTUBE] yt-dlp with proxy extraction failed: {e}")
            return None, None

    def get_transcript_with_proxy(self, video_id):
        """Get transcript using YouTube Transcript API with proxy simulation"""
        if not YOUTUBE_TRANSCRIPT_AVAILABLE:
            print(f"[YOUTUBE] YouTube transcript API not available")
            return None

        try:
            print(
                f"[YOUTUBE] Trying transcript API with enhanced headers for {video_id}"
            )

            # Simulate different session/headers to avoid blocking

            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                }
            )

            time.sleep(random.uniform(1, 3))

            transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
            print(f"[YOUTUBE] Transcript API found {len(transcript_data)} segments")

            # Combine transcript segments
            full_transcript = ""
            for entry in transcript_data:
                text = entry.get("text", "").strip()
                if text:
                    full_transcript += text + " "

            result = full_transcript.strip()
            print(f"[YOUTUBE] Transcript extracted: {len(result)} characters")
            print(f"[YOUTUBE] Transcript preview: {result[:200]}...")
            return result

        except Exception as e:
            error_msg = str(e)
            if (
                "YouTube is blocking requests from your IP" in error_msg
                or "cloud provider" in error_msg
            ):
                print(
                    f"[YOUTUBE] YouTube blocked transcript API (cloud provider restriction)"
                )
                return None
            else:
                print(f"[YOUTUBE] Transcript API with proxy simulation failed: {e}")
                return None

    def extract_transcript_selenium(self, youtube_url):
        """Extract transcript using browser automation (Selenium)"""
        try:
            print(f"[YOUTUBE] Trying Selenium transcript extraction for: {youtube_url}")

            # Setup Selenium driver (reuse existing setup)
            # driver = (
            #     self._setup_selenium_driver()
            #     if hasattr(self, "_setup_selenium_driver")
            #     else None
            # )
            driver = self._setup_selenium_driver()

            # if not driver:
            #     # Basic Chrome setup for transcript extraction
            #     from selenium.webdriver.chrome.service import Service

            #     chrome_options = Options()
            #     chrome_options.add_argument("--headless")
            #     chrome_options.add_argument("--no-sandbox")
            #     chrome_options.add_argument("--disable-dev-shm-usage")
            #     chrome_options.add_argument("--disable-gpu")
            #     chrome_options.add_argument(
            #         "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            #     )

            #     driver = webdriver.Chrome(options=chrome_options)

            driver.get(youtube_url)

            # Wait for page to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Try to find and click transcript button
            try:
                # Look for transcript button (multiple possible selectors)
                transcript_selectors = [
                    "//button[@aria-label='Show transcript']",
                    "//button[contains(@aria-label, 'transcript')]",
                    "//button[contains(text(), 'Transcript')]",
                    "//*[@id='transcript-button']",
                ]

                transcript_button = None
                for selector in transcript_selectors:
                    try:
                        transcript_button = driver.find_element(By.XPATH, selector)
                        break
                    except:
                        continue

                if transcript_button:
                    driver.execute_script("arguments[0].click();", transcript_button)
                    time.sleep(2)

                    # Extract transcript text
                    transcript_selectors = [
                        ".transcript-segment",
                        ".ytd-transcript-segment-renderer",
                        "[data-purpose='transcript-segment']",
                    ]

                    transcript_text = ""
                    for selector in transcript_selectors:
                        try:
                            elements = driver.find_elements(By.CSS_SELECTOR, selector)
                            if elements:
                                transcript_text = " ".join([el.text for el in elements])
                                break
                        except:
                            continue

                    if transcript_text:
                        print(
                            f"[YOUTUBE] Selenium transcript extracted: {len(transcript_text)} characters"
                        )
                        return transcript_text

            except Exception as e:
                print(f"[YOUTUBE] Selenium transcript button not found or failed: {e}")

            # Try to extract title and basic info even if transcript fails
            try:
                title_element = driver.find_element(
                    By.CSS_SELECTOR, "h1.ytd-video-primary-info-renderer"
                )
                title = title_element.text if title_element else "YouTube Video"
                print(f"[YOUTUBE] Selenium extracted title: {title}")

                # Could return basic metadata even without transcript
                return None
            except:
                pass

            return None

        except Exception as e:
            print(f"[YOUTUBE] Selenium transcript extraction failed: {e}")
            return None
        finally:
            if "driver" in locals():
                try:
                    driver.quit()
                except:
                    pass
        """Get video metadata and extract audio using yt-dlp"""
        try:
            print(f"[YOUTUBE] Starting yt-dlp extraction for: {youtube_url}")
            with tempfile.TemporaryDirectory() as temp_dir:
                # Use a clean filename template
                output_template = os.path.join(temp_dir, "audio.%(ext)s")

                ydl_opts = {
                    "format": "bestaudio",  # Just get best audio, no conversion
                    "outtmpl": output_template,
                    "quiet": False,  # Enable verbose output for debugging
                    "no_warnings": False,
                    # Enhanced headers to avoid bot detection
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-us,en;q=0.5",
                        "Accept-Encoding": "gzip,deflate",
                        "Accept-Charset": "ISO-8859-1,utf-8;q=0.7,*;q=0.7",
                        "Keep-Alive": "300",
                        "Connection": "keep-alive",
                    },
                    # Add extractor arguments for YouTube
                    "extractor_args": {
                        "youtube": {
                            "skip": ["hls", "dash"],
                            "player_skip": ["configs"],
                        }
                    },
                    # Retry settings
                    "retries": 5,
                    "fragment_retries": 5,
                    # Add some delay to avoid rate limiting
                    "sleep_interval": 2,
                    "max_sleep_interval": 10,
                }

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    print(f"[YOUTUBE] Extracting video info...")
                    # Get video info first
                    info = ydl.extract_info(youtube_url, download=False)

                    # Extract metadata
                    metadata = {
                        "title": info.get("title", "YouTube Video"),
                        "author": info.get("uploader", info.get("channel", "Unknown")),
                        "duration": info.get("duration", None),
                        "description": info.get("description", ""),
                        "view_count": info.get("view_count", 0),
                        "upload_date": info.get("upload_date", ""),
                    }

                    print(
                        f"[YOUTUBE] Extracted metadata: {metadata['title']} by {metadata['author']} ({metadata['duration']}s)"
                    )

                    print(f"[YOUTUBE] Starting audio download...")
                    # Download audio
                    ydl.download([youtube_url])

                    # Find the downloaded audio file
                    audio_file = None
                    print(f"[YOUTUBE] Checking temp directory: {temp_dir}")
                    for file in os.listdir(temp_dir):
                        file_path = os.path.join(temp_dir, file)
                        if os.path.isfile(file_path):
                            print(
                                f"[YOUTUBE] Found file: {file} ({os.path.getsize(file_path)} bytes)"
                            )
                            audio_file = file_path
                            break

                    if not audio_file:
                        raise Exception("No audio file found after download")

                    # Copy the file to a new location with a clean name since temp_dir will be deleted
                    import shutil

                    file_ext = os.path.splitext(audio_file)[1] or ".webm"
                    clean_audio_path = os.path.join(
                        tempfile.gettempdir(), f"youtube_audio_{os.getpid()}{file_ext}"
                    )
                    shutil.copy2(audio_file, clean_audio_path)
                    print(f"[YOUTUBE] Audio copied to: {clean_audio_path}")

                    return metadata, clean_audio_path

        except Exception as e:
            print(f"[YOUTUBE] yt-dlp extraction failed: {e}")
            import traceback

            traceback.print_exc()
            return None, None

    def get_transcript_fallback(self, video_id):
        """Fallback to YouTube transcript API if yt-dlp fails"""
        if not YOUTUBE_TRANSCRIPT_AVAILABLE:
            print(f"[YOUTUBE] YouTube transcript API not available")
            return None

        try:
            print(f"[YOUTUBE] Trying fallback transcript API for {video_id}")
            # Use the correct API method
            transcript_data = YouTubeTranscriptApi.get_transcript(video_id)
            print(
                f"[YOUTUBE] Found transcript data with {len(transcript_data)} segments"
            )

            # Combine transcript segments
            full_transcript = ""
            for entry in transcript_data:
                text = entry.get("text", "").strip()
                if text:
                    full_transcript += text + " "

            result = full_transcript.strip()
            print(f"[YOUTUBE] Transcript extracted: {len(result)} characters")
            print(f"[YOUTUBE] Transcript preview: {result[:200]}...")
            return result

        except Exception as e:
            error_msg = str(e)
            if (
                "YouTube is blocking requests from your IP" in error_msg
                or "cloud provider" in error_msg
            ):
                print(
                    f"[YOUTUBE] YouTube blocked our server IP (cloud provider restriction)"
                )
                return None
            else:
                print(f"[YOUTUBE] Fallback transcript API failed for {video_id}: {e}")
                import traceback

                traceback.print_exc()
                return None

    def split_audio_file(self, audio_file_path, max_duration_minutes=10):
        """Split long audio files into smaller segments for better transcription"""
        try:
            # For now, return the original file since ffmpeg is not available
            # This will be enhanced when ffmpeg is installed
            print(f"[YOUTUBE] ffmpeg not available, using original file (no splitting)")
            return [audio_file_path]

        except Exception as e:
            print(f"[YOUTUBE] Error splitting audio: {e}")
            return [audio_file_path]

    async def transcribe_audio_segments(self, audio_segments):
        """Transcribe multiple audio segments and combine them"""
        try:
            all_transcripts = []

            for i, segment_file in enumerate(audio_segments):
                print(
                    f"[YOUTUBE] Transcribing segment {i+1}/{len(audio_segments)}: {segment_file}"
                )
                print(f"[YOUTUBE] File exists: {os.path.exists(segment_file)}")
                if os.path.exists(segment_file):
                    print(f"[YOUTUBE] File size: {os.path.getsize(segment_file)} bytes")

                transcript = await self.speech_service.transcribe_audio(segment_file)
                if transcript:
                    all_transcripts.append(transcript)
                    print(f"[YOUTUBE] Segment {i+1} transcript: {transcript[:100]}...")
                else:
                    print(f"[YOUTUBE] No transcript for segment {i+1}")

                # Clean up segment file if it's different from original
                try:
                    if len(audio_segments) > 1 and segment_file != audio_segments[0]:
                        os.remove(segment_file)
                except:
                    pass

            # Combine all transcripts
            combined_transcript = " ".join(all_transcripts)
            print(
                f"[YOUTUBE] Combined transcript length: {len(combined_transcript)} characters"
            )

            return combined_transcript if combined_transcript.strip() else None

        except Exception as e:
            print(f"[YOUTUBE] Error transcribing segments: {e}")
            import traceback

            traceback.print_exc()
            return None

    async def transcribe_audio(self, audio_file_path):
        """Transcribe audio using the existing Speech2TextService with segmentation for long files"""
        try:
            print(f"[YOUTUBE] Starting transcription for: {audio_file_path}")

            # Split audio if it's too long (currently returns original file)
            audio_segments = self.split_audio_file(
                audio_file_path, max_duration_minutes=10
            )

            # Transcribe all segments
            transcript = await self.transcribe_audio_segments(audio_segments)

            return transcript
        except Exception as e:
            print(f"[YOUTUBE] Error in transcribe_audio: {e}")
            import traceback

            traceback.print_exc()
            return None

    def scrape_youtube_single_video_only(self, youtube_url):
        """
        Scrape ONLY a single YouTube video (NOT multi-level, NOT related videos).

        This is specifically for /scrape-and-summarize endpoint to prevent Selenium
        from treating YouTube as a regular website and following all internal links.

        Uses the proven working hybrid method but for a single video only.
        """
        # Simply use the working hybrid scraping method
        # This already handles all the extraction logic properly
        return self.scrape_youtube_video_hybrid(youtube_url)

    def scrape_youtube_video(self, youtube_url):
        """Main method now using hybrid approach"""
        return self.scrape_youtube_video_hybrid(youtube_url)

    def scrape_youtube_video_hybrid(self, youtube_url):
        """Fast YouTube scraping - uses only Selenium which works reliably"""

        # self._setup_selenium_driver()

        video_id = self.extract_video_id(youtube_url)
        if not video_id:
            logger.error(f"[HYBRID] Failed to extract video ID from: {youtube_url}")
            return None

        logger.info(f"[HYBRID] Starting YouTube processing for: {youtube_url}")
        print(f"[HYBRID] Starting YouTube processing for: {youtube_url}")

        # Use only Selenium which works reliably
        # Skip yt-dlp (blocked by YouTube), pytube (400 errors), transcript API (requires auth)
        try:
            logger.info(f"[HYBRID] 🔄 Extracting with Selenium...")
            print(f"[HYBRID] 🔄 Extracting with Selenium...")
            result = self.extract_with_selenium(youtube_url)

            if result and isinstance(result, dict) and result.get("transcript_raw"):
                logger.info(f"[HYBRID] ✅ Success with Selenium")
                print(f"[HYBRID] ✅ Success with Selenium")
                return result

        except Exception as e:
            logger.error(f"[HYBRID] Selenium failed: {e}")
            print(f"[HYBRID] Selenium failed: {e}")

        # If Selenium fails, return error
        logger.error(f"[HYBRID] 🚫 Failed to extract video for {youtube_url}")
        print(f"[HYBRID] 🚫 Failed to extract video for {youtube_url}")
        return {
            "url": youtube_url,
            "video_id": video_id,
            "title": "YouTube Video",
            "status": "failed",
            "content": "Unable to access this video - Failed to extract transcript",
            "error": "extraction_failed",
            "metadata": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "scraping_method": "selenium_failed",
                "note": "Could not extract video content using available methods",
            },
        }

    def extract_with_ytdlp_proxy(self, youtube_url):
        """Extract using yt-dlp with proxy support"""
        return self.get_video_metadata_and_audio_with_proxy(youtube_url)

    def get_video_metadata_and_audio(self, youtube_url):
        """Original method - fallback to proxy version"""
        return self.get_video_metadata_and_audio_with_proxy(youtube_url)

    def get_transcript_only_with_proxy(self, video_id, youtube_url):
        """Get transcript only using API with proxy simulation"""
        transcript = self.get_transcript_with_proxy(video_id)
        if transcript:
            # Try to get basic metadata
            try:
                oembed_url = (
                    f"https://www.youtube.com/oembed?url={youtube_url}&format=json"
                )
                response = requests.get(oembed_url, timeout=10)
                if response.status_code == 200:
                    oembed_data = response.json()
                    title = oembed_data.get("title", f"YouTube Video {video_id}")
                    author = oembed_data.get("author_name", "Unknown")
                else:
                    title = f"YouTube Video {video_id}"
                    author = "Unknown"
            except:
                title = f"YouTube Video {video_id}"
                author = "Unknown"

            formatted_content = f"""
**YouTube Video Analysis**

**Title:** {title}
**Author:** {author}
**Video URL:** {youtube_url}

**Transcript:**
{transcript}
"""

            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": title,
                "content": formatted_content,
                "transcript_raw": transcript,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "transcript_api_with_proxy_simulation",
                    "author": author,
                    "video_id": video_id,
                    "content_length": len(transcript),
                },
            }
        return None

    def extract_with_selenium(self, youtube_url):
        """Extract using Selenium browser automation"""
        transcript = self.extract_transcript_selenium(youtube_url)
        if transcript:
            video_id = self.extract_video_id(youtube_url)
            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": "YouTube Video (Selenium)",
                "content": f"**Transcript:**\n{transcript}",
                "transcript_raw": transcript,
                "status": "active",
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "selenium_browser_automation",
                    "video_id": video_id,
                    "content_length": len(transcript),
                },
            }
        return None

    def get_transcript_only_original(self, video_id, youtube_url):
        """Get transcript using original method"""
        transcript = self.get_transcript_fallback(video_id)
        if transcript:
            return self.get_transcript_only_with_proxy(
                video_id, youtube_url
            )  # Reuse formatting
        return None

    def process_audio_to_transcript(
        self, metadata, audio_file, youtube_url, video_id, method_name
    ):
        """Process audio file to transcript using Whisper"""
        try:
            # Transcribe audio using async function
            import concurrent.futures

            def run_transcription():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    return loop.run_until_complete(self.transcribe_audio(audio_file))
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_transcription)
                transcript = future.result(timeout=300)  # 5 minute timeout

            if not transcript:
                return {
                    "url": youtube_url,
                    "video_id": video_id,
                    "title": metadata["title"],
                    "content": "Failed to transcribe audio from this video",
                    "error": "transcription_failed",
                    "metadata": {
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "scraping_method": f"{method_name}_transcription_failed",
                        "author": metadata["author"],
                        "video_id": video_id,
                    },
                }

            # Format content with video info and transcript
            formatted_content = f"""
**YouTube Video Analysis**

**Title:** {metadata['title']}
**Author:** {metadata['author']}
**Video URL:** {youtube_url}
**Duration:** {metadata['duration']} seconds

**Transcript:**
{transcript}
"""

            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": metadata["title"],
                "content": formatted_content,
                "transcript_raw": transcript,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": f"{method_name}_with_whisper",
                    "author": metadata["author"],
                    "video_id": video_id,
                    "duration": metadata["duration"],
                    "content_length": len(transcript),
                    "description": (
                        metadata["description"][:500] if metadata["description"] else ""
                    ),
                },
            }

        except Exception as e:
            print(f"[HYBRID] Error processing audio to transcript: {e}")
            return None
        finally:
            # Cleanup temporary audio file
            if audio_file and os.path.exists(audio_file):
                try:
                    os.remove(audio_file)
                except:
                    pass
        """Main method to scrape YouTube video and extract transcript using yt-dlp + Whisper"""
        audio_file = None
        try:
            print(f"[YOUTUBE] Processing video: {youtube_url}")

            # Extract video ID
            video_id = self.extract_video_id(youtube_url)
            if not video_id:
                return None

            # Get video metadata and audio
            metadata, audio_file = self.get_video_metadata_and_audio(youtube_url)
            if not metadata or not audio_file:
                print(f"[YOUTUBE] yt-dlp failed, trying fallback transcript API...")
                # Try fallback to transcript API
                transcript = self.get_transcript_fallback(video_id)
                if transcript:
                    print(
                        f"[YOUTUBE] Fallback transcript successful: {len(transcript)} chars"
                    )
                    # Try to get comprehensive metadata using multiple methods
                    title = f"YouTube Video {video_id}"
                    author = "Unknown"
                    duration = None

                    try:
                        # Method 1: Try oembed API
                        oembed_url = f"https://www.youtube.com/oembed?url={youtube_url}&format=json"
                        response = requests.get(oembed_url, timeout=10)
                        if response.status_code == 200:
                            oembed_data = response.json()
                            title = oembed_data.get("title", title)
                            author = oembed_data.get("author_name", author)
                            print(f"[YOUTUBE] oEmbed metadata: {title} by {author}")
                    except Exception as e:
                        print(f"[YOUTUBE] oEmbed failed: {e}")

                    try:
                        # Method 2: Try yt-dlp info extraction without download
                        ydl_opts = {
                            "quiet": True,
                            "no_warnings": True,
                            "skip_download": True,
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(youtube_url, download=False)
                            title = info.get("title", title)
                            author = info.get("uploader", info.get("channel", author))
                            duration = info.get("duration", duration)
                            print(f"[YOUTUBE] yt-dlp info: {title} by {author}")
                    except Exception as e:
                        print(f"[YOUTUBE] yt-dlp info extraction failed: {e}")

                    # Use extracted metadata
                    formatted_content = f"""
                            **YouTube Video Analysis**

                            **Title:** {title}
                            **Author:** {author}
                            **Video URL:** {youtube_url}
                            {f"**Duration:** {duration} seconds" if duration else ""}

                            **Transcript:**
                            {transcript}
                            """
                    return {
                        "url": youtube_url,
                        "video_id": video_id,
                        "title": title,
                        "content": formatted_content,
                        "transcript_raw": transcript,
                        "metadata": {
                            "scraped_at": datetime.now(timezone.utc).isoformat(),
                            "scraping_method": "youtube_transcript_api_fallback",
                            "author": author,
                            "video_id": video_id,
                            "duration": duration,
                            "content_length": len(transcript),
                        },
                    }
                else:
                    return {
                        "url": youtube_url,
                        "video_id": video_id,
                        "title": "YouTube Video",
                        "content": "Unable to access this video - YouTube is blocking requests from cloud servers",
                        "error": "youtube_ip_blocked",
                        "metadata": {
                            "scraped_at": datetime.now(timezone.utc).isoformat(),
                            "scraping_method": "blocked_by_youtube",
                            "note": "YouTube blocks most cloud provider IPs (AWS, Google Cloud, Azure) to prevent automated access. This affects both audio download and transcript extraction.",
                        },
                    }

            # Transcribe audio - run in a new thread to avoid event loop conflicts
            import concurrent.futures

            def run_transcription():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    return loop.run_until_complete(self.transcribe_audio(audio_file))
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_transcription)
                transcript = future.result(timeout=300)  # 5 minute timeout

            if not transcript:
                return {
                    "url": youtube_url,
                    "video_id": video_id,
                    "title": metadata["title"],
                    "content": "Failed to transcribe audio from this video",
                    "error": "transcription_failed",
                    "metadata": {
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "scraping_method": "yt-dlp + whisper",
                        "author": metadata["author"],
                        "video_id": video_id,
                    },
                }

            # Format content with video info and transcript
            formatted_content = f"""
                        **YouTube Video Analysis**

                        **Title:** {metadata['title']}
                        **Author:** {metadata['author']}
                        **Video URL:** {youtube_url}
                        **Duration:** {metadata['duration']} seconds

                        **Transcript:**
                        {transcript}
                        """

            return {
                "url": youtube_url,
                "video_id": video_id,
                "title": metadata["title"],
                "content": formatted_content,
                "transcript_raw": transcript,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "yt-dlp + whisper",
                    "author": metadata["author"],
                    "video_id": video_id,
                    "duration": metadata["duration"],
                    "content_length": len(transcript),
                    "description": (
                        metadata["description"][:500] if metadata["description"] else ""
                    ),
                },
            }

        except Exception as e:
            print(f"[YOUTUBE] Error processing video: {e}")
            return None
        finally:
            # Cleanup temporary audio file
            if audio_file and os.path.exists(audio_file):
                try:
                    os.remove(audio_file)
                except:
                    pass
