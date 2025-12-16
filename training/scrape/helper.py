# Add this helper function for YouTube content summarization
from datetime import datetime, timezone
import json
import os
import re, yaml, requests
from urllib.parse import urljoin
from typing import Dict
import traceback
from db.lance_db_service import LanceDBServer, ScrapedData

# from training.scrape.fast_multilevel_scraper import scrape_website_fast
from agent_route.lance_agent import LanceClient, QueryInput
from cust_helpers import pathconfig
from db.rds_db import connect_to_rds
from services.web_scrape_service import WebScrapingLanceClient
from services.youtube_scrape_service import YouTubeScrapingClient
from utils.async_check import run_async
from utils.base_logger import get_logger
from utils.fireworkzz import get_evaluator_fireworks, get_fireworks_response
from utils.normal import load_yaml_file
from utils.s3_utils import (
    load_yaml_from_s3,
    save_yaml_to_s3,
)

logger = get_logger(__name__)


def flatten_list(lst):
    flattened = []
    for item in lst:
        if isinstance(item, list):
            flattened.extend(flatten_list(item))
        else:
            flattened.append(item)
    return flattened


# --- helper functions
def check_robots_txt(base_url, session):
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        response = session.get(robots_url, timeout=5)
        if response.status_code == 200:
            paths = []
            for line in response.text.split("\n"):
                line = line.strip()
                if line.startswith(("Disallow:", "Allow:")):
                    path = line.split(":", 1)[1].strip().lstrip("/")
                    if path and path != "*" and not path.startswith("#"):
                        paths.append(path.split("?")[0])  # Remove query params
            return list(set(paths))
    except:
        pass
    return []


def check_endpoint(base_url, endpoint, session):
    try:
        url = urljoin(base_url, endpoint)
        response = session.get(url, timeout=5, allow_redirects=False)
        if response.status_code == 200:
            return {
                "endpoint": endpoint,
                "url": url,
                "status": response.status_code,
                "size": len(response.content),
                "accessible": True,
                "protected": False,
                "redirect": False,
            }
    except:
        pass
    return None


def discover_api_endpoints(content, base_url):
    import re

    endpoints = set()
    patterns = [
        r'["\']([^"\']*(?:/api/|/rest/|/graphql|/webhook)[^"\']*)["\']',
        r'url\s*:\s*["\']([^"\']+)["\']',
        r'fetch\s*\(\s*["\']([^"\']+)["\']',
        r'axios\.[a-z]+\s*\(\s*["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        for match in matches:
            if match.startswith("/") and not match.startswith("//"):
                endpoints.add(match.lstrip("/"))
            elif match.startswith(base_url):
                path = match.replace(base_url, "").lstrip("/")
                if path:
                    endpoints.add(path)
    return list(endpoints)


def is_youtube_video_url(url: str) -> bool:
    """
    Detect if a URL is a YouTube video URL (not just youtube.com).

    Returns True for:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://youtube.com/watch?v=VIDEO_ID

    Returns False for:
    - https://www.youtube.com/ (home page)
    - https://www.youtube.com/channel/... (channel page)
    - https://www.youtube.com/user/... (user page)
    - https://www.youtube.com/@... (handle page)
    - https://www.youtube.com/results?search_query=... (search results)
    """
    if not url:
        return False

    import re

    # Match specific YouTube video URL patterns
    patterns = [
        r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]{11})",  # Standard YouTube
        r"(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})",  # Short YouTube
        r"(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]{11})",  # Embedded
    ]

    for pattern in patterns:
        if re.search(pattern, url):
            return True

    return False


def summarize_youtube_data_advanced(youtube_data):
    """
    Summarize YouTube video content similar to web scraping summarization
    """
    try:
        video_url = youtube_data.get("url", "N/A")
        title = youtube_data.get("title", "YouTube Video")
        transcript = youtube_data.get("transcript_raw", "")
        author = youtube_data.get("metadata", {}).get("author", "Unknown")

        # Check if transcript is substantial enough
        MIN_TRANSCRIPT_LENGTH = (
            20  # Further reduced to 20 characters to catch more content
        )
        if not transcript or len(transcript.strip()) < MIN_TRANSCRIPT_LENGTH:
            logger.warning(
                f"Transcript for {video_url} is too short to summarize ({len(transcript)} chars)."
            )

            # Check if this is due to YouTube IP blocking
            if youtube_data.get("error") == "youtube_ip_blocked":
                return f"""**YouTube Video Analysis**

**Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Access Limitation Notice:**
This video could not be processed because YouTube is blocking requests from cloud server IPs (AWS, Google Cloud, Azure, etc.) to prevent automated access.

**Possible Solutions:**
- Use a proxy or VPN service
- Implement YouTube cookies authentication
- Access the video from a non-cloud IP address
- Use alternative video processing methods

**Video Information:**
While we cannot access the content directly, this appears to be a legitimate YouTube video. You may need to manually review the content or use alternative processing methods."""

            # Return a more detailed basic summary for very short content instead of failing
            if transcript and len(transcript.strip()) > 0:
                return f"""**YouTube Video Analysis**

**Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Content Summary:**
This video contains limited speech content. The available transcript shows: "{transcript[:200]}{'...' if len(transcript) > 200 else ''}"

While the transcript is brief, this appears to be a short-form video or one with minimal spoken content. The video may focus more on visual elements, music, or brief commentary rather than extended dialogue."""
            else:
                return f"""**YouTube Video Analysis**

**Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Content Summary:**
This video appears to contain no speech content or the audio could not be processed. This could be:
- A music video without lyrics
- A visual-only video (animations, montages, etc.)
- A video where speech recognition was unsuccessful
- Content that is primarily instrumental or ambient

The video may rely on visual storytelling, music, or non-verbal communication rather than spoken content."""

        # Load prompt template
        yaml_prompts = load_yaml_file(path=pathconfig.agent_template)
        summary_prompt_template = yaml_prompts.get("youtube_summary_prompt_template")

        if not summary_prompt_template:
            # Fallback to web scraping template if YouTube-specific doesn't exist
            summary_prompt_template = yaml_prompts.get("scrape_summary_prompt_template")
            if not summary_prompt_template:
                logger.error("No summary prompt template found in YAML file.")
                return None

        # Create the prompt
        full_prompt = f"""
Please analyze and summarize this YouTube video content:

**Video Title:** {title}
**Author/Channel:** {author}
**Video URL:** {video_url}

**Video Transcript:**
{transcript}

Please provide a comprehensive summary that captures:
1. Main topics and key points discussed
2. Important insights or conclusions
3. Any actionable information or recommendations
4. Overall theme and purpose of the video

Format the summary to be informative and well-structured.
"""

        # Get AI response
        ai_response = get_fireworks_response(full_prompt, role="system")

        if ai_response and isinstance(ai_response, str) and ai_response.strip():
            return ai_response.strip()
        else:
            logger.error(f"AI failed to generate summary for YouTube video {video_url}")
            return None

    except Exception as e:
        logger.error(f"Error during YouTube summarization: {e}")
        traceback.print_exc()
        return None


