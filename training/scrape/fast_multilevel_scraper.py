import asyncio
import json
import logging
import os
from queue import Queue
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse
import re
import requests

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from dotenv import load_dotenv
from services.redis_service import RedisService
from services.youtube_scrape_service import YouTubeScrapingClient
from training.scrape.helper import (
    _save_scrape_to_lancedb,
    _save_website_summary_to_db,
    _save_website_to_s3,
    generate_level_context,
)
from utils.fireworkzz import get_fireworks_response
from flask import jsonify

logger = logging.getLogger(__name__)
load_dotenv()


class FastMultilevelScraper:
    """Fast multi-level website scraper with concurrent processing"""

    def __init__(self, user_id: str, max_workers: int = 2):
        """
        Initialize scraper

        Args:
            user_id: User ID for logging
            max_workers: Number of concurrent scraping threads (default: 3)
        """
        self.user_id = user_id
        self.max_workers = max_workers
        self.timeout = 12  # Page load timeout in seconds (reduced for faster fallback)
        self.max_depth = 2  # Maximum 2 levels (homepage + subpages, no deeper)
        self.max_links_per_level = 4  # Maximum 4 links per level
        self.visited = set()  # Track visited URLs
        self.page_cache = {}  # Cache for page content to avoid re-scraping
        self.service = RedisService()
        # ---- NEW: Create driver pool ----
        self.driver_pool = Queue()

    def _borrow_driver(self):
        return self.driver_pool.get()

    def _return_driver(self, driver):
        self.driver_pool.put(driver)

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for duplicate detection (remove trailing slashes, query params, etc)"""
        parsed = urlparse(url)
        # Return normalized domain + path without query params or fragments
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        return normalized.lower()

    def _get_duplicate_key(self, url: str) -> str:
        """Get Redis key for storing scraping history"""
        normalized_url = self._normalize_url(url)
        return f"scraped_url:{self.user_id}:{normalized_url}"

    def _is_binary_or_non_html(self, url: str, html: str) -> bool:
        if re.search(
            r"\.(ico|png|jpg|jpeg|gif|svg|pdf|mp4|webm|css|js|woff2?)$",
            url,
            re.IGNORECASE,
        ):
            return True
        if len(html) < 200 and "<html" not in html.lower():
            return True
        if (
            html
            and sum(1 for ch in html[:200] if ord(ch) < 32 and ch not in "\n\t") > 5
        ):
            return True
        return False

    def _looks_unnatural(self, text: str) -> bool:
        if not text:
            return True
        ratio = len(re.findall(r"[^a-zA-Z0-9\s.,!?]", text)) / max(len(text), 1)
        if ratio > 0.30:
            return True
        if text.count(" ") / max(len(text), 1) < 0.05:
            return True
        for w in text.split():
            if len(w) > 40:
                return True
        return False

    async def check_duplicate_scrape(self, url: str) -> Optional[Dict]:
        """
        Check if user has already scraped this URL recently

        Returns:
            Dict with duplicate info if found, None otherwise
        """
        try:
            cache_key = self._get_duplicate_key(url)
            # cached_result = redis_config_glide.get(cache_key)

            cached_result = await self.service.get(cache_key)

            if cached_result:
                logger.info(
                    f"[FAST] Duplicate URL detected for user {self.user_id}: {url}"
                )
                return json.loads(cached_result)

            return None
        except Exception as e:
            logger.warning(f"[FAST] Failed to check duplicate: {e}")
            return None

    async def clear_duplicate_scrape(self, url: str) -> bool:
        """Remove duplicate-scrape entry from Redis."""
        try:
            cache_key = self._get_duplicate_key(url)
            result = await self.service.delete(cache_key)
            # redis.delete returns number of keys removed (0 or 1)
            return result == 1
        except Exception as e:
            logger.error(f"[FAST] Failed to delete duplicate key: {e}")
            return False

    def store_scraped_url(self, url: str, result: Dict, ttl_hours: int = 24) -> bool:
        """
        Store URL in cache with expiration (24 hours by default)

        Args:
            url: The scraped URL
            result: The scraping result to cache
            ttl_hours: Time-to-live in hours

        Returns:
            True if stored successfully, False otherwise
        """
        try:
            cache_key = self._get_duplicate_key(url)
            # Store only essential info to avoid large cache entries
            cache_data = {
                "url": url,
                "title": result.get("title"),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "word_count": sum(
                    p.get("word_count", 0) for p in result.get("all_pages", [])
                ),
                "total_pages": result.get("metadata", {}).get("total_pages", 0),
            }

            # Set with expiration
            ttl_seconds = ttl_hours * 3600
            asyncio.run(
                self.service.set(cache_key, json.dumps(cache_data), ttl_seconds)
            )

            logger.info(
                f"[FAST] Stored scrape history for {url} (expires in {ttl_hours} hours)"
            )
            return True
        except Exception as e:
            logger.warning(f"[FAST] Failed to store scraped URL: {e}")
            return False

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

            chrome_options.add_argument("--lang=en-US")

            prefs = {
                "translate": {"enabled": True},
                "intl.accept_languages": "en,en_US",
            }
            chrome_options.add_experimental_option("prefs", prefs)

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

    def _extract_main_links(
        self, html: str, base_url: str, base_domain: str
    ) -> List[str]:
        """
        Extract main internal links (max 5) from page HTML
        Prioritizes links that are likely main content (based on href structure)
        """

        links = []
        link_scores = {}  # Score links by relevance

        # Extract all href values using regex - handle both single and double quotes
        href_pattern = r'href\s*=\s*["\']([^"\']+)["\']'
        for match in re.finditer(href_pattern, html, re.IGNORECASE):
            href = match.group(1)
            absolute_url = urljoin(base_url, href)

            # Basic URL validation
            if not absolute_url or absolute_url in self.visited:
                continue

            parsed = urlparse(absolute_url)

            # Only internal links, exclude non-HTML, fragments, queries
            if (
                parsed.netloc != base_domain
                or absolute_url.endswith(
                    (".pdf", ".jpg", ".png", ".gif", ".css", ".js")
                )
                or "#" in absolute_url.split("/")[-1]
            ):
                continue

            # Simple scoring based on URL structure
            score = self._score_link(absolute_url, base_url)
            link_scores[absolute_url] = score

        # Sort by score and take top 5
        sorted_links = sorted(link_scores.items(), key=lambda x: x[1], reverse=True)
        return [url for url, _ in sorted_links[: self.max_links_per_level]]

    def _score_link(self, absolute_url: str, base_url: str) -> float:
        """Score a link for relevance and importance"""
        score = 0.0

        # Score based on path depth (shallower = more important)
        path_depth = absolute_url.count("/") - base_url.count("/")
        if path_depth <= 2:
            score += 2.0
        elif path_depth <= 3:
            score += 1.5

        # Penalize query params and hashes
        if "?" not in absolute_url and "#" not in absolute_url:
            score += 0.5

        # Boost main content-like URLs
        main_keywords = [
            "about",
            "products",
            "services",
            "features",
            "pricing",
            "contact",
        ]
        if any(kw in absolute_url.lower() for kw in main_keywords):
            score += 1.5

        return score

    def _extract_content_from_html(self, html: str) -> Optional[str]:
        """Extract ONLY main content - aggressive filtering for clean summaries"""
        try:
            # Remove scripts, styles, comments
            html = re.sub(
                r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
            )
            html = re.sub(
                r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE
            )
            html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

            # Remove navigation, footer, header, sidebar
            html = re.sub(
                r"<(nav|footer|header|aside)[^>]*>.*?</\1>",
                "",
                html,
                flags=re.DOTALL | re.IGNORECASE,
            )

            # Remove div/section with classes/ids like nav/menu/sidebar/ad/banner/cookie/popup/modal/overlay
            html = re.sub(
                r'<(?:div|section)[^>]*(?:class|id)="[^"]*(?:nav|menu|sidebar|ad|banner|cookie|popup|modal|overlay)[^"]*"[^>]*>.*?</(?:div|section)>',
                "",
                html,
                flags=re.DOTALL | re.IGNORECASE,
            )

            # Remove tables (often cookie/legal info)
            html = re.sub(
                r"<table[^>]*>.*?</table>", "", html, flags=re.DOTALL | re.IGNORECASE
            )
            html = re.sub(
                r"<tbody[^>]*>.*?</tbody>", "", html, flags=re.DOTALL | re.IGNORECASE
            )

            # Extract main content if it exists
            main_content = re.search(
                r'<(?:main|article|[^>]*role=["\']main["\'][^>]*)>.*?</(?:main|article)>',
                html,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if main_content:
                html = main_content.group(0)

            # Remove all HTML tags
            text = re.sub(r"<[^>]+>", "\n", html)

            # Aggressive whitespace cleanup
            text = re.sub(r"\n\s*\n+", "\n", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = text.strip()

            # Remove junk lines
            lines = text.split("\n")
            filtered_lines = []
            junk_phrases = [
                "skip to",
                "sign in",
                "sign up",
                "log in",
                "subscribe",
                "newsletter",
                "cookie",
                "consent",
                "privacy",
                "terms",
                "policy",
                "advertisement",
                "sponsored",
                "ad:",
                "follow us",
                "twitter",
                "facebook",
                "instagram",
            ]
            for line in lines:
                line = line.strip()
                if not line or len(line) < 10:
                    continue
                if any(p in line.lower() for p in junk_phrases):
                    continue
                filtered_lines.append(line)

            text = "\n".join(filtered_lines)

            # --- Detect if text looks coded / non-human ---
            if not text or self._looks_unnatural(text):
                return None

            # Limit to first 1500 chars
            return text

        except Exception as e:
            logger.error(f"[FAST] Error extracting content: {e}")
            return None

    def _looks_unnatural(self, text: str) -> bool:
        """Detects encoded / gibberish / minified content"""
        if not text:
            return True
        # Too many symbols / non-alphanumeric
        symbol_ratio = len(re.findall(r"[^a-zA-Z0-9\s.,!?]", text)) / max(len(text), 1)
        if symbol_ratio > 0.30:
            return True
        # Very few spaces → minified
        if text.count(" ") / max(len(text), 1) < 0.05:
            return True
        # Very long single words → base64/hash
        for w in text.split():
            if len(w) > 40:
                return True
        return False

    def _scrape_page(self, url: str, depth: int = 0) -> Optional[Dict]:
        driver = None
        try:
            if url in self.visited:
                return None
            self.visited.add(url)
            logger.info(f"[FAST] Scraping level {depth}: {url}")

            driver = self._borrow_driver()
            driver.set_page_load_timeout(8)

            # Load page with Selenium
            try:
                logger.info(f"[FAST] Loading {url} with Selenium...")
                driver.get(url)
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                except:
                    pass
                try:
                    WebDriverWait(driver, 8).until(
                        EC.presence_of_all_elements_located(
                            (
                                By.CSS_SELECTOR,
                                "main, article, [role='main'], .content, .main-content",
                            )
                        )
                    )
                except:
                    pass
                time.sleep(3)
            except Exception as e:
                logger.warning(f"[FAST] Selenium load failed for {url}: {e}")
                return self._scrape_page_with_requests(url, depth)

            # Get HTML content
            html_content = driver.page_source

            # Skip non-HTML / binary files
            if self._is_binary_or_non_html(url, html_content):
                logger.info(f"[FAST] Skipping non-HTML content: {url}")
                # return {
                #     "url": url,
                #     "title": "Non-HTML Content",
                #     "content": "",
                #     "word_count": 0,
                #     "depth": depth,
                #     "links": [],
                # }
                return None

            # JS error placeholder check
            if (
                "doesn't work properly without JavaScript" in html_content
                or "enable JavaScript" in html_content.lower()
            ):
                logger.warning(f"[FAST] JS Error page, retrying for {url}")
                time.sleep(3)
                html_content = driver.page_source

            # Extract title
            title_match = re.search(
                r"<title[^>]*>([^<]+)</title>", html_content, re.IGNORECASE
            )
            title = title_match.group(1).strip() if title_match else "No Title"

            # Extract content
            content = self._extract_content_from_html(html_content)

            if not content:
                logger.warning(f"[FAST] No content for {url}, fallback to requests")
                return self._scrape_page_with_requests(url, depth)

            # Build result
            page_data = {
                "url": url,
                "title": title,
                "content": content,
                "word_count": len(content.split()),
                "depth": depth,
                "links": [],
            }

            # Extract links if depth allows
            if depth < self.max_depth - 1:
                base_domain = urlparse(url).netloc
                links = self._extract_main_links(html_content, url, base_domain)
                page_data["links"] = links

            logger.info(
                f"[FAST] ✅ Scraped with Selenium: {title} ({page_data['word_count']} words)"
            )
            return page_data

        except Exception as e:
            logger.error(f"[FAST] Unexpected error scraping {url}: {e}")
            return None
        finally:
            if driver:
                self._return_driver(driver)

    def _scrape_page_with_requests(self, url: str, depth: int = 0) -> Optional[Dict]:
        """Fallback scraping method using requests library for faster, simpler extraction"""
        try:
            logger.info(f"[FAST] Using requests fallback for {url}")

            # Create a session to maintain cookies and connections
            session = requests.Session()

            # Parse the base domain for referer
            parsed_url = urlparse(url)
            base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "en-US,en;q=0.9,en-GB;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
                "Referer": base_domain + "/",
                "Priority": "u=0, i",
            }

            # Add default cookies to session
            session.cookies.update(
                {
                    "Path": "/",
                    "Domain": parsed_url.netloc,
                }
            )

            try:
                # First request to establish session/cookies
                response = session.get(
                    url, headers=headers, timeout=15, allow_redirects=True, verify=True
                )
                response.raise_for_status()
            except requests.exceptions.HTTPError as e:
                # If we get 403, try with different user agent
                if e.response.status_code == 403:
                    logger.warning(
                        f"[FAST] Got 403, trying alternative User-Agent for {url}"
                    )
                    headers["User-Agent"] = (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                    response = session.get(
                        url,
                        headers=headers,
                        timeout=15,
                        allow_redirects=True,
                        verify=True,
                    )
                    response.raise_for_status()
                else:
                    raise

            html_content = response.content.decode("utf-8", errors="ignore")

            # Extract title using regex
            title_match = re.search(
                r"<title[^>]*>([^<]+)</title>", html_content, re.IGNORECASE
            )
            title = title_match.group(1).strip() if title_match else "No Title"

            # Extract content from HTML
            content = self._extract_content_from_html(html_content)

            # Accept any content
            if not content:
                logger.warning(f"[FAST] Requests got no content from {url}")
                return None

            page_data = {
                "url": url,
                "title": title,
                "content": content,
                "word_count": len(content.split()),
                "depth": depth,
                "links": [],
            }

            # Extract links for next level only if within depth limit
            if depth < self.max_depth - 1:
                base_domain = urlparse(url).netloc
                links = self._extract_main_links(html_content, url, base_domain)
                page_data["links"] = links

            logger.info(
                f"[FAST] ✅ Scraped with requests: {title} ({page_data['word_count']} words)"
            )
            return page_data

        except Exception as e:
            logger.warning(f"[FAST] Requests fallback also failed for {url}: {e}")
            return None

    def scrape_multilevel(self, url: str, level) -> Dict:
        """
        Scrape website with multiple levels using concurrent processing

        Returns structure with individual page data at each level
        """
        # Check if it's a YouTube URL and use old method if so
        if self._is_youtube_url(url):
            logger.info(f"[FAST] Detected YouTube URL, using legacy scraping method")
            return self._scrape_youtube_legacy(url)

        start_time = time.time()
        logger.info(f"[FAST] Starting multi-level scrape: {url}")

        result = {
            "url": url,
            "title": "",
            "content": "",
            "pages_by_level": {0: [], 1: [], 2: []},
            "all_pages": [],
            "metadata": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "total_pages": 0,
                "total_time_seconds": 0,
                "scraping_method": "fast_multilevel_concurrent",
                "max_depth": self.max_depth,
                "links_per_page": self.max_links_per_level,
            },
        }
        self.start_driver()

        # Level 0: Scrape homepage
        level_0_data = self._scrape_page(url, depth=0)
        if not level_0_data:
            logger.error(f"[FAST] Failed to scrape homepage for {url}")
            # Return error response instead of None
            return {
                "url": url,
                "title": "Failed to Scrape",
                "content": f"Unable to extract content from {url}. The website may be blocked, require authentication, or have anti-scraping measures.",
                "pages_by_level": {0: [], 1: [], 2: []},
                "all_pages": [],
                "status": "failed",
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "total_pages": 0,
                    "total_time_seconds": round(time.time() - start_time, 2),
                    "scraping_method": "fast_multilevel_failed",
                    "max_depth": self.max_depth,
                    "links_per_page": self.max_links_per_level,
                    "error": "homepage_extraction_failed",
                },
            }
            # return None

        result["pages_by_level"][0] = [level_0_data]
        result["all_pages"].append(level_0_data)
        result["title"] = level_0_data["title"]
        result["content"] = level_0_data["content"]
        result["contacts"] = "ALL"

        # Level 1: Scrape links from homepage
        if level_0_data["links"] and level > 1:
            logger.info(f"[FAST] Found {len(level_0_data['links'])} links at level 0")
            level_1_pages = self._scrape_pages_concurrent(
                level_0_data["links"], depth=1
            )
            result["pages_by_level"][1] = level_1_pages
            result["all_pages"].extend(level_1_pages)
            print("done with the level depth", level)

            if level > 2:
                # Level 2: Scrape links from level 1 pages
                level_2_links = []
                for page in level_1_pages:
                    level_2_links.extend(
                        page.get("links", [])[:3]
                    )  # Reduce links for speed

                if level_2_links:
                    logger.info(f"[FAST] Found {len(level_2_links)} links at level 1")
                    level_2_pages = self._scrape_pages_concurrent(
                        level_2_links, depth=2
                    )
                    result["pages_by_level"][2] = level_2_pages
                    result["all_pages"].extend(level_2_pages)
                    print("done with the level depth", level)

        # Update metadata
        result["metadata"]["total_pages"] = len(result["all_pages"])
        result["metadata"]["total_time_seconds"] = round(time.time() - start_time, 2)

        # Build AI-powered comprehensive summary
        result["content"] = self._generate_ai_summary(result)
        result["contacts"] = "All"

        logger.info(
            f"[FAST] ✅ Completed in {result['metadata']['total_time_seconds']}s "
            f"({result['metadata']['total_pages']} pages)"
        )

        # Store in cache for duplicate detection
        self.store_scraped_url(url, result)

        return result

    def _scrape_pages_concurrent(self, urls: List[str], depth: int) -> List[Dict]:
        """Scrape multiple pages concurrently"""
        pages = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._scrape_page, url, depth): url for url in urls
            }

            for future in as_completed(futures):
                try:
                    page_data = future.result(timeout=self.timeout + 5)
                    if page_data:
                        pages.append(page_data)
                except Exception as e:
                    url = futures[future]
                    logger.warning(f"[FAST] Failed to scrape {url}: {e}")

        return pages

    def _is_youtube_url(self, url: str) -> bool:
        """Check if the URL is a YouTube video URL"""
        youtube_patterns = [
            "youtube.com/watch",
            "youtu.be/",
            "youtube.com/embed/",
            "youtube.com/v/",
        ]
        url_lower = url.lower()
        return any(pattern in url_lower for pattern in youtube_patterns)

    def _scrape_youtube_legacy(self, url: str) -> Dict:
        """Use legacy YouTube scraping method (from routes.py)"""
        try:
            # Import the YouTubeScrapingClient class from routes

            scraper = YouTubeScrapingClient(user_id=self.user_id)
            result = scraper.scrape_youtube_video(url)

            if result and (result.get("transcript_raw") or result.get("content")):
                # Get the transcript content
                transcript = result.get("transcript_raw") or result.get("content", "")

                # Generate AI summary from the transcript
                ai_summary = self._generate_youtube_summary(
                    transcript, result.get("title", "YouTube Video")
                )

                # Return formatted result
                return {
                    "url": url,
                    "title": result.get("title", "YouTube Video"),
                    "content": ai_summary
                    or transcript,  # Use AI summary if available, fallback to transcript
                    "pages_by_level": {0: [], 1: [], 2: []},
                    "all_pages": [],
                    "metadata": {
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "total_pages": 1,
                        "total_time_seconds": 0,
                        "scraping_method": "youtube_video",
                        "max_depth": 1,
                        "links_per_page": 0,
                        "content_type": "video_transcript",
                    },
                }

            # If no transcript, return error
            if result:
                return result
            return None

        except Exception as e:
            logger.error(f"[FAST] YouTube legacy scraping failed: {e}")
            return {
                "url": url,
                "title": "YouTube Video",
                "content": f"Unable to scrape YouTube video: {str(e)}",
                "pages_by_level": {0: [], 1: [], 2: []},
                "all_pages": [],
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "total_pages": 0,
                    "total_time_seconds": 0,
                    "scraping_method": "youtube_legacy_failed",
                    "max_depth": 1,
                    "links_per_page": 0,
                    "error": str(e),
                },
            }

    def _generate_youtube_summary(self, transcript: str, title: str) -> Optional[str]:
        """Generate AI summary for YouTube video transcript"""
        try:
            # Limit transcript to avoid token limits
            transcript_limited = transcript[:3000]

            prompt = f"""Analyze the following YouTube video transcript and provide a comprehensive video analysis.

            Video Title: {title}

            Transcript:
            {transcript_limited}

            Please provide:
            1. **Summary** (2-3 sentences about the main content)
            2. **Main Purpose** (what the video is about and why)
            3. **Key Topics** (bullet points of main topics covered)
            4. **Target Audience** (who would find this video useful)
            5. **Key Takeaways** (important points viewers should remember)

            Keep the analysis concise, professional, and easy to understand. Format it nicely."""

            ai_response = get_fireworks_response(prompt, role="system")

            if ai_response:
                return ai_response.strip()
            return None

        except Exception as e:
            logger.warning(f"[FAST] YouTube AI summary generation failed: {e}")
            return None

    def _extract_key_sentences(self, text: str, num_sentences: int = 2) -> str:
        """Extract first N sentences from text for a concise, informative summary"""
        sentences = text.split(". ")
        if not sentences:
            return text[:150]

        key_sentences = []
        for sentence in sentences[:num_sentences]:
            sentence = sentence.strip()
            if sentence and len(sentence) > 10:  # Only include meaningful sentences
                key_sentences.append(sentence)

        result = ". ".join(key_sentences)
        if result and not result.endswith("."):
            result += "."
        return result[:200]  # Cap at 200 chars for safety

    # def _generate_ai_summary(self, result: Dict) -> str:
    #     """Generate AI summary using professional scrape_summary_prompt_template"""
    #     try:
    #         # print("scrape result", result)

    #         pages_by_level = result.get("pages_by_level", [])
    #         if not pages_by_level or not pages_by_level[0]:
    #             logger.warning("[FAST] No homepage content available for AI summary")
    #             return "No content available for summary"

    #         # Build full context from ALL pages at ALL levels
    #         full_context = ""
    #         for level_pages in pages_by_level:
    #             for page in level_pages:
    #                 content = page.get("content", "")
    #                 if content:
    #                     full_context += "\n" + content

    #         if not full_context.strip():
    #             logger.warning("[FAST] All pages are empty for summary generation")
    #             return "No content available for summary"

    #         url = result.get("url", "")
    #         title = result.get("title", "Website")

    #         logger.info(f"[FAST] Generating AI summary for: {title}")
    #         logger.debug(
    #             f"[FAST] Full content length: {len(full_context)} chars, URL: {url}"
    #         )

    #         # Load template
    #         from cust_helpers import pathconfig
    #         from utils.normal import load_yaml_file

    #         try:
    #             yaml_data = load_yaml_file(path=pathconfig.agent_template)
    #             prompt_template = yaml_data.get("scrape_summary_prompt_template")
    #         except Exception as e:
    #             logger.warning(f"[FAST] Failed to load agent_templates.yaml: {e}")
    #             prompt_template = None

    #         # Build prompt
    #         if prompt_template:
    #             full_prompt = prompt_template.format(
    #                 url=url, website_content=full_context
    #             )
    #             logger.debug("[FAST] Using scrape_summary_prompt_template from YAML")
    #         else:
    #             full_prompt = f"Summarize the following website:\n\nURL: {url}\n\nContent:\n{full_context}"

    #         logger.debug(
    #             f"[FAST] Sending prompt to AI (length: {len(full_prompt)} chars)"
    #         )
    #         ai_summary = get_fireworks_response(full_prompt, role="system")
    #         print("new summarty",ai_summary)

    #         # if not ai_summary or ai_summary.strip() == "":
    #         #     logger.warning(
    #         #         "[FAST] AI returned empty response, using fallback summary"
    #         #     )
    #         #     return self._compile_content_summary(result)

    #         logger.info(
    #             f"[FAST] AI summary generated successfully ({len(ai_summary)} chars)"
    #         )
    #         return ai_summary.strip()

    #     except Exception as e:
    #         logger.warning(f"[FAST] AI summary failed: {e}, using fallback summary")
    #         logger.debug(traceback.format_exc())
    #         return self._compile_content_summary(result)

    def _generate_ai_summary(self, result: Dict) -> str:
        """
        Robust AI summary generator. Defensive about input shapes to avoid
        'int' object is not iterable and similar errors.
        """
        try:
            logger.debug("[FAST] _generate_ai_summary invoked")
            logger.debug(f"[FAST] raw result keys: {list(result.keys())}")

            pages_by_level = result.get("pages_by_level", None)

            # Defensive checks and normalization
            if pages_by_level is None:
                logger.warning("[FAST] result missing 'pages_by_level'")
                return self._compile_content_summary(result)

            # If pages_by_level is a dict (level->list), convert to sorted list
            if isinstance(pages_by_level, dict):
                try:
                    # sort by numeric key if possible
                    sorted_items = sorted(
                        pages_by_level.items(),
                        key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else kv[0],
                    )
                    pages_by_level = [v for k, v in sorted_items]
                except Exception:
                    # fallback: just take values
                    pages_by_level = list(pages_by_level.values())

            # Ensure top-level is iterable (list/tuple)
            if not isinstance(pages_by_level, (list, tuple)):
                logger.warning(
                    f"[FAST] unexpected pages_by_level type: {type(pages_by_level)}"
                )
                return self._compile_content_summary(result)
            full_context = generate_level_context(pages_by_level)

            if not full_context:
                logger.warning(
                    "[FAST] full_context is empty after processing pages_by_level"
                )
                return self._compile_content_summary(result)

            url = result.get("url", "")
            title = result.get("title", "Website")

            logger.info(f"[FAST] Generating AI summary for: {title} ({url})")
            logger.debug(f"[FAST] full_context length: {len(full_context)} chars")

            # Load prompt template safely
            from cust_helpers import pathconfig
            from utils.normal import load_yaml_file

            prompt_template = None
            try:
                yaml_data = load_yaml_file(path=pathconfig.agent_template)
                prompt_template = yaml_data.get("scrape_summary_prompt_template")
            except Exception as e:
                logger.debug(f"[FAST] could not load prompt template: {e}")

            if prompt_template:
                full_prompt = prompt_template.format(
                    url=url, website_content=full_context
                )
            else:
                full_prompt = f"Summarize the following website (URL: {url}). Combine facts across pages and stay strictly within content:\n\n{full_context}"

            logger.debug(f"[FAST] prompt length: {len(full_prompt)} chars")

            # Send to AI (wrap call in try)
            try:
                ai_summary = get_fireworks_response(full_prompt, role="system")
            except Exception as e:
                logger.warning(f"[FAST] get_fireworks_response failed: {e}")
                return self._compile_content_summary(result)

            if (
                not ai_summary
                or not isinstance(ai_summary, str)
                or not ai_summary.strip()
            ):
                logger.warning(
                    "[FAST] AI returned empty or non-string response, using fallback"
                )
                return self._compile_content_summary(result)

            logger.info(f"[FAST] ✅ AI summary generated ({len(ai_summary)} chars)")
            return ai_summary.strip()

        except Exception as e:
            # Log full diagnostic info to help debug 'int' or other shape errors
            logger.warning(f"[FAST] AI summary failed: {e}, using fallback summary")
            try:
                import traceback

                logger.debug(traceback.format_exc())
                logger.debug(f"[FAST] result repr: {repr(result)[:5000]}")
            except Exception:
                pass
            return self._compile_content_summary(result)

    def _compile_content_summary(self, result: Dict) -> str:
        """Fallback: Compile content from all pages into an organized summary"""
        try:
            summary = f"**{result.get('title', 'Website')}**\n\n"

            # Homepage summary - more detailed (first 200 chars of content)
            homepage = result["pages_by_level"][0]
            if homepage and homepage[0].get("content"):
                page = homepage[0]
                summary += f"**Overview:**\n"
                # Use first portion of extracted content
                content_preview = page["content"][:500]  # First 500 chars
                if content_preview:
                    summary += f"{content_preview}\n\n"

            # Level 1 pages - concise
            level_1_pages = result["pages_by_level"][1]
            if level_1_pages:
                summary += f"**Key Sections ({len(level_1_pages)} pages):**\n"
                for page in level_1_pages[:5]:  # Limit to 5 pages
                    title = page.get("title", "Untitled")
                    # Get first 100 chars of content for each page
                    content_preview = page.get("content", "")[:100]
                    if content_preview:
                        summary += f"• **{title}**: {content_preview}...\n"
                    else:
                        summary += f"• {title}\n"
                summary += "\n"

            # Level 2 pages - just titles
            level_2_pages = result["pages_by_level"][2]
            if level_2_pages:
                summary += f"**Related Topics ({len(level_2_pages)} pages):**\n"
                for page in level_2_pages[:10]:  # Limit to first 10
                    title = page.get("title", "Untitled")
                    summary += f"• {title}\n"

            return summary.strip()

        except Exception as e:
            logger.error(f"[FAST] Fallback summary generation failed: {e}")
            # Last resort: return raw homepage content
            try:
                homepage = result["pages_by_level"][0]
                if homepage and homepage[0].get("content"):
                    return homepage[0]["content"][:1000]
            except:
                pass

            return f"Website: {result.get('title', 'Unknown')}"

    def _generate_structured_summary(self, result: Dict) -> str:
        """Fallback structured summary when AI analysis fails"""
        summary = f"**{result['title']}**\n\n"

        # Homepage summary - more detailed (3 sentences)
        homepage = result["pages_by_level"][0]
        if homepage:
            page = homepage[0]
            summary += f"**Overview:**\n"
            key_content = self._extract_key_sentences(page["content"], num_sentences=3)
            summary += f"{key_content}\n\n"

        # Level 1 pages - concise (1 sentence each)
        level_1_pages = result["pages_by_level"][1]
        if level_1_pages:
            summary += f"**Key Sections ({len(level_1_pages)} pages):**\n"
            for page in level_1_pages:
                key_content = self._extract_key_sentences(
                    page["content"], num_sentences=1
                )
                summary += f"• **{page['title']}**: {key_content}\n"
            summary += "\n"

        # Level 2 pages - just titles
        level_2_pages = result["pages_by_level"][2]
        if level_2_pages:
            summary += f"**Related Topics ({len(level_2_pages)} pages):**\n"
            for page in level_2_pages[:10]:  # Limit to first 10 to avoid clutter
                summary += f"• {page['title']}\n"

        return summary.strip()


def scrape_website_fast(url: str, user_id: str, level) -> Optional[Dict]:
    """
    Convenience function to scrape a website using fast multilevel scraper

    Args:
        url: Website URL to scrape
        user_id: User ID (for logging)

    Returns:
        Scraping result dict or None on failure
    """
    scraper = FastMultilevelScraper(user_id=user_id, max_workers=3)

    # Check for duplicate
    duplicate = asyncio.run(scraper.check_duplicate_scrape(url))
    if duplicate:
        logger.info(f"[FAST] Returning cached result for duplicate URL: {url}")
        return {
            "url": url,
            "title": duplicate.get("title", "Website"),
            "content": f"⚠️ **Duplicate Scrape Detected**\n\nYou already scraped this website on {duplicate.get('scraped_at', 'N/A')}.\n\n**Website**: {duplicate.get('title', 'N/A')}\n**Total Pages Scraped**: {duplicate.get('total_pages', 0)}\n**Total Content**: {duplicate.get('word_count', 0)} words\n\nIf you'd like to re-scrape, please try again after 24 hours when the cache expires.",
            "is_duplicate": True,
            "duplicate_info": duplicate,
            "pages_by_level": {0: [], 1: [], 2: []},
            "all_pages": [],
            "metadata": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "total_pages": 0,
                "total_time_seconds": 0,
                "scraping_method": "fast_multilevel_duplicate_detected",
                "max_depth": 2,
                "links_per_page": 4,
                "original_scrape_time": duplicate.get("scraped_at"),
            },
        }

    return scraper.scrape_multilevel(url, level)


def run_scrapper_links(url, user_id, level):
    # ---------- Background Saves: S3 + MySQL + LanceDB ----------
    from threading import Thread

    # ---- Perform fast scrape ----
    scraped_data = scrape_website_fast(url, user_id, level)

    if not scraped_data:
        return jsonify({"error": "Failed to scrape website"}), 500

    scraping_failure = False
    if scraped_data and scraped_data.get("content") == "":
        scraping_failure = True

    if not scraping_failure:
        is_duplicate = scraped_data.get("is_duplicate", False)
        is_youtube = (
            scraped_data.get("metadata", {}).get("scraping_method") == "youtube_video"
        )
        has_error = scraped_data.get("metadata", {}).get("error") is not None

        # ---------- Response Construction ----------
        if is_duplicate:
            response_data = {
                "status": "duplicate_detected",
                "url": scraped_data["url"],
                "title": scraped_data.get("title", "Website"),
                "message": "This website was already scraped recently",
                "homepage_summary": scraped_data.get("content", ""),
                "duplicate_info": scraped_data.get("duplicate_info", {}),
                "cache_expires_at": scraped_data.get("metadata", {}).get(
                    "original_scrape_time"
                ),
                "retry_after_hours": 24,
                "pages_by_level": {},
                "total_pages": 0,
                "scraping_time": 0,
            }
            return jsonify(response_data), 409

        elif is_youtube:
            response_data = {
                "status": "success",
                "url": scraped_data["url"],
                "title": scraped_data["title"],
                "content_type": "video",
                "video_summary": scraped_data.get("content", ""),
                "total_pages": 1,
                "scraping_time": 0,
                "metadata": scraped_data.get("metadata", {}),
            }

        else:  # Website
            if has_error:
                response_data = {
                    "status": "partial_failure",
                    "url": scraped_data["url"],
                    "title": scraped_data.get("title", "Failed to Scrape"),
                    "homepage_summary": scraped_data.get(
                        "content", "Unable to extract content from this website"
                    ),
                    "pages_by_level": {},
                    "total_pages": 0,
                    "scraping_time": scraped_data["metadata"].get(
                        "total_time_seconds", 0
                    ),
                    "error": scraped_data["metadata"].get("error", "unknown_error"),
                }
            else:
                response_data = {
                    "status": "success",
                    "url": scraped_data["url"],
                    "title": scraped_data["title"],
                    "homepage_summary": (
                        scraped_data["pages_by_level"][0][0]["content"]
                        if scraped_data["pages_by_level"][0]
                        else ""
                    ),
                    "pages_by_level": {},
                    "total_pages": scraped_data["metadata"]["total_pages"],
                    "scraping_time": scraped_data["metadata"]["total_time_seconds"],
                }

                # Reformat pages
                for level in range(3):
                    pages = scraped_data["pages_by_level"][level]
                    response_data["pages_by_level"][str(level)] = [
                        {
                            "url": page["url"],
                            "title": page["title"],
                            "summary": page["content"],
                            "word_count": page["word_count"],
                            "depth": page["depth"],
                            "has_sublinks": len(page.get("links", [])) > 0,
                        }
                        for page in pages
                    ]

        Thread(
            target=_save_scrape_to_lancedb,
            args=(user_id, scraped_data),
            daemon=True,
        ).start()
        Thread(
            target=_save_website_summary_to_db,
            args=(user_id, url, scraped_data),
            daemon=True,
        ).start()

    Thread(
        target=_save_website_to_s3,
        args=(user_id, url, scraped_data),
        daemon=True,
    ).start()

    return response_data
