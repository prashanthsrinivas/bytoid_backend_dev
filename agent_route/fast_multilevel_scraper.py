import asyncio
import json
import logging
import os
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from dotenv import load_dotenv
from utils.fireworkzz import get_fireworks_response
from utils.redis_config import redis_config_glide

logger = logging.getLogger(__name__)
load_dotenv()


class FastMultilevelScraper:
    """Fast multi-level website scraper with concurrent processing"""

    def __init__(self, user_id: str, max_workers: int = 3):
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

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for duplicate detection (remove trailing slashes, query params, etc)"""
        parsed = urlparse(url)
        # Return normalized domain + path without query params or fragments
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip('/')
        return normalized.lower()

    def _get_duplicate_key(self, url: str) -> str:
        """Get Redis key for storing scraping history"""
        normalized_url = self._normalize_url(url)
        return f"scraped_url:{self.user_id}:{normalized_url}"

    def check_duplicate_scrape(self, url: str) -> Optional[Dict]:
        """
        Check if user has already scraped this URL recently
        
        Returns:
            Dict with duplicate info if found, None otherwise
        """
        try:
            cache_key = self._get_duplicate_key(url)
            cached_result = redis_config_glide.get(cache_key)
            
            if cached_result:
                logger.info(f"[FAST] Duplicate URL detected for user {self.user_id}: {url}")
                return json.loads(cached_result)
            
            return None
        except Exception as e:
            logger.warning(f"[FAST] Failed to check duplicate: {e}")
            return None

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
                "word_count": sum(p.get("word_count", 0) for p in result.get("all_pages", [])),
                "total_pages": result.get("metadata", {}).get("total_pages", 0),
            }
            
            # Set with expiration
            ttl_seconds = ttl_hours * 3600
            redis_config_glide.setex(cache_key, ttl_seconds, json.dumps(cache_data))
            
            logger.info(f"[FAST] Stored scrape history for {url} (expires in {ttl_hours} hours)")
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
            chrome_options.add_argument("--disable-images")  # Disable image loading for speed
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
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)

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

    def _extract_main_links(
        self, html: str, base_url: str, base_domain: str
    ) -> List[str]:
        """
        Extract main internal links (max 5) from page HTML
        Prioritizes links that are likely main content (based on href structure)
        """
        import re
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
                or absolute_url.endswith((".pdf", ".jpg", ".png", ".gif", ".css", ".js"))
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

    def _extract_content_from_html(self, html: str) -> str:
        """Extract ONLY main content - aggressive filtering for clean summaries"""
        try:
            import re
            
            # Remove all the junk first
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
            
            # Remove navigation, menu, footer, header, sidebar - very aggressively
            html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<aside[^>]*>.*?</aside>', '', html, flags=re.DOTALL | re.IGNORECASE)
            
            # Remove ANY div/section with class containing: nav, menu, sidebar, ad, banner, cookie, popup, modal
            html = re.sub(r'<(?:div|section)[^>]*class="[^"]*(?:nav|menu|sidebar|ad|banner|cookie|popup|modal|overlay)[^"]*"[^>]*>.*?</(?:div|section)>', '', html, flags=re.DOTALL | re.IGNORECASE)
            
            # Remove divs with ID containing similar keywords
            html = re.sub(r'<(?:div|section)[^>]*id="[^"]*(?:nav|menu|sidebar|ad|banner|cookie|popup|modal)[^"]*"[^>]*>.*?</(?:div|section)>', '', html, flags=re.DOTALL | re.IGNORECASE)
            
            # Remove tables (often cookie/legal info)
            html = re.sub(r'<table[^>]*>.*?</table>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<tbody[^>]*>.*?</tbody>', '', html, flags=re.DOTALL | re.IGNORECASE)
            
            # Extract main content area if it exists
            main_content = re.search(r'<(?:main|article|[^>]*role=["\']main["\'][^>]*)>.*?</(?:main|article)>', html, flags=re.DOTALL | re.IGNORECASE)
            if main_content:
                html = main_content.group(0)
            
            # Remove all HTML tags
            text = re.sub(r'<[^>]+>', '\n', html)
            
            # Clean whitespace extremely aggressively
            text = re.sub(r'\n\s*\n+', '\n', text)  # Remove multiple blank lines
            text = re.sub(r'[ \t]+', ' ', text)     # Remove extra spaces/tabs
            text = text.strip()
            
            # Remove lines that are obviously junk (very short or navigation items)
            lines = text.split('\n')
            filtered_lines = []
            for line in lines:
                line = line.strip()
                # Skip empty lines and very short lines
                if not line or len(line) < 10:
                    continue
                # Skip common junk patterns
                if any(phrase in line.lower() for phrase in [
                    'skip to', 'sign in', 'sign up', 'log in', 'subscribe', 'newsletter',
                    'cookie', 'consent', 'privacy', 'terms', 'policy', 'advertisement',
                    'sponsored', 'ad:', 'follow us', 'twitter', 'facebook', 'instagram'
                ]):
                    continue
                filtered_lines.append(line)
            
            text = '\n'.join(filtered_lines)
            
            # LIMIT to first 1500 chars - this gives AI enough context but keeps it clean
            return text[:1500] if text else ""
        
        except Exception as e:
            logger.error(f"[FAST] Error extracting content: {e}")
            return ""

    def _scrape_page(self, url: str, depth: int = 0) -> Optional[Dict]:
        """
        Scrape a single page using Selenium for full JavaScript rendering
        Extract raw HTML and send to AI for summarization
        
        Returns dict with: url, title, content, links (for next level)
        """
        driver = None
        try:
            if url in self.visited:
                return None

            self.visited.add(url)

            logger.info(f"[FAST] Scraping level {depth}: {url}")
            
            # Use Selenium for everything - better for dynamic content
            driver = self._setup_selenium_driver()
            driver.set_page_load_timeout(8)
            
            try:
                logger.info(f"[FAST] Loading {url} with Selenium...")
                driver.get(url)
                
                # Wait for body to load with proper timeout for JS-heavy sites
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                except:
                    pass
                
                # Wait for common content containers to load (main, article, content div, etc)
                try:
                    WebDriverWait(driver, 8).until(
                        EC.presence_of_all_elements_located((By.CSS_SELECTOR, "main, article, [role='main'], .content, .main-content"))
                    )
                except:
                    pass
                
                # Give page more time to render JavaScript content (increase to 3 seconds for JS-heavy sites)
                time.sleep(3)
                
            except Exception as e:
                logger.warning(f"[FAST] Selenium load failed for {url}: {str(e)[:80]}")
                # Fallback to requests as last resort
                return self._scrape_page_with_requests(url, depth)

            html_content = driver.page_source

            # Check if we got a JavaScript error page
            if "doesn't work properly without JavaScript" in html_content or "enable JavaScript" in html_content.lower():
                logger.warning(f"[FAST] Got JS error page, retrying with more wait time for {url}")
                # Wait longer for content to load
                time.sleep(3)
                html_content = driver.page_source
            
            # Extract title using regex
            import re
            title_match = re.search(r'<title[^>]*>([^<]+)</title>', html_content, re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else "No Title"

            # Extract content from HTML
            content = self._extract_content_from_html(html_content)

            # Accept any content we get
            if not content:
                logger.warning(f"[FAST] Got no content from {url}, trying requests fallback")
                return self._scrape_page_with_requests(url, depth)

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

            logger.info(f"[FAST] ✅ Scraped with Selenium: {title} ({page_data['word_count']} words)")
            return page_data

        except Exception as e:
            logger.error(f"[FAST] Unexpected error scraping {url}: {str(e)[:100]}")
            return None
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass

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
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9,en-GB;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
                'Referer': base_domain + '/',
                'Priority': 'u=0, i',
            }
            
            # Add default cookies to session
            session.cookies.update({
                'Path': '/',
                'Domain': parsed_url.netloc,
            })
            
            try:
                # First request to establish session/cookies
                response = session.get(url, headers=headers, timeout=15, allow_redirects=True, verify=True)
                response.raise_for_status()
            except requests.exceptions.HTTPError as e:
                # If we get 403, try with different user agent
                if e.response.status_code == 403:
                    logger.warning(f"[FAST] Got 403, trying alternative User-Agent for {url}")
                    headers['User-Agent'] = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    response = session.get(url, headers=headers, timeout=15, allow_redirects=True, verify=True)
                    response.raise_for_status()
                else:
                    raise
            
            html_content = response.content.decode('utf-8', errors='ignore')
            
            # Extract title using regex
            import re
            title_match = re.search(r'<title[^>]*>([^<]+)</title>', html_content, re.IGNORECASE)
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
            
            logger.info(f"[FAST] ✅ Scraped with requests: {title} ({page_data['word_count']} words)")
            return page_data
            
        except Exception as e:
            logger.warning(f"[FAST] Requests fallback also failed for {url}: {e}")
            return None

    def scrape_multilevel(self, url: str) -> Dict:
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

        result["pages_by_level"][0] = [level_0_data]
        result["all_pages"].append(level_0_data)
        result["title"] = level_0_data["title"]
        result["content"] = level_0_data["content"]

        # Level 1: Scrape links from homepage
        if level_0_data["links"]:
            logger.info(f"[FAST] Found {len(level_0_data['links'])} links at level 0")
            level_1_pages = self._scrape_pages_concurrent(level_0_data["links"], depth=1)
            result["pages_by_level"][1] = level_1_pages
            result["all_pages"].extend(level_1_pages)

            # Level 2: Scrape links from level 1 pages
            level_2_links = []
            for page in level_1_pages:
                level_2_links.extend(page.get("links", [])[:3])  # Reduce links for speed

            if level_2_links:
                logger.info(f"[FAST] Found {len(level_2_links)} links at level 1")
                level_2_pages = self._scrape_pages_concurrent(level_2_links, depth=2)
                result["pages_by_level"][2] = level_2_pages
                result["all_pages"].extend(level_2_pages)

        # Update metadata
        result["metadata"]["total_pages"] = len(result["all_pages"])
        result["metadata"]["total_time_seconds"] = round(time.time() - start_time, 2)

        # Build AI-powered comprehensive summary
        result["content"] = self._generate_ai_summary(result)

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
            'youtube.com/watch',
            'youtu.be/',
            'youtube.com/embed/',
            'youtube.com/v/',
        ]
        url_lower = url.lower()
        return any(pattern in url_lower for pattern in youtube_patterns)

    def _scrape_youtube_legacy(self, url: str) -> Dict:
        """Use legacy YouTube scraping method (from routes.py)"""
        try:
            # Import the YouTubeScrapingClient class from routes
            from agent_route.routes import YouTubeScrapingClient
            
            scraper = YouTubeScrapingClient(user_id=self.user_id)
            result = scraper.scrape_youtube_video(url)
            
            if result and (result.get("transcript_raw") or result.get("content")):
                # Get the transcript content
                transcript = result.get("transcript_raw") or result.get("content", "")
                
                # Generate AI summary from the transcript
                ai_summary = self._generate_youtube_summary(transcript, result.get("title", "YouTube Video"))
                
                # Return formatted result
                return {
                    "url": url,
                    "title": result.get("title", "YouTube Video"),
                    "content": ai_summary or transcript,  # Use AI summary if available, fallback to transcript
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
        sentences = text.split('. ')
        if not sentences:
            return text[:150]
        
        key_sentences = []
        for sentence in sentences[:num_sentences]:
            sentence = sentence.strip()
            if sentence and len(sentence) > 10:  # Only include meaningful sentences
                key_sentences.append(sentence)
        
        result = '. '.join(key_sentences)
        if result and not result.endswith('.'):
            result += '.'
        return result[:200]  # Cap at 200 chars for safety

    def _generate_ai_summary(self, result: Dict) -> str:
        """Generate AI summary using the professional scrape_summary_prompt_template"""
        try:
            # Just take homepage content and send to AI
            homepage = result["pages_by_level"][0]
            if not homepage or not homepage[0].get('content'):
                logger.warning("[FAST] No homepage content available for AI summary")
                return "No content available for summary"
            
            content = homepage[0]['content']
            url = result.get('url', '')
            title = result.get('title', 'Website')
            
            logger.info(f"[FAST] Generating AI summary for: {title}")
            logger.debug(f"[FAST] Content length: {len(content)} chars, URL: {url}")
            
            # Load the professional scrape_summary_prompt_template from agent_templates.yaml
            from cust_helpers import pathconfig
            from utils.normal import load_yaml_file
            
            try:
                yaml_data = load_yaml_file(path=pathconfig.agent_template)
                prompt_template = yaml_data.get("scrape_summary_prompt_template")
                if not prompt_template:
                    logger.warning("[FAST] scrape_summary_prompt_template not found in agent_templates.yaml, using fallback")
                    prompt_template = None
            except Exception as e:
                logger.warning(f"[FAST] Failed to load agent_templates.yaml: {e}, using fallback")
                prompt_template = None
            
            # Use template if available, otherwise use default
            if prompt_template:
                # Format the template with actual content
                full_prompt = prompt_template.format(
                    url=url,
                    website_content=content
                )
                logger.debug(f"[FAST] Using scrape_summary_prompt_template from agent_templates.yaml")
            else:
                # Fallback to inline prompt if template not available
                full_prompt = f"""You are an expert content analyst. Create a SHORT, CONCISE summary of website content.