def evaluate_youtube_content(clarification_prompt, youtube_data, summary_text):
    """
    Evaluate YouTube content to extract clarifications
    """
    try:
        # Combine video content for evaluation
        full_content = f"""
        Video URL: {youtube_data.get('url', '')}
        Video Title: {youtube_data.get('title', '')}
        Author/Channel: {youtube_data.get('metadata', {}).get('author', 'Unknown')}
        Summary: {summary_text}
        Transcript Preview: {youtube_data.get('transcript_raw', '')[:2000]}...
        """

        # Replace placeholder in prompt (assuming you have a YouTube-specific prompt)
        filled_prompt = clarification_prompt.replace(
            "{{youtube_content}}", full_content
        )

        # Get AI response
        ai_response = get_evaluator_fireworks(filled_prompt, "system")

        # Parse response
        try:
            result = json.loads(ai_response)
        except json.JSONDecodeError:
            json_text = re.search(r"\{.*\}", ai_response, re.DOTALL)
            result = json.loads(json_text.group(0)) if json_text else {}

        return {
            "summary": summary_text,
            "clarifications": result.get("clarifications", []),
            "clean_content": summary_text,
        }

    except Exception as e:
        logger.error(f"Error evaluating YouTube content: {e}")
        return None


def clarific_youtube(user_id, val, video_url, title):
    """
    Process clarifications from YouTube content
    """
    clarification_responses = []
    failed_key = f"{user_id}/yaml/failed_ques.yaml"

    failed_ques = flatten_list(load_yaml_from_s3(failed_key) or [])
    failed_data = failed_ques

    # Check for existing YouTube clarifications to prevent duplicates
    existing_questions = set()
    for existing_item in failed_data:
        if (
            existing_item.get("is_youtube")
            and existing_item.get("filename") == video_url
        ):
            existing_questions.add(existing_item.get("User", "").strip().lower())

    quote_summary = val["summary"] if "summary" in val else title

    # Process new clarifications
    for actual_q in val.get("clarifications", []):
        actual_q = actual_q.strip()
        if not actual_q or actual_q.lower() in existing_questions:
            continue

        entry_obj = {
            "User": actual_q,
            "Rephrased Question": actual_q,
            "Ai Response": "",
            "quote": quote_summary,
            "filename": video_url,
            "doc_value": 0,
            "is_youtube": True,
            "youtube_url": video_url,
            "youtube_title": title,
        }
        clarification_responses.append(entry_obj)
        existing_questions.add(actual_q.lower())

    # Merge and save
    updated_data = failed_data + clarification_responses
    save_yaml_to_s3(data=updated_data, user_id=user_id, filename="failed_ques.yaml")

    return clarification_responses


