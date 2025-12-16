from datetime import datetime, timezone
import time
import os
from urllib.parse import urljoin, urlparse
import requests
from dotenv import load_dotenv
from utils.fireworkzz import get_firework_embedding
from bs4 import BeautifulSoup
load_dotenv()

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
from collections import deque


class WebScrapingLanceClient:
    def __init__(self, user_id: str):
        load_dotenv()
        self.lancedb_url = os.getenv("LANCE_DB_IP")
        self.user_id = user_id
        self.dimension = 2880
        self.embeddings = get_firework_embedding()

    def _setup_selenium_driver(self):
        """Setup Chrome driver with appropriate options"""
        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )

            # Try different Chrome/Chromium paths
            chrome_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/snap/bin/chromium",
            ]

            for chrome_path in chrome_paths:
                if os.path.exists(chrome_path):
                    chrome_options.binary_location = chrome_path
                    print(f"[SELENIUM] Using Chrome binary: {chrome_path}")
                    break
            else:
                raise Exception(
                    "Chrome/Chromium binary not found. Please install Chrome or Chromium."
                )

            driver = webdriver.Chrome(options=chrome_options)
            # print("[SELENIUM] Chrome driver initialized successfully")
            return driver

        except Exception as e:
            print(f"[SELENIUM] Chrome driver setup failed: {e}")
            raise

    def _extract_internal_links(self, soup, base_url, base_domain):
        """Extract internal links from the same domain"""
        links = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            absolute_url = urljoin(base_url, href)

            # Only include links from same domain, exclude fragments and queries
            if (
                urlparse(absolute_url).netloc == base_domain
                and not absolute_url.endswith(
                    (".pdf", ".jpg", ".png", ".gif", ".css", ".js")
                )
                and "#" not in absolute_url.split("/")[-1]
            ):
                links.append(absolute_url)

        return list(set(links))  # Remove duplicates

    def _compile_multilevel_content(self, level_content):
        """Compile content from all levels into comprehensive summary"""
        compiled = f"**Website Overview:**\nThis analysis covers {sum(len(pages) for pages in level_content.values())} pages across {len([k for k, v in level_content.items() if v])} levels.\n\n"

        for level, pages in level_content.items():
            if not pages:
                continue

            compiled += f"**Level {level} ({'Homepage' if level == 0 else f'Sub-pages Level {level}'}):**\n"

            for page in pages:
                compiled += f"- **{page['title']}** ({page['word_count']} words): {page['content'][:200]}...\n"

            compiled += "\n"

        return compiled

    def scrape_website(self, url: str, use_selenium=True, max_depth=2, max_pages=20):
        """Main scraping method - can use either Selenium or requests"""
        if use_selenium:
            return self.scrape_website_multilevel_enhanced(url, max_depth, max_pages)
        else:
            return self._scrape_single_page_requests_enhanced(url)

    def _scrape_single_page_requests(self, url: str):
        """Robust single-page scraping method using requests"""
        try:
            print(f"[REQUESTS] Attempting to scrape: {url}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }

            # Make request with longer timeout
            response = requests.get(
                url, headers=headers, timeout=15, allow_redirects=True
            )
            print(f"[REQUESTS] Response status: {response.status_code}")
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            title = soup.find("title")
            title_text = title.get_text().strip() if title else "Scraped Website"
            content = self._extract_content_with_structure(soup)

            print(
                f"[REQUESTS] Successfully scraped: {title_text} ({len(content)} chars)"
            )

            return {
                "url": url,
                "title": title_text,
                "content": content,
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "requests_single_page",
                    "content_length": len(content),
                    "status_code": response.status_code,
                },
            }
        except Exception as e:
            print(f"[REQUESTS] Error scraping {url}: {e}")
            import traceback

            traceback.print_exc()
            return None

    def _extract_content_with_structure(self, soup, url=""):
        """Enhanced content extraction that preserves headings and structure"""

        # Remove unwanted elements
        for element in soup(["script", "style", "nav", "header", "footer", "aside"]):
            element.decompose()

        content_data = {
            "headings": [],
            "main_content": "",
            "meta_info": {},
            "structured_content": [],
        }

        # Extract meta information
        title_tag = soup.find("title")
        content_data["meta_info"]["title"] = (
            title_tag.get_text().strip() if title_tag else ""
        )

        meta_desc = soup.find("meta", attrs={"name": "description"})
        content_data["meta_info"]["description"] = (
            meta_desc.get("content", "") if meta_desc else ""
        )

        # Extract all headings with hierarchy
        headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        for heading in headings:
            heading_text = heading.get_text().strip()
            if heading_text:
                content_data["headings"].append(
                    {
                        "level": int(heading.name[1]),  # h1 -> 1, h2 -> 2, etc.
                        "text": heading_text,
                        "tag": heading.name,
                    }
                )

        # Extract structured content sections
        main_content_areas = soup.find_all(
            ["main", "article", "section", "div"],
            class_=lambda x: x
            and any(
                term in x.lower()
                for term in ["content", "main", "article", "body", "text"]
            ),
        )

        if not main_content_areas:
            main_content_areas = [soup.find("body")] if soup.find("body") else [soup]

        for area in main_content_areas:
            if area:
                # Extract paragraphs and lists
                paragraphs = area.find_all(["p", "div", "li"])
                for para in paragraphs[:20]:  # Limit to avoid too much content
                    text = para.get_text().strip()
                    if text and len(text) > 20:  # Filter out short/empty content
                        content_data["structured_content"].append(
                            {
                                "type": para.name,
                                "text": text[:300] + "..." if len(text) > 300 else text,
                            }
                        )

        # Compile main content
        all_text = soup.get_text()
        lines = (line.strip() for line in all_text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        content_data["main_content"] = " ".join(chunk for chunk in chunks if chunk)

        return content_data

    def scrape_website_multilevel_enhanced(
        self, url: str, max_depth: int = 3, max_pages: int = 50
    ):
        """Enhanced multi-level scraping with detailed structure extraction"""
        driver = None
        try:
            # Try Selenium first, fallback to requests
            try:
                driver = self._setup_selenium_driver()
            except Exception as selenium_error:
                print(f"[SELENIUM] Failed: {selenium_error}")
                # print("[FALLBACK] Using requests method")
                return self._scrape_single_page_requests_enhanced(url)

            scraped_data = {
                "url": url,
                "title": "",
                "content": "",
                "detailed_analysis": {
                    "total_pages": 0,
                    "levels": {},
                    "all_headings": [],
                    "site_structure": {},
                },
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "levels_scraped": {},
                    "total_pages": 0,
                    "scraping_method": "selenium_multilevel_enhanced",
                },
            }

            base_domain = urlparse(url).netloc
            visited = set()
            to_visit = deque([(url, 0)])
            pages_scraped = 0
            level_detailed_content = {i: [] for i in range(max_depth + 1)}

            while to_visit and pages_scraped < max_pages:
                current_url, depth = to_visit.popleft()

                if current_url in visited or depth > max_depth:
                    continue

                print(f"[ENHANCED] Scraping Level {depth}: {current_url}")

                try:
                    driver.get(current_url)
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                    time.sleep(2)

                    soup = BeautifulSoup(driver.page_source, "html.parser")

                    # Enhanced content extraction
                    content_data = self._extract_content_with_structure(
                        soup, current_url
                    )

                    page_analysis = {
                        "url": current_url,
                        "title": content_data["meta_info"]["title"],
                        "description": content_data["meta_info"]["description"],
                        "headings": content_data["headings"],
                        "main_content": content_data["main_content"][
                            :2000
                        ],  # Limit for storage
                        "structured_content": content_data["structured_content"][
                            :10
                        ],  # Top 10 sections
                        "word_count": len(content_data["main_content"].split()),
                        "heading_count": len(content_data["headings"]),
                        "content_type": self._classify_page_type(content_data),
                    }

                    level_detailed_content[depth].append(page_analysis)

                    # Collect all headings for site-wide analysis
                    for heading in content_data["headings"]:
                        heading["source_url"] = current_url
                        heading["level_depth"] = depth
                        scraped_data["detailed_analysis"]["all_headings"].append(
                            heading
                        )

                    # Extract links for next level
                    if depth < max_depth:
                        links = self._extract_internal_links(
                            soup, current_url, base_domain
                        )
                        for link in links[:8]:  # Limit links per page
                            if link not in visited:
                                to_visit.append((link, depth + 1))

                    visited.add(current_url)
                    pages_scraped += 1

                    print(
                        f"[ENHANCED] ✅ Analyzed: {page_analysis['title']} "
                        f"({page_analysis['word_count']} words, {page_analysis['heading_count']} headings)"
                    )

                except Exception as e:
                    print(f"[ENHANCED] ❌ Error analyzing {current_url}: {e}")
                    continue

            # Compile enhanced final content
            scraped_data["title"] = (
                level_detailed_content[0][0]["title"]
                if level_detailed_content[0]
                else "Website"
            )
            scraped_data["content"] = self._compile_enhanced_multilevel_content(
                level_detailed_content
            )

            # Enhanced metadata
            scraped_data["detailed_analysis"]["total_pages"] = pages_scraped
            scraped_data["detailed_analysis"]["levels"] = level_detailed_content
            scraped_data["detailed_analysis"]["site_structure"] = (
                self._analyze_site_structure(level_detailed_content)
            )

            scraped_data["metadata"]["levels_scraped"] = {
                f"level_{i}": len(pages)
                for i, pages in level_detailed_content.items()
                if pages
            }
            scraped_data["metadata"]["total_pages"] = pages_scraped

            return scraped_data

        except Exception as e:
            print(f"[ENHANCED] Fatal error: {e}")
            return None

        finally:
            if driver:
                driver.quit()

    def _classify_page_type(self, content_data):
        """Classify page type based on content and headings"""
        title = content_data["meta_info"]["title"].lower()
        headings_text = " ".join([h["text"].lower() for h in content_data["headings"]])

        if any(word in title for word in ["home", "welcome", "index"]):
            return "homepage"
        elif any(word in title for word in ["about", "company", "team"]):
            return "about_page"
        elif any(word in title for word in ["contact", "reach", "support"]):
            return "contact_page"
        elif any(
            word in headings_text for word in ["product", "service", "buy", "price"]
        ):
            return "product_page"
        elif any(word in headings_text for word in ["blog", "news", "article"]):
            return "blog_page"
        else:
            return "information_page"

    def _analyze_site_structure(self, level_content):
        """Analyze overall site structure and patterns"""
        structure_analysis = {
            "navigation_depth": len([k for k, v in level_content.items() if v]),
            "page_types_distribution": {},
            "common_headings": {},
            "content_patterns": [],
        }

        # Analyze page type distribution
        all_page_types = []
        for level, pages in level_content.items():
            for page in pages:
                page_type = page.get("content_type", "unknown")
                all_page_types.append(page_type)

        from collections import Counter

        structure_analysis["page_types_distribution"] = dict(Counter(all_page_types))

        # Find common heading patterns
        all_headings = []
        for level, pages in level_content.items():
            for page in pages:
                for heading in page.get("headings", []):
                    all_headings.append(heading["text"].lower())

        heading_counter = Counter(all_headings)
        structure_analysis["common_headings"] = dict(heading_counter.most_common(10))

        return structure_analysis

    def _compile_enhanced_multilevel_content(self, level_content):
        """Compile enhanced content with detailed structure analysis"""

        total_pages = sum(len(pages) for pages in level_content.values())
        active_levels = len([k for k, v in level_content.items() if v])

        compiled = f"**Website Overview:**\n"
        compiled += f"This comprehensive analysis covers {total_pages} pages across {active_levels} levels of depth. "
        compiled += f"The website structure reveals detailed content organization with specific headings and page classifications.\n\n"

        for level, pages in level_content.items():
            if not pages:
                continue

            level_name = "Homepage" if level == 0 else f"Sub-pages Level {level}"
            compiled += f"**Level {level} ({level_name}) - {len(pages)} pages:**\n"

            for page in pages:
                compiled += f"- **{page['title']}** ({page['content_type'].replace('_', ' ').title()}):\n"

                # Add exact headings found
                if page.get("headings"):
                    compiled += f"  * Key Headings: "
                    headings_by_level = {}
                    for h in page["headings"][:8]:  # Limit to top 8 headings
                        level_key = f"H{h['level']}"
                        if level_key not in headings_by_level:
                            headings_by_level[level_key] = []
                        headings_by_level[level_key].append(h["text"])

                    heading_summary = []
                    for h_level in sorted(headings_by_level.keys()):
                        heading_summary.append(
                            f"{h_level}: {', '.join(headings_by_level[h_level][:3])}"
                        )
                    compiled += " | ".join(heading_summary) + "\n"

                # Add content summary
                compiled += f"  * Content: {page['main_content'][:200]}...\n"
                compiled += f"  * Stats: {page['word_count']} words, {page['heading_count']} headings\n\n"

        # Add site-wide insights
        compiled += f"**Site Structure Insights:**\n"
        compiled += f"The website demonstrates a hierarchical structure with clear content organization. "
        compiled += f"Navigation patterns show systematic information architecture with specific page types "
        compiled += f"serving distinct user needs. Content quality and depth vary by page type and level.\n\n"

        return compiled

    def _scrape_single_page_requests_enhanced(self, url: str):
        """Enhanced single-page scraping with structure extraction"""
        try:
            print(f"[REQUESTS ENHANCED] Scraping: {url}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            content_data = self._extract_content_with_structure(soup, url)

            return {
                "url": url,
                "title": content_data["meta_info"]["title"] or "Scraped Website",
                "content": f"**Single Page Analysis:**\n\n**Headings Found:**\n"
                + "\n".join(
                    [
                        f"- {h['tag'].upper()}: {h['text']}"
                        for h in content_data["headings"][:15]
                    ]
                )
                + f"\n\n**Main Content:**\n{content_data['main_content'][:2000]}...",
                "detailed_analysis": {
                    "total_pages": 1,
                    "levels": {
                        0: [
                            {
                                "url": url,
                                "title": content_data["meta_info"]["title"],
                                "headings": content_data["headings"],
                                "content_type": self._classify_page_type(content_data),
                                "word_count": len(content_data["main_content"].split()),
                            }
                        ]
                    },
                    "all_headings": content_data["headings"],
                },
                "metadata": {
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "scraping_method": "requests_enhanced_single_page",
                    "content_length": len(content_data["main_content"]),
                },
            }
        except Exception as e:
            print(f"[REQUESTS ENHANCED] Error: {e}")
            return None