## CRITICAL RULES:
1. **CONCISE ONLY** - Keep total response to ~150-200 words max
2. **Extract SPECIFIC facts** - Product names, features, key details only
3. **NO generic filler** - No "provides services", "company that offers", etc.
4. **Use exact details** - Real names, specifications, actual information
5. **Accurate information only** - From the provided content ONLY

## INPUT:
- url: {url}
- website_content: {content}

## OUTPUT FORMAT (SHORT & CLEAN):
**What is this?**
1-2 sentences: Specific description with actual product/service names

**Key Information**
- [Specific item with brief detail - max 1 line each]
- [Specific item with brief detail]
- [Specific item with brief detail]

**Related Sections**
- [Section name and what it covers - brief]
- [Section name and what it covers - brief]

## CRITICAL REMINDERS:
- CONCISE: 1-2 lines per bullet point, NOT paragraphs
- SPECIFIC: Use exact product/service names from content
- NO fluff: Skip generic descriptions
- Keep it clean: No formatting artifacts or extra text
- Extract REAL details from content, NOT generic descriptions
- Do NOT write "a website that provides..." or "an online platform that offers..."
- DO write specific product/service names with details (e.g., "HubSpot Content Hub with drag-and-drop editor and built-in SEO tools")
- Keep language professional but direct
- Balance detail and conciseness in each section"""
                logger.debug(f"[FAST] Using fallback inline prompt")
            
            # Get AI summary with system role for better instruction following
            logger.debug(f"[FAST] Sending prompt to AI (length: {len(full_prompt)} chars)")
            ai_summary = get_fireworks_response(full_prompt, role="system")
            
            if not ai_summary or ai_summary.strip() == "":
                logger.warning(f"[FAST] AI returned empty response, using fallback")
                return self._compile_content_summary(result)
            
            logger.info(f"[FAST] ✅ AI summary generated successfully ({len(ai_summary)} chars)")
            return ai_summary.strip()
            
        except Exception as e:
            logger.warning(f"[FAST] AI summary failed: {e}, using fallback summary")
            logger.debug(f"[FAST] Error traceback: {traceback.format_exc()}")
            # Fallback to structured summary without AI
            return self._compile_content_summary(result)

    def _compile_content_summary(self, result: Dict) -> str:
        """Fallback: Compile content from all pages into an organized summary"""
        try:
            summary = f"**{result.get('title', 'Website')}**\n\n"
            
            # Homepage summary - more detailed (first 200 chars of content)
            homepage = result["pages_by_level"][0]
            if homepage and homepage[0].get('content'):
                page = homepage[0]
                summary += f"**Overview:**\n"
                # Use first portion of extracted content
                content_preview = page['content'][:500]  # First 500 chars
                if content_preview:
                    summary += f"{content_preview}\n\n"
            
            # Level 1 pages - concise
            level_1_pages = result["pages_by_level"][1]
            if level_1_pages:
                summary += f"**Key Sections ({len(level_1_pages)} pages):**\n"
                for page in level_1_pages[:5]:  # Limit to 5 pages
                    title = page.get('title', 'Untitled')
                    # Get first 100 chars of content for each page
                    content_preview = page.get('content', '')[:100]
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
                    title = page.get('title', 'Untitled')
                    summary += f"• {title}\n"
            
            return summary.strip()
        
        except Exception as e:
            logger.error(f"[FAST] Fallback summary generation failed: {e}")
            # Last resort: return raw homepage content
            try:
                homepage = result["pages_by_level"][0]
                if homepage and homepage[0].get('content'):
                    return homepage[0]['content'][:1000]
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
            key_content = self._extract_key_sentences(page['content'], num_sentences=3)
            summary += f"{key_content}\n\n"
        
        # Level 1 pages - concise (1 sentence each)
        level_1_pages = result["pages_by_level"][1]
        if level_1_pages:
            summary += f"**Key Sections ({len(level_1_pages)} pages):**\n"
            for page in level_1_pages:
                key_content = self._extract_key_sentences(page['content'], num_sentences=1)
                summary += f"• **{page['title']}**: {key_content}\n"
            summary += "\n"
        
        # Level 2 pages - just titles
        level_2_pages = result["pages_by_level"][2]
        if level_2_pages:
            summary += f"**Related Topics ({len(level_2_pages)} pages):**\n"
            for page in level_2_pages[:10]:  # Limit to first 10 to avoid clutter
                summary += f"• {page['title']}\n"
        
        return summary.strip()


def scrape_website_fast(url: str, user_id: str) -> Optional[Dict]:
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
    duplicate = scraper.check_duplicate_scrape(url)
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
    
    return scraper.scrape_multilevel(url)