def validate_youtube_clarifications(user_id):
    """
    Validate clarifications from YouTube videos
    """
    try:
        prompts = load_yaml_file(path=pathconfig.agent_template)

        passes_key = f"{user_id}/yaml/passed_ques.yaml"
        failed_key = f"{user_id}/yaml/failed_ques.yaml"

        passed_data = flatten_list(load_yaml_from_s3(passes_key) or [])
        failed_data = flatten_list(load_yaml_from_s3(failed_key) or [])

        # Filter YouTube clarifications
        youtube_clarifications = [
            item
            for item in failed_data
            if item.get("is_youtube") and not item.get("Ai Response")
        ]

        if not youtube_clarifications:
            logger.info("No YouTube clarifications to validate")
            return

        # Get answers for clarifications using existing function
        content = fetch_youtube_ques_with_docs(youtube_clarifications, user_id)

        # Process similar to scraping validation
        batch_size = 10
        valid_responses, updated_clarification_responses = [], []

        for i in range(0, len(content), batch_size):
            batch = content[i : i + batch_size]
            res_raw = evaluator_batch_llama_youtube(
                prompts.get(
                    "youtube_response_validator_batch",
                    prompts.get("scraping_response_validator_batch"),
                ),
                batch,
            )

            # Parse and process results (similar to scraping validation)
            try:
                match = re.search(r"\[\s*\{.*?\}\s*\]", res_raw, re.DOTALL)
                if match:
                    json_str = match.group(0).replace("{{", "{").replace("}}", "}")
                    res_json = json.loads(json_str)
                else:
                    res_json = json.loads(res_raw)
            except:
                try:
                    clean_response = res_raw.replace("{{", "{").replace("}}", "}")
                    match = re.search(r"\[\s*\{.*?\}\s*\]", clean_response, re.DOTALL)
                    res_json = yaml.safe_load(match.group(0)) if match else []
                except:
                    res_json = []

            # Process results
            for original_item, eval_result in zip(batch, res_json):
                actual_q = original_item["query"]
                related_res = eval_result.get("related", False)
                usecase_res = eval_result.get("has_usecase_details", False)
                filename = original_item.get("filename", "").strip()

                # Find original entry
                original_entry = None
                for item in youtube_clarifications:
                    if (
                        item.get("User") == actual_q
                        and item.get("filename") == filename
                    ):
                        original_entry = item
                        break

                if not original_entry:
                    continue

                entry_obj = {
                    "User": actual_q,
                    "Rephrased Question": original_entry.get("Rephrased Question", ""),
                    "Ai Response": eval_result.get("explanation", ""),
                    "quote": original_entry.get("quote", ""),
                    "filename": filename,
                    "doc_value": original_item.get("doc_value", ""),
                    "is_youtube": True,
                    "youtube_url": original_entry.get("youtube_url", ""),
                    "youtube_title": original_entry.get("youtube_title", ""),
                }

                if related_res and usecase_res:
                    entry_obj["date_processed"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    valid_responses.append(entry_obj)
                else:
                    updated_clarification_responses.append(entry_obj)

        # Update files
        npassed_data = append_passed_with_ai_diff(passed_data, valid_responses)

        answered_keys = {(v.get("User"), v.get("filename")) for v in valid_responses}
        failed_data = [
            e
            for e in failed_data
            if not (
                e.get("is_youtube")
                and (e.get("User"), e.get("filename")) in answered_keys
            )
        ]

        # Update failed questions with new responses
        for updated_item in updated_clarification_responses:
            for i, item in enumerate(failed_data):
                if (
                    item.get("User") == updated_item.get("User")
                    and item.get("filename") == updated_item.get("filename")
                    and item.get("is_youtube")
                ):
                    failed_data[i] = updated_item
                    break

        # Save
        if npassed_data:
            save_yaml_to_s3(npassed_data, user_id, "passed_ques.yaml")
        if failed_data:
            save_yaml_to_s3(failed_data, user_id, "failed_ques.yaml")

        logger.info(f"✅ Validated YouTube clarifications for user {user_id}")

    except Exception as e:
        logger.error(f"Error validating YouTube clarifications: {e}")
        traceback.print_exc()


def fetch_youtube_ques_with_docs(clarification_list, user_id):
    """
    Fetch answers for YouTube clarifications using LanceDB
    """
    content = []

    for item in clarification_list:
        question_text = item.get("User", "").strip()
        filename = item.get("filename", "")  # YouTube URL

        if not question_text:
            continue

        # Get answer from LanceDB
        base_doc_ans = []
        if question_text:
            top_k = 3
            query_input = QueryInput(
                user_id=user_id, query_text=question_text, top_k=top_k
            )
            lance_client = LanceClient(user_id=user_id)
            results = run_async(lance_client.query_vector(query_input))
            for r in results:
                clean_text = r.get("text", "").encode().decode("unicode_escape")
                base_doc_ans.append(clean_text)

        response_text = (
            " ".join(base_doc_ans) if base_doc_ans else "No relevant information found."
        )

        content.append(
            {
                "query": question_text,
                "response_text": response_text,
                "filename": filename,
                "doc_value": item.get("doc_value", 0),
            }
        )

    return content


def evaluator_batch_llama_youtube(prompt_template_str, qa_list):
    """
    Evaluate YouTube-based questions and answers using LLaMA
    """
    qa_input_block = "\n".join(
        [
            f"{i+1}.\nUser Question: {item['query']}\nAI Response: {item['response_text']}"
            for i, item in enumerate(qa_list)
        ]
    )

    full_prompt = prompt_template_str.replace("{qa_list}", qa_input_block)

    try:
        llama_response = get_fireworks_response(full_prompt, role="user")
        return llama_response
    except Exception as e:
        print(f"🔥 LLaMA Evaluator batch Error for YouTube: {e}")
        return []


# Flatten nested lists if any
def flatten_list(lst):
    flattened = []
    for item in lst:
        if isinstance(item, list):
            flattened.extend(flatten_list(item))
        else:
            flattened.append(item)
    return flattened


def append_passed_with_ai_diff(existing, new_entries):
    """
    Append new entries to existing if AI Response differs,
    keeping both old and new entries (no overwrite).
    """
    """
    Append new entries to existing:
    - Keep both old and new AI responses if they differ.
    - Avoid adding exact duplicates (same User, filename, Ai Response).
    """
    seen = set()  # (User, filename, Ai Response) triples

    # Add existing entries to seen
    for e in existing:
        key = (e.get("User"), e.get("filename"), e.get("Ai Response"))
        seen.add(key)

    for entry in new_entries:
        key = (entry.get("User"), entry.get("filename"), entry.get("Ai Response"))
        if key not in seen:
            existing.append(entry)
            seen.add(key)

    return existing


def summarize_scraped_data_advanced(scraped_json_data):
    """
    Takes scraped data, validates it, injects it into a prompt, and returns
    a natural language summary from the AI model.

    For YouTube videos: Uses simple summary prompt (clean, no steps)
    For websites: Uses detailed analysis prompt (with structure analysis)
    """
    try:
        url = scraped_json_data.get("url", "N/A")
        content = scraped_json_data.get("content", "")

        # ✅ FIX 1: Add a minimum content length check.
        # If the content is less than 50 characters, it's probably not summarizable.
        MIN_CONTENT_LENGTH = 50
        if not content or len(content.strip()) < MIN_CONTENT_LENGTH:
            logger.warning(
                f"Content for {url} is too short to summarize ({len(content)} chars)."
            )
            # Return a specific error code instead of None
            return "UNSUITABLE_CONTENT"

        # Load the updated prompt from your YAML file
        yaml_prompts = load_yaml_file(path=pathconfig.agent_template)

        # Check if this is YouTube content and use simpler prompt
        is_youtube = is_youtube_video_url(url)

        if is_youtube:
            summary_prompt_template = yaml_prompts.get(
                "youtube_summary_prompt_template"
            )
            if not summary_prompt_template:
                logger.warning("YouTube prompt not found, using default scrape prompt")
                summary_prompt_template = yaml_prompts.get(
                    "scrape_summary_prompt_template"
                )
        else:
            summary_prompt_template = yaml_prompts.get("scrape_summary_prompt_template")

        if not summary_prompt_template:
            logger.error(
                "Prompt 'scrape_summary_prompt_template' not found in YAML file."
            )
            return None

        # Replace placeholders in the prompt
        full_prompt = summary_prompt_template.replace("{url}", str(url)).replace(
            "{website_content}", content
        )

        # Get the formatted text summary from the AI
        ai_response = get_fireworks_response(full_prompt, role="system")

        # Check if the AI response is valid
        if ai_response and isinstance(ai_response, str) and ai_response.strip():
            return ai_response.strip()
        else:
            logger.error(
                f"AI failed to generate a valid summary for {url}. Response: {ai_response}"
            )
            return None

    except Exception as e:
        logger.error(f"An exception occurred during summarization: {e}")
        traceback.print_exc()
        return None


def _scrape_and_process_async(user_id, url_to_scrape, is_youtube):
    """
    Background thread function that does all the heavy processing:
    - Scraping
    - Summarization
    - Embedding
    - LanceDB saving
    - Clarification extraction

    This runs in background so user interface is never blocked.
    """
    try:
        logger.info(f"[ASYNC] Starting background processing for: {url_to_scrape}")

        # STEP 1: Scrape content
        if is_youtube:
            logger.info(f"[ASYNC] Scraping YouTube video...")
            yt_scraper = YouTubeScrapingClient(user_id=user_id)
            scraped_data = yt_scraper.scrape_youtube_single_video_only(url_to_scrape)

            if not scraped_data:
                logger.error(f"[ASYNC] Failed to scrape YouTube: {url_to_scrape}")
                return
        else:
            logger.info(f"[ASYNC] Scraping website (multi-level)...")
            scraper = WebScrapingLanceClient(user_id=user_id)
            scraped_data = scraper.scrape_website(
                url=url_to_scrape, use_selenium=True, max_depth=3, max_pages=25
            )

            if not scraped_data:
                logger.error(f"[ASYNC] Failed to scrape website: {url_to_scrape}")
                return

        logger.info(f"[ASYNC] Content scraped, generating summary...")

        # STEP 2: Summarize
        summary_text = summarize_scraped_data_advanced(scraped_data)

        if not summary_text or summary_text == "UNSUITABLE_CONTENT":
            logger.warning(f"[ASYNC] Summarization failed for: {url_to_scrape}")
            return

        # STEP 3: Extract clarifications from scraped content
        prompts = load_yaml_file(path=pathconfig.agent_template)
        clarification_prompt = prompts.get("extract_scraping_clarifications_prompt")

        val = evaluate_scraped_content(clarification_prompt, scraped_data, summary_text)
        if not val:
            logger.warning(f"[ASYNC] Failed to evaluate content for: {url_to_scrape}")
            val = {"clarifications": []}

        # STEP 4: Process clarifications if any exist
        if val.get("clarifications"):
            try:
                clarific_scraping(
                    user_id, val, url_to_scrape, scraped_data.get("title", "No Title")
                )
            except Exception as e:
                logger.error(f"[ASYNC] Clarification processing failed: {e}")

        # STEP 5: Embed and save to LanceDB
        try:
            logger.info(f"[ASYNC] Creating embeddings and saving to LanceDB...")
            embedding_client = WebScrapingLanceClient(user_id=user_id)
            embedding_vector = embedding_client.embeddings.embed_query(summary_text)

            timestamp = datetime.now(timezone.utc).isoformat()
            lancedb_payload = {
                "user_id": user_id,
                "url": url_to_scrape,
                "title": scraped_data.get("title", "No Title"),
                "content": summary_text,
                "timestamp": timestamp,
                "metadata": scraped_data.get("metadata", {}),
                "embedding": embedding_vector,
            }

            # Save to LanceDB
            lancedb_server_url = os.getenv("LANCE_DB_IP")
            if lancedb_server_url:
                try:
                    response = requests.post(
                        f"{lancedb_server_url}/insert_scraped_data",
                        json=lancedb_payload,
                        timeout=30,
                    )
                    if response.status_code == 200:
                        logger.info(f"[ASYNC] Saved to LanceDB: {url_to_scrape}")
                    else:
                        logger.warning(
                            f"[ASYNC] LanceDB save failed: {response.status_code}"
                        )
                except Exception as e:
                    logger.warning(f"[ASYNC] LanceDB connection error: {e}")

            # STEP 6: Save website metadata to YAML
            if not is_youtube:
                website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
                existing_websites = load_yaml_from_s3(website_metadata_path) or []

                website_entry = {
                    "url": url_to_scrape,
                    "title": scraped_data.get("title", "No Title"),
                    "summary": summary_text,
                    "timestamp": timestamp,
                    "clarifications_count": len(val.get("clarifications", [])),
                    "status": "active",
                }

                existing_websites.append(website_entry)
                save_yaml_to_s3(existing_websites, user_id, "scraped_websites.yaml")
                logger.info(f"[ASYNC] Saved website metadata: {url_to_scrape}")
            else:
                # For YouTube, save to scraped_youtube.yaml
                youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
                existing_videos = load_yaml_from_s3(youtube_metadata_path) or []

                video_entry = {
                    "url": url_to_scrape,
                    "title": scraped_data.get("title", "No Title"),
                    "summary": summary_text,
                    "timestamp": timestamp,
                    "status": "active",
                }

                existing_videos.append(video_entry)
                save_yaml_to_s3(existing_videos, user_id, "scraped_youtube.yaml")
                logger.info(f"[ASYNC] Saved YouTube metadata: {url_to_scrape}")

            # STEP 7: Validate clarifications using AI (in background)
            if val.get("clarifications"):
                try:
                    validate_scraping_clarifications(user_id)
                    logger.info(f"[ASYNC] Clarifications validated: {url_to_scrape}")
                except Exception as e:
                    logger.warning(f"[ASYNC] Clarification validation error: {e}")

        except Exception as e:
            logger.error(f"[ASYNC] Processing failed: {e}")
            traceback.print_exc()

        logger.info(f"[ASYNC] ✅ Completed background processing: {url_to_scrape}")

    except Exception as e:
        logger.error(f"[ASYNC] Fatal error in background processing: {e}")
        traceback.print_exc()


# def _save_scrape_to_lancedb(user_id: str, scraped_data: dict):
#     """
#     Convert scrape result → embedding → Pydantic → LanceDB insert
#     """

#     try:

#         url = scraped_data["url"]
#         title = scraped_data.get("title", "Website")
#         content = scraped_data.get("content", "") or ""

#         timestamp = (
#             scraped_data["metadata"].get("scraped_at") or datetime.utcnow().isoformat()
#         )

#         # === Generate embedding ===
#         full_text = f"{title}\n\n{content}"
#         embed_client = WebScrapingLanceClient(user_id=user_id)
#         embedding_vector = embed_client.embeddings.embed_query(full_text)
#         raw_pages = scraped_data.get("pages_by_level", {})
#         pages_by_level = {str(k): v for k, v in raw_pages.items()}

#         # === Prepare payload ===
#         payload = {
#             "user_id": user_id,
#             "url": url,
#             "title": title or "Untitled Page",
#             "content": content,
#             "timestamp": timestamp,
#             "metadata": scraped_data.get("metadata", {}),
#             "pages_by_level": pages_by_level,
#             "embedding": embedding_vector,
#         }
#         scraped_model = ScrapedData(**payload)

#         # === Insert into LanceDB ===
#         LanceDBServer().insert_scraped_data(scraped_model)

#         logger.info(f"[LANCEDB] Saved scrape for {url}")

#     except Exception as e:
#         logger.error(f"[LANCEDB] Failed to save: {e}")


def chunk_text(text: str, chunk_size=3000, overlap=300):
    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk.strip())
        start = end - overlap  # overlap for semantic continuity

    return chunks


def _save_scrape_to_lancedb(user_id: str, scraped_data: dict):
    """
    Save scrape results including ALL pages.
    Use chunked embeddings for long text.
    """

    try:
        # -----------------------------
        # Base metadata
        # -----------------------------
        url = scraped_data["url"]
        title = scraped_data.get("title", "Website")
        main_content = scraped_data.get("content", "") or ""

        timestamp = (
            scraped_data["metadata"].get("scraped_at") or datetime.utcnow().isoformat()
        )

        # -----------------------------
        # Build FULL textual dataset
        # -----------------------------
        pages_by_level = scraped_data.get("pages_by_level", {})
        text_parts = []

        # Main Page
        text_parts.append(f"MAIN PAGE:\n{main_content}")

        # Nested pages
        for lvl, pages in pages_by_level.items():
            if not pages:
                continue

            if isinstance(pages, dict):
                pages = [pages]

            for idx, p in enumerate(pages):
                c = p.get("content")
                if not c:
                    continue
                if not isinstance(c, str):
                    try:
                        c = str(c)
                    except:
                        continue

                text_parts.append(f"\n=== LEVEL {lvl} PAGE {idx} ===\n{c.strip()}")

        combined_text = "\n\n".join(text_parts).strip()

        # -----------------------------
        # Chunk the combined text
        # -----------------------------
        chunks = chunk_text(combined_text, chunk_size=3000, overlap=300)

        logger.info(
            f"[LANCEDB] Total text length={len(combined_text)}, chunks={len(chunks)}"
        )

        # -----------------------------
        # Embed each chunk
        # -----------------------------
        embed_client = WebScrapingLanceClient(user_id=user_id)

        chunk_embeddings = []
        for idx, chunk in enumerate(chunks):
            emb = embed_client.embeddings.embed_query(chunk)
            chunk_embeddings.append(emb)
            logger.debug(f"[LANCEDB] embedded chunk {idx+1}/{len(chunks)}")

        # -----------------------------
        # Pick the first chunk embedding for primary vector search
        # -----------------------------
        main_embedding = chunk_embeddings[0]

        # -----------------------------
        # Normalize pages_by_level
        # -----------------------------
        normalized_pages = {str(k): v for k, v in pages_by_level.items()}

        # -----------------------------
        # Prepare payload
        # -----------------------------
        payload = {
            "user_id": user_id,
            "url": url,
            "title": title,
            "content": main_content,
            "contacts": scraped_data.get("contacts"),
            "timestamp": timestamp,
            "metadata": {
                "status": scraped_data.get("status", "active"),
                **scraped_data.get("metadata", {}),
                "chunk_embeddings": chunk_embeddings,  # store all embeddings
                "total_chunks": len(chunks),
            },
            "pages_by_level": normalized_pages,
            "embedding": main_embedding,
        }

        scraped_model = ScrapedData(**payload)

        # -----------------------------
        # Save into LanceDB
        # -----------------------------
        LanceDBServer().insert_scraped_data(scraped_model)
        logger.info(f"[LANCEDB] Stored scrape with chunk embeddings for {url}")

    except Exception as e:
        logger.error(f"[LANCEDB] Failed to save: {e}")


def evaluate_scraped_content(clarification_prompt, scraped_data, summary_text):
    """
    Evaluate scraped content to extract clarifications using AI.
    Similar to evaluate_transcript but for web scraping.
    """
    try:
        # Combine scraped content for evaluation
        full_content = f"""
        URL: {scraped_data.get('url', '')}
        Title: {scraped_data.get('title', '')}
        Summary: {summary_text}
        Original Content Preview: {scraped_data.get('content', '')[:2000]}...
        """

        # Replace placeholder in prompt
        filled_prompt = clarification_prompt.replace(
            "{{scraped_content}}", full_content
        )

        # Get AI response
        ai_response = get_evaluator_fireworks(filled_prompt, "system")

        # Parse the response (assuming it returns JSON with clarifications)
        try:
            result = json.loads(ai_response)
        except json.JSONDecodeError:
            import re

            json_text = re.search(r"\{.*\}", ai_response, re.DOTALL)
            result = json.loads(json_text.group(0)) if json_text else {}

        return {
            "summary": summary_text,
            "clarifications": result.get("clarifications", []),
            "clean_content": summary_text,
        }

    except Exception as e:
        logger.error(f"Error evaluating scraped content: {e}")
        return None


def clarific_scraping(user_id, val, url, title):
    """
    Process clarifications extracted from scraped content with duplicate prevention.
    """
    clarification_responses = []
    failed_key = f"{user_id}/yaml/failed_ques.yaml"

    failed_ques = flatten_list(load_yaml_from_s3(failed_key) or [])
    failed_data = failed_ques

    # Load existing clarifications to check for duplicates
    existing_questions = set()
    for existing_item in failed_data:  # Now failed_data is defined
        if existing_item.get("is_scraping") and existing_item.get("filename") == url:
            existing_questions.add(existing_item.get("User", "").strip().lower())

    quote_summary = val["summary"] if "summary" in val else title

    # Process new clarifications with duplicate check
    for actual_q in val.get("clarifications", []):
        actual_q = actual_q.strip()
        if not actual_q or actual_q.lower() in existing_questions:
            continue  # Skip duplicates

        entry_obj = {
            "User": actual_q,
            "Rephrased Question": actual_q,
            "Ai Response": "",
            "quote": quote_summary,
            "filename": url,
            "doc_value": 0,
            "is_scraping": True,
            "scrape_url": url,
            "scrape_title": title,
        }
        clarification_responses.append(entry_obj)
        existing_questions.add(actual_q.lower())

    # Merge old + new clarifications
    updated_data = failed_data + clarification_responses  # Now this works

    # Save back into YAML
    save_yaml_to_s3(data=updated_data, user_id=user_id, filename="failed_ques.yaml")

    return clarification_responses


def validate_scraping_clarifications(user_id):
    """
    Validate clarifications from failed_ques.yaml by getting answers and evaluating them.
    Similar to the document processing validation but for scraping clarifications.
    """
    try:
        # Load prompts
        prompts = load_yaml_file(path=pathconfig.agent_template)

        # File paths in S3
        passes_key = f"{user_id}/yaml/passed_ques.yaml"
        failed_key = f"{user_id}/yaml/failed_ques.yaml"

        passed_data = flatten_list(load_yaml_from_s3(passes_key) or [])
        failed_data = flatten_list(load_yaml_from_s3(failed_key) or [])

        # Filter only scraping-related clarifications that need validation
        scraping_clarifications = [
            item
            for item in failed_data
            if item.get("is_scraping") and not item.get("Ai Response")
        ]

        if not scraping_clarifications:
            logger.info("No scraping clarifications to validate")
            return

        # Get answers for clarifications
        content = fetch_scraping_ques_with_docs(scraping_clarifications, user_id)

        # Batch process for evaluation
        batch_size = 10
        valid_responses, updated_clarification_responses = [], []

        for i in range(0, len(content), batch_size):
            batch = content[i : i + batch_size]
            res_raw = evaluator_batch_llama_scraping(
                prompts.get("scraping_response_validator_batch"), batch
            )

            # Parse evaluator response
            try:
                # First try to find JSON array
                match = re.search(r"\[\s*\{.*?\}\s*\]", res_raw, re.DOTALL)
                if match:
                    json_str = match.group(0)
                    # Clean up any template artifacts
                    json_str = json_str.replace("{{", "{").replace("}}", "}")
                    res_json = json.loads(json_str)
                else:
                    # Fallback: try to parse the entire response
                    res_json = json.loads(res_raw)
            except json.JSONDecodeError as e:
                logger.error(f"❌ JSON parsing failed, trying YAML: {e}")
                try:
                    # Remove template artifacts before YAML parsing
                    clean_response = res_raw.replace("{{", "{").replace("}}", "}")
                    match = re.search(r"\[\s*\{.*?\}\s*\]", clean_response, re.DOTALL)
                    res_json = yaml.safe_load(match.group(0)) if match else []
                except Exception as yaml_e:
                    logger.error(f"❌ Both JSON and YAML parsing failed: {yaml_e}")
                    res_json = []
            except Exception as e:
                logger.error(f"❌ Unexpected error parsing evaluator response: {e}")
                res_json = []

            # Process evaluation results
            for original_item, eval_result in zip(batch, res_json):
                actual_q = original_item["query"]
                related_res = eval_result.get("related", False)
                usecase_res = eval_result.get("has_usecase_details", False)
                filename = original_item.get("filename", "").strip()

                # Find original clarification entry
                original_entry = None
                for item in scraping_clarifications:
                    if (
                        item.get("User") == actual_q
                        and item.get("filename") == filename
                    ):
                        original_entry = item
                        break

                if not original_entry:
                    continue

                entry_obj = {
                    "User": actual_q,
                    "Rephrased Question": original_entry.get("Rephrased Question", ""),
                    "Ai Response": eval_result.get("explanation", ""),
                    "quote": original_entry.get("quote", ""),
                    "filename": filename,
                    "doc_value": original_item.get("doc_value", ""),
                    "is_scraping": True,
                    "scrape_url": original_entry.get("scrape_url", ""),
                    "scrape_title": original_entry.get("scrape_title", ""),
                }

                if related_res and usecase_res:
                    entry_obj["date_processed"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    valid_responses.append(entry_obj)
                else:
                    updated_clarification_responses.append(entry_obj)

        # Update passed questions
        npassed_data = append_passed_with_ai_diff(passed_data, valid_responses)

        # Remove answered questions from failed_data and add updated clarifications
        answered_keys = {(v.get("User"), v.get("filename")) for v in valid_responses}
        failed_data = [
            e
            for e in failed_data
            if not (
                e.get("is_scraping")
                and (e.get("User"), e.get("filename")) in answered_keys
            )
        ]

        # Update failed questions with new AI responses
        for updated_item in updated_clarification_responses:
            # Replace old entry with updated one
            for i, item in enumerate(failed_data):
                if (
                    item.get("User") == updated_item.get("User")
                    and item.get("filename") == updated_item.get("filename")
                    and item.get("is_scraping")
                ):
                    failed_data[i] = updated_item
                    break

        # Save back to S3
        if npassed_data:
            save_yaml_to_s3(npassed_data, user_id, "passed_ques.yaml")
        if failed_data:
            save_yaml_to_s3(failed_data, user_id, "failed_ques.yaml")

        logger.info(f"✅ Validated scraping clarifications for user {user_id}")

    except Exception as e:
        logger.error(f"Error validating scraping clarifications: {e}")
        traceback.print_exc()


def fetch_scraping_ques_with_docs(clarification_list, user_id):
    """
    Fetch answers for scraping clarifications using LanceDB.
    Similar to fetch_ques_with_docs but for scraping-based questions.
    """
    content = []

    for item in clarification_list:
        question_text = item.get("User", "").strip()
        filename = item.get("filename", "")  # This will be the URL

        if not question_text:
            continue

        # Get answer from LanceDB
        base_doc_ans = []
        if question_text:
            top_k = 3
            query_input = QueryInput(
                user_id=user_id, query_text=question_text, top_k=top_k
            )
            lance_client = LanceClient(user_id=user_id)
            results = run_async(lance_client.query_vector(query_input))
            for r in results:
                clean_text = r.get("text", "").encode().decode("unicode_escape")
                base_doc_ans.append(clean_text)

        response_text = (
            " ".join(base_doc_ans) if base_doc_ans else "No relevant information found."
        )

        content.append(
            {
                "query": question_text,
                "response_text": response_text,
                "filename": filename,
                "doc_value": item.get("doc_value", 0),
            }
        )

    return content


def evaluator_batch_llama_scraping(prompt_template_str, qa_list):
    """
    Evaluate scraping-based questions and answers using LLaMA.
    Similar to evaluator_batch_llama but specifically for scraping content.
    """
    qa_input_block = "\n".join(
        [
            f"{i+1}.\nUser Question: {item['query']}\nAI Response: {item['response_text']}"
            for i, item in enumerate(qa_list)
        ]
    )

    # Use replace instead of format to avoid KeyError with JSON braces
    full_prompt = prompt_template_str.replace("{qa_list}", qa_input_block)

    try:
        llama_response = get_fireworks_response(full_prompt, role="user")
        return llama_response
    except Exception as e:
        print(f"🔥 LLaMA Evaluator batch Error for scraping: {e}")
        return []


# ============================================================================
# FAST MULTI-LEVEL WEBSITE SCRAPING ENDPOINTS
# ============================================================================


def _save_website_summary_to_db(user_id: str, url: str, scraped_data: dict):
    """
    Save scraped website summary to database in background
    """
    try:
        logger.info(f"[SAVE_DB] Starting database save for {url}")

        connection = connect_to_rds()
        if connection is None:
            logger.error("[SAVE_DB] Database connection failed")
            return

        cursor = connection.cursor()

        try:
            import uuid
            from urllib.parse import urlparse

            scrape_id = str(uuid.uuid4())
            parsed_url = urlparse(url)
            normalized_url = (
                f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}".rstrip(
                    "/"
                ).lower()
            )

            title = scraped_data.get("title", "")
            original_summary = scraped_data.get("content", "")
            total_pages = scraped_data.get("metadata", {}).get("total_pages", 0)
            total_words = sum(
                p.get("word_count", 0) for p in scraped_data.get("all_pages", [])
            )
            scrape_method = scraped_data.get("metadata", {}).get("scraping_method", "")
            scrape_duration = scraped_data.get("metadata", {}).get(
                "total_time_seconds", 0
            )

            cursor.execute(
                """
                INSERT INTO scraped_websites 
                (scrape_id, user_id_fk, url, normalized_url, title, original_summary, edited_summary, 
                total_pages, total_words, scrape_method, scrape_duration_seconds, is_edited)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new
                ON DUPLICATE KEY UPDATE
                    original_summary = new.original_summary,
                    edited_summary = new.original_summary,
                    title = new.title,
                    total_pages = new.total_pages,
                    total_words = new.total_words,
                    scrape_method = new.scrape_method,
                    scrape_duration_seconds = new.scrape_duration_seconds,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    scrape_id,
                    user_id,
                    url,
                    normalized_url,
                    title,
                    original_summary,
                    original_summary,
                    total_pages,
                    total_words,
                    scrape_method,
                    scrape_duration,
                    False,
                ),
            )

            connection.commit()
            logger.info(f"[SAVE_DB] Successfully saved website summary for {url}")

        finally:
            cursor.close()
            connection.close()

    except Exception as e:
        logger.error(f"[SAVE_DB] Error saving website summary: {e}")
        traceback.print_exc()


def _save_website_to_s3(user_id: str, url: str, scraped_data: dict):
    """
    Save scraped website data to S3 YAML file in background
    Saves YouTube videos to scraped_youtube.yaml and websites to scraped_websites.yaml
    """
    try:
        logger.info(f"[SAVE_S3] Starting background save for {url}")

        # Check if this is a YouTube video
        is_youtube = (
            scraped_data.get("metadata", {}).get("scraping_method") == "youtube_video"
        )

        # Generate summary from scraped data
        # summary_text = _compile_fast_scrape_summary(scraped_data)
        summary_text = scraped_data.get("content", "")
        if not summary_text:
            logger.warning(f"[SAVE_S3] Failed to generate summary for {url}")
            summary_text = scraped_data.get(
                "title", "Website" if not is_youtube else "YouTube Video"
            )

        timestamp = datetime.now(timezone.utc).isoformat()

        if is_youtube:
            # Save YouTube video to YouTube file
            youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
            logger.info(
                f"[SAVE_S3] Loading existing YouTube videos from {youtube_metadata_path}"
            )
            existing_videos = load_yaml_from_s3(youtube_metadata_path) or []
            logger.info(
                f"[SAVE_S3] Found {len(existing_videos)} existing YouTube videos"
            )

            # Create YouTube entry
            youtube_entry = {
                "url": url,
                "title": scraped_data.get("title", "YouTube Video"),
                "summary": summary_text[:500],
                "timestamp": timestamp,
                "status": scraped_data.get("status", "active"),
                "content": scraped_data.get("content", ""),
                "metadata": scraped_data.get("metadata", {}),
                "contacts": scraped_data.get("contacts", "All"),
            }

            # Append and save
            existing_videos.append(youtube_entry)
            logger.info(f"[SAVE_S3] Saving {len(existing_videos)} YouTube videos to S3")
            save_yaml_to_s3(existing_videos, user_id, "scraped_youtube.yaml")
            logger.info(f"[SAVE_S3] ✅ Successfully saved YouTube video {url} to S3")
        else:
            # Load existing websites
            website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
            logger.info(
                f"[SAVE_S3] Loading existing websites from {website_metadata_path}"
            )
            existing_websites = load_yaml_from_s3(website_metadata_path) or []
            logger.info(f"[SAVE_S3] Found {len(existing_websites)} existing websites")

            # Create website entry
            website_entry = {
                "url": url,
                "title": scraped_data.get("title", "Website"),
                "summary": scraped_data.get("content"),
                "pages_count": scraped_data["metadata"]["total_pages"],
                "scraping_time": scraped_data["metadata"]["total_time_seconds"],
                "timestamp": timestamp,
                "status": scraped_data.get("status", "active"),
                "pages_by_level": scraped_data["pages_by_level"],
                "contacts": scraped_data.get("contacts", "All"),
            }

            # Append and save
            existing_websites.append(website_entry)
            logger.info(f"[SAVE_S3] Saving {len(existing_websites)} websites to S3")
            save_yaml_to_s3(existing_websites, user_id, "scraped_websites.yaml")
            logger.info(f"[SAVE_S3] ✅ Successfully saved {url} to S3")

    except Exception as e:
        logger.error(f"[SAVE_S3] Error saving to S3: {e}", exc_info=True)


def _summary_update_lance_s3(user_id: str, url: str, summary: str):
    try:
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        logger.info(f"[SAVE_S3] Loading existing websites from {website_metadata_path}")

        existing_websites = load_yaml_from_s3(website_metadata_path) or []

        current_link = None
        for i in existing_websites:
            if i.get("url") == url:
                current_link = i
                break

        if not current_link:
            print("no link present")
            return False

        # Update YAML object
        current_link["summary"] = summary
        # print("current link", current_link)
        # print("existing websites", existing_websites)
        # 1️⃣ Save S3 FIRST
        save_yaml_to_s3(existing_websites, user_id, "scraped_websites.yaml")

        # 2️⃣ Then update LanceDB
        lance_service = LanceDBServer()
        lance_service._update_summary_scrape(user_id=user_id, url=url, content=summary)

        logger.info(f"[SAVE_S3] ✅ Successfully saved {url} to S3 and LanceDB")
        return True

    except Exception:
        logger.exception(f"[SAVE_S3] ❌ Failed to save summary for {url}")
        return False


def _update_status_lance_s3(user_id: str, url: str, status: str):
    try:
        webname = "scraped_websites.yaml"
        ytname = "scraped_youtube.yaml"

        def _is_youtube_url(url: str) -> bool:
            """Check if the URL is a YouTube video URL"""
            youtube_patterns = [
                "youtube.com/watch",
                "youtu.be/",
                "youtube.com/embed/",
                "youtube.com/v/",
            ]
            url_lower = url.lower()
            return any(pattern in url_lower for pattern in youtube_patterns)

        filename = ytname if _is_youtube_url(url) else webname
        metadata_path = f"{user_id}/yaml/{filename}"

        logger.info(f"[SAVE_S3] Loading metadata from {metadata_path}")

        existing_items = load_yaml_from_s3(metadata_path) or []

        current_link = None
        for item in existing_items:
            if item.get("url") == url:
                current_link = item
                break

        if not current_link:
            logger.warning(f"[SAVE_S3] URL not found: {url}")
            return False

        # Update YAML
        current_link["status"] = status

        lance_service = LanceDBServer()
        lance_service._update_status_scrape(user_id=user_id, url=url, status=status)
        # Save YAML FIRST
        save_yaml_to_s3(existing_items, user_id, filename)

        # Update LanceDB

        logger.info(f"[SAVE_S3] ✅ Updated status='{status}' for {url}")
        return True

    except Exception:
        logger.exception(f"[SAVE_S3] ❌ Failed to update status for {url}")
        return False


def _intenral_link_summary_update_lance_s3(
    user_id: str, url: str, inner_url: str, summary: str
):
    try:
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        logger.info(f"[SAVE_S3] Loading existing websites from {website_metadata_path}")

        existing_websites = load_yaml_from_s3(website_metadata_path) or []

        current_link = None
        for i in existing_websites:
            if i.get("url") == url:
                current_link = i
                break

        if not current_link:
            return False

        pages_by_level = current_link.get("pages_by_level", {})
        updated = False

        for level, pages in pages_by_level.items():
            if not isinstance(pages, list):
                continue

            for page in pages:
                if page.get("url") == inner_url:
                    page["content"] = summary
                    updated = True
                    break

            if updated:
                break

        if not updated:
            return False  # inner URL not found

        # 1️⃣ Save YAML first
        save_yaml_to_s3(existing_websites, user_id, "scraped_websites.yaml")

        # 2️⃣ Then update LanceDB
        lance_service = LanceDBServer()
        lance_service._update_innerscrape_scrape(
            user_id=user_id,
            url=url,
            innerurl=inner_url,
            content=summary,
        )

        logger.info(f"[SAVE_S3] ✅ Successfully saved inner summary for {inner_url}")
        return True

    except Exception:
        logger.exception(f"[SAVE_S3] ❌ Failed to save inner summary for {inner_url}")
        return False


def _internal_link_delete_lance_s3(user_id: str, url: str, inner_url: str):
    try:
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        logger.info(
            f"[DELETE_S3] Loading existing websites from {website_metadata_path}"
        )

        existing_websites = load_yaml_from_s3(website_metadata_path) or []

        current_link = None
        for site in existing_websites:
            if site.get("url") == url:
                current_link = site
                break

        if not current_link:
            return False

        pages_by_level = current_link.get("pages_by_level", {})
        deleted = False

        for level in list(pages_by_level.keys()):
            pages = pages_by_level.get(level, [])
            if not isinstance(pages, list):
                continue

            original_len = len(pages)
            pages_by_level[level] = [p for p in pages if p.get("url") != inner_url]

            if len(pages_by_level[level]) != original_len:
                deleted = True

            # Remove empty levels
            if not pages_by_level[level]:
                del pages_by_level[level]

        if not deleted:
            return False  # inner link not found

        # Persist YAML first
        save_yaml_to_s3(existing_websites, user_id, "scraped_websites.yaml")

        # Sync LanceDB
        lance_service = LanceDBServer()
        lance_service._delete_innerscrape_scrape(
            user_id=user_id, url=url, innerurl=inner_url
        )

        logger.info(f"[DELETE_S3] ✅ Deleted inner link {inner_url} from {url}")
        return True

    except Exception:
        logger.exception(f"[DELETE_S3] ❌ Failed to delete inner link {inner_url}")
        return False


def _scrape_website_fast_async(user_id: str, url: str):
    """
    Background async function for fast website scraping
    """
    try:
        logger.info(f"[ASYNC_FAST] Starting fast scrape for {url}")

        scraped_data = scrape_website_fast(url, user_id)
        if not scraped_data:
            logger.error(f"[ASYNC_FAST] Scraping failed for {url}")
            return

        # Generate comprehensive summary from all pages
        summary_text = _compile_fast_scrape_summary(scraped_data)

        if not summary_text:
            logger.warning(f"[ASYNC_FAST] Failed to generate summary for {url}")
            return

        # Create embeddings and save to LanceDB
        embedding_client = WebScrapingLanceClient(user_id=user_id)
        embedding_vector = embedding_client.embeddings.embed_query(summary_text)

        timestamp = datetime.now(timezone.utc).isoformat()
        lancedb_payload = {
            "user_id": user_id,
            "url": url,
            "title": scraped_data.get("title", "Website"),
            "content": summary_text,
            "timestamp": timestamp,
            "metadata": {
                "pages_count": scraped_data["metadata"]["total_pages"],
                "levels": (
                    scraped_data["metadata"]["levels_scraped"]
                    if "levels_scraped" in scraped_data["metadata"]
                    else {}
                ),
                "scraping_time": scraped_data["metadata"]["total_time_seconds"],
            },
            "embedding": embedding_vector,
        }

        # Save to LanceDB
        lancedb_server_url = os.getenv("LANCE_DB_IP")
        if lancedb_server_url:
            try:
                response = requests.post(
                    f"{lancedb_server_url}/insert_scraped_data",
                    json=lancedb_payload,
                    timeout=30,
                )
                if response.status_code != 200:
                    logger.warning(
                        f"[ASYNC_FAST] LanceDB returned {response.status_code}"
                    )
            except Exception as e:
                logger.error(f"[ASYNC_FAST] LanceDB error: {e}")

        # Save website metadata
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        logger.info(
            f"[ASYNC_FAST] Loading existing websites from {website_metadata_path}"
        )
        existing_websites = load_yaml_from_s3(website_metadata_path) or []
        logger.info(f"[ASYNC_FAST] Found {len(existing_websites)} existing websites")

        website_entry = {
            "url": url,
            "title": scraped_data.get("title", "Website"),
            "summary": summary_text[:500],  # Store first 500 chars
            "pages_count": scraped_data["metadata"]["total_pages"],
            "scraping_time": scraped_data["metadata"]["total_time_seconds"],
            "timestamp": timestamp,
            "status": "active",
            "pages_by_level": scraped_data[
                "pages_by_level"
            ],  # Store full structure for click-to-expand
        }

        existing_websites.append(website_entry)
        logger.info(f"[ASYNC_FAST] Saving {len(existing_websites)} websites to S3")
        save_yaml_to_s3(existing_websites, user_id, "scraped_websites.yaml")
        logger.info(f"[ASYNC_FAST] ✅ Completed scraping and saving for {url}")

    except Exception as e:
        logger.error(f"[ASYNC_FAST] Error: {e}", exc_info=True)
        traceback.print_exc()


def _scrape_youtube_async(user_id: str, url: str):
    """
    Background async function for YouTube scraping (existing method)
    """
    try:
        logger.info(f"[ASYNC_YT] Starting YouTube scrape for {url}")

        yt_scraper = YouTubeScrapingClient(user_id=user_id)
        scraped_data = yt_scraper.scrape_youtube_single_video_only(url)

        if not scraped_data:
            logger.error(f"[ASYNC_YT] YouTube scraping failed for {url}")
            return

        summary_text = summarize_youtube_data_advanced(scraped_data)
        if not summary_text or summary_text == "UNSUITABLE_CONTENT":
            logger.warning(f"[ASYNC_YT] Summarization failed for {url}")
            return

        # Continue with rest of YouTube processing (embeddings, LanceDB, etc.)
        # ... (existing code from _scrape_and_process_async)

    except Exception as e:
        logger.error(f"[ASYNC_YT] Error: {e}")
        traceback.print_exc()


def _compile_fast_scrape_summary(scraped_data: Dict) -> str:
    """
    Compile comprehensive summary from fast multi-level scraping
    """
    try:
        lines = []
        lines.append(f"**Website: {scraped_data['title']}**\n")
        lines.append(f"**URL:** {scraped_data['url']}\n")
        lines.append(f"\n**Overview:**")
        lines.append(
            f"This comprehensive analysis covers {scraped_data['metadata']['total_pages']} pages "
        )
        lines.append(
            f"across different levels of the website, scraped in {scraped_data['metadata']['total_time_seconds']} seconds.\n\n"
        )

        # Add content from each level
        for level in range(3):
            pages = scraped_data["pages_by_level"][level]
            if not pages:
                continue

            level_name = "Homepage" if level == 0 else f"Level {level} Pages"
            lines.append(f"**{level_name} ({len(pages)} pages):**\n")

            for page in pages:
                lines.append(f"- **{page['title']}**\n")
                lines.append(f"  Content Preview: {page['content'][:200]}...\n")
                if page.get("links"):
                    lines.append(f"  Contains {len(page['links'])} sub-links\n")

            lines.append("\n")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error compiling summary: {e}")
        return None


def generate_level_context(pages_by_level):
    # Build full_context safely
    full_context_parts = []
    total_chars = 0
    for lvl_index, level_pages in enumerate(pages_by_level):
        if level_pages is None:
            logger.debug(f"[FAST] level {lvl_index} is None, skipping")
            continue

        # If level_pages is a single page dict (not a list), normalize
        if isinstance(level_pages, dict):
            level_pages = [level_pages]

        # If level_pages is not iterable skip with log
        if not isinstance(level_pages, (list, tuple)):
            logger.warning(
                f"[FAST] level {lvl_index} expected list but got {type(level_pages)}; skipping"
            )
            continue

        for page_index, page in enumerate(level_pages):
            if not isinstance(page, dict):
                logger.debug(
                    f"[FAST] skipping non-dict page at level {lvl_index} index {page_index}: {type(page)}"
                )
                continue

            content = page.get("content", "")
            if content is None:
                continue

            # If content is not a string, convert safely
            if not isinstance(content, str):
                try:
                    content = str(content)
                    logger.debug(
                        f"[FAST] coerced content to str for page at level {lvl_index} index {page_index}"
                    )
                except Exception:
                    logger.debug(
                        f"[FAST] failed to coerce content to str at level {lvl_index} index {page_index}; skipping"
                    )
                    continue

            content = content.strip()
            if not content:
                continue

            # Append but limit to keep prompt reasonably sized
            snippet = content[:5000]  # keep per-page cap
            full_context_parts.append(snippet)
            total_chars += len(snippet)

            # Optional: stop if too big for your LLM budget
            if total_chars > 50000:
                logger.info("[FAST] full_context truncated at ~50k chars")
                break

        if total_chars > 50000:
            break

    full_context = "\n\n".join(full_context_parts).strip()
    return full_context
