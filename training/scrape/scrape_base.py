import asyncio
from datetime import datetime
from datetime import timezone
from db.lance_db_service import LanceDBServer
from flask import Blueprint, request, Response, jsonify
from services.web_scrape_service import WebScrapingLanceClient
from services.youtube_scrape_service import YouTubeScrapingClient
from agent_route.ag_helperzz import (
    deletefilebasedData,
)
from agent_route.lance_agent import LanceClient
from training.scrape.fast_multilevel_scraper import (
    FastMultilevelScraper,
)
from cust_helpers import pathconfig
from training.scrape.helper import *
from training.scrape.helper import _scrape_and_process_async
from training.scrape.helper import _scrape_youtube_async
from training.scrape.helper import _scrape_website_fast_async
from training.scrape.helper import _summary_update_lance_s3
from training.scrape.helper import _intenral_link_summary_update_lance_s3
from training.scrape.helper import _internal_link_delete_lance_s3
from training.scrape.helper import _update_status_lance_s3
from utils.base_logger import get_logger
from utils.celery_base import get_scrape_lock, run_back_scrape
from utils.normal import load_yaml_file
import traceback
from db.rds_db import connect_to_rds
from datetime import datetime
from utils.s3_utils import (
    delete_file_from_s3,
    load_yaml_from_s3,
    save_yaml_to_s3,
)
from db.db_checkers import (
    fetch_userid_from_launch,
    check_userid_valid,
)
from datetime import datetime
from credits_route.route import Credits
from utils.key_rotation_manager import SecureKMSService

secure_kms = SecureKMSService()


logger = get_logger(__name__)

scrape_agent_bps = Blueprint("agents_scrape", __name__)


@scrape_agent_bps.route("/scrape-youtube", methods=["POST"])
async def scrape_youtube_route():
    """
    Scrape YouTube video, get transcript, summarize, and extract clarifications
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        youtube_url = data.get("url")

        if not api_key or not youtube_url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Check for duplicates
        youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
        existing_videos = load_yaml_from_s3(youtube_metadata_path) or []

        for video in existing_videos:
            if video.get("status") == "active" and video.get("url") == youtube_url:
                return (
                    jsonify(
                        {
                            "error": "Duplicate video found",
                            "message": f"YouTube video '{youtube_url}' has already been processed.",
                            "existing_entry": video,
                        }
                    ),
                    409,
                )

        # Step 1: Scrape YouTube video
        youtube_scraper = YouTubeScrapingClient(user_id=user_id)
        scraped_data = youtube_scraper.scrape_youtube_video(youtube_url)

        if not scraped_data:
            return (
                jsonify({"error": "Failed to access YouTube video or get transcript"}),
                500,
            )

        if scraped_data.get("error") == "transcript_unavailable":
            return (
                jsonify(
                    {
                        "error": "Transcript not available",
                        "message": "This YouTube video doesn't have captions/transcript available",
                    }
                ),
                422,
            )

        # Step 2: Summarize
        summary_text = await summarize_youtube_data_advanced(scraped_data, user_id)

        enc_title = secure_kms.encrypt(user_id, scraped_data.get("title", "YouTube Video"))
        enc_summary = secure_kms.encrypt(user_id, summary_text)

        scraped_data["title"] = enc_title
        summary_text = enc_summary

        if summary_text == "UNSUITABLE_CONTENT":
            return (
                jsonify(
                    {
                        "error": "Video content could not be analyzed",
                        "details": "The transcript was too short or not suitable for summarization",
                    }
                ),
                422,
            )

        if not summary_text:
            return jsonify({"error": "Failed to generate video summary"}), 500

        # Step 3: Extract clarifications
        prompts = load_yaml_file(path=pathconfig.agent_template)
        # Use existing clarification prompt or create YouTube-specific one
        clarification_prompt = prompts.get(
            "extract_youtube_clarifications_prompt"
        ) or prompts.get("extract_scraping_clarifications_prompt")

        val = await evaluate_youtube_content(
            clarification_prompt, scraped_data, summary_text, userid=user_id
        )
        if not val:
            return (
                jsonify(
                    {"error": "Failed to evaluate video content for clarifications"}
                ),
                500,
            )

        # Step 4: Process clarifications
        if val["clarifications"]:
            clarific_youtube(
                user_id, val, youtube_url, scraped_data.get("title", "No Title")
            )

        # Step 5: Create embedding and save to LanceDB
        embedding_client = YouTubeScrapingClient(user_id=user_id)
        embedding_vector = embedding_client.embeddings.embed_query(summary_text)

        timestamp = datetime.now(timezone.utc).isoformat()
        lancedb_payload = {
            "user_id": user_id,
            "url": youtube_url,
            "title": scraped_data.get("title", "YouTube Video"),
            "content": summary_text,
            "timestamp": timestamp,
            "metadata": scraped_data.get("metadata", {}),
            "embedding": embedding_vector,
        }

        # ---------- calculate credits -------------------

        total_input_chars = len(original_summary_text)
        # total_output_chars = 0
        # total_output_chars += sum(len(vec) for vec in embedding_vector)
        total_output_chars = len(embedding_vector)

        total_chars = total_input_chars + total_output_chars

        credits = Credits()
        await credits.update_ai_credits_redis(
            credit_type="embedding",
            total_chars=total_chars,
            user_id=user_id,
            reference_id=inspect.stack()[0].function,
        )

        # ---------------------------------------------------

        # # Step 6: Save to LanceDB
        # lancedb_server_url = os.getenv("LANCE_DB_IP")
        # if not lancedb_server_url:
        #     return jsonify({"error": "LANCE_DB_IP environment variable not set"}), 500

        try:
            # response = requests.post(
            #     f"{lancedb_server_url}/insert_scraped_data",
            #     json=lancedb_payload,
            #     timeout=30,
            # )
            ser = LanceDBServer()
            response = ser.insert_scraped_data(data=lancedb_payload)
            # response = S()
            if not response:
                raise Exception(f"LanceDB returned status {response.status_code}")
        except Exception as e:
            logger.error(f"LanceDB Error: {e}")
            return jsonify({"error": f"Vector database error: {str(e)}"}), 500

        # Step 7: Save YouTube metadata
        video_entry = {
            "url": youtube_url,
            "video_id": scraped_data.get("video_id"),
            "title": scraped_data.get("title", "YouTube Video"),
            "author": scraped_data.get("metadata", {}).get("author", "Unknown"),
            "summary": summary_text,
            "timestamp": timestamp,
            "clarifications_count": len(val.get("clarifications", [])),
            "status": "active",
        }

        existing_videos.append(video_entry)
        save_yaml_to_s3(existing_videos, user_id, "scraped_youtube.yaml")

        # Step 8: Validate clarifications if any
        if val.get("clarifications"):
            await validate_youtube_clarifications(user_id)

        return (
            jsonify(
                {
                    "status": "success",
                    "summary": summary_text,
                    "url": youtube_url,
                    "title": scraped_data.get("title"),
                    "author": scraped_data.get("metadata", {}).get("author"),
                    "timestamp": timestamp,
                    "clarifications_found": len(val.get("clarifications", [])),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error in YouTube scraping route: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


def fetch_youtube_summaries(user_id):
    youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
    videos_data = load_yaml_from_s3(youtube_metadata_path)

    if not videos_data:
        return []

    active_videos = []
    for v in videos_data:
        if v.get("status") == "active":
            v["type"] = "youtube"
            active_videos.append(v)

    return active_videos


def fetch_website_summaries(user_id):
    website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
    websites_data = load_yaml_from_s3(website_metadata_path)

    if not websites_data:
        logger.info(f"No scraped websites file found for user {user_id}")
        return []

    active_websites = []
    for w in websites_data:
        if w.get("status"):
            if "pages_by_level" in w and isinstance(w["pages_by_level"], dict):
                w["pages_by_level"] = {
                    str(k): v for k, v in w["pages_by_level"].items()
                }

            w["type"] = "web"
            active_websites.append(w)

    return active_websites


# Add route to get YouTube summaries
# Add route to get YouTube summaries
@scrape_agent_bps.route("/get-youtube-summaries", methods=["GET"])
def get_youtube_summaries():
    api_key = request.args.get("api_key")
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400

    user_id = fetch_userid_from_launch(api_key)
    if not user_id:
        return jsonify({"error": "Invalid API Key"}), 401

    return jsonify(fetch_youtube_summaries(user_id)), 200


@scrape_agent_bps.route("/get-website-summaries", methods=["GET"])
def get_website_summaries():
    api_key = request.args.get("api_key")
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400

    user_id = fetch_userid_from_launch(api_key)
    if not user_id:
        return jsonify({"error": "Invalid API Key"}), 401

    return jsonify(fetch_website_summaries(user_id)), 200


def _unwrap_response(res):
    """Convert a Flask Response to Python data if needed"""
    if isinstance(res, Response):
        try:
            return res.get_json()
        except:
            return None
    return res


async def get_active_scrape_status(user_id):
    """
    Returns: { "url": <url>, "status": "processing" } or None
    """
    active_url = await get_scrape_lock(user_id)
    if active_url:
        return {
            "url": active_url.decode() if isinstance(active_url, bytes) else active_url,
            "status": "processing",
        }
    return None


@scrape_agent_bps.route("/get-web-summaries", methods=["GET"])
async def get_web_summaries():
    try:
        api_key = request.args.get("api_key")
        if not api_key:
            return jsonify({"error": "api_key is required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        comp_web = []
        comp_web.extend(fetch_youtube_summaries(user_id))
        comp_web.extend(fetch_website_summaries(user_id))

        active_scrape = await get_active_scrape_status(user_id)
        if active_scrape:
            comp_web.append(active_scrape)

        return jsonify(comp_web), 200

    except Exception as e:
        logger.exception("Error fetching summaries")
        return jsonify({"error": str(e)}), 500


# Add route to delete YouTube summary
@scrape_agent_bps.route("/delete-youtube-summary", methods=["DELETE", "POST"])
async def delete_youtube_summary():
    """Delete a YouTube video summary and related clarifications"""
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url_to_delete = data.get("url")

        if not api_key or not url_to_delete:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Delete from LanceDB
        lance_client = LanceDBServer()
        delete_result = lance_client.delete_scraped_data(
            user_id=user_id, url=url_to_delete
        )

        if delete_result.get("status") != "success":
            return (
                jsonify(
                    {
                        "error": "Failed to delete from LanceDB",
                        "details": delete_result.get("message"),
                    }
                ),
                500,
            )

        # Update metadata
        youtube_metadata_path = f"{user_id}/yaml/scraped_youtube.yaml"
        videos_data = load_yaml_from_s3(youtube_metadata_path) or []

        updated_videos = []
        for video in videos_data:
            if video.get("url") == url_to_delete:
                # video["status"] = "deleted"
                # video["deleted_at"] = datetime.now().isoformat()
                continue
            updated_videos.append(video)

        if len(updated_videos) == 0:
            delete_file_from_s3(youtube_metadata_path)
        else:
            save_yaml_to_s3(updated_videos, user_id, "scraped_youtube.yaml")

        # Delete clarifications
        success = deletefilebasedData(url_to_delete, user_id)
        if not success:
            logger.warning(
                f"Failed to delete YouTube clarification entries for user {user_id}"
            )

        return jsonify({"message": "YouTube video summary deleted successfully"}), 200

    except Exception as e:
        logger.error(f"Error deleting YouTube summary: {e}")
        return jsonify({"error": str(e)}), 500


@scrape_agent_bps.route("/scrape", methods=["POST"])
async def scrape_website_route():
    """This function handles the web request, scrapes data, and saves it."""
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        url_to_scrape = data.get("url")

        if not user_id or not url_to_scrape:
            return jsonify({"error": "user_id and url are required"}), 400

        # --- Step 1: Scrape the website (This part is correct) ---
        scraper = WebScrapingLanceClient(user_id=user_id)
        scraped_data = scraper.scrape_website(
            url=url_to_scrape, use_selenium=True, max_depth=3, max_pages=25
        )

        if not scraped_data:
            return jsonify({"error": "Failed to scrape the website content"}), 500

        # --- Step 2: NEW - AI Summarization of scraped content ---
        logger.info(f"Summarizing scraped content for: {scraped_data['url']}")
        summary_text = await summarize_scraped_data_advanced(scraped_data, user_id)

        enc_title = secure_kms.encrypt(user_id, scraped_data.get("title", "YouTube Video"))
        enc_summary = secure_kms.encrypt(user_id, summary_text)

        scraped_data["title"] = enc_title
        summary_text = enc_summary

        

        if not summary_text or summary_text == "UNSUITABLE_CONTENT":
            logger.warning(f"Summarization failed, using original content")
            summary_text = scraped_data["content"][:2000]  # Fallback to raw content

        # --- Step 3: Process the summarized text to get an embedding ---
        embedding_client = WebScrapingLanceClient(user_id=user_id)

        full_content = f"{scraped_data['title_plain']}\n\n{summary_plain}"
        embedding_vector = embedding_client.embeddings.embed_query(full_content)

        # -------- calculate credits ---------------

        total_input_chars = len(full_content)
        # total_output_chars = 0
        # total_output_chars += sum(len(vec) for vec in embedding_vector)
        total_output_chars = len(embedding_vector)

        total_chars = total_input_chars + total_output_chars

        credits = Credits()
        await credits.update_ai_credits_redis(
            credit_type="embedding",
            total_chars=total_chars,
            user_id=user_id,
            reference_id=inspect.stack()[0].function,
        )

        # -----------------------------------------

        # --- Step 4: NEW - Prepare the payload for the LanceDB server (using summary) ---
        lancedb_payload = {
            "user_id": user_id,
            "url": scraped_data["url"],
            "title": scraped_data["title"],
            "content": summary_text,  # Use AI-generated summary, not raw content
            "timestamp": scraped_data["metadata"]["scraped_at"],
            "metadata": scraped_data["metadata"],
            "embedding": embedding_vector,
        }

        # --- Step 5: NEW - Send the data to your LanceDB/FastAPI server ---
        # lancedb_server_url = os.getenv("LANCE_DB_IP")
        # if not lancedb_server_url:
        #     return (
        #         jsonify({"error": "LANCE_DB_IP environment variable is not set"}),
        #         500,
        #     )

        # response = requests.post(
        #     f"{lancedb_server_url}/insert_scraped_data", json=lancedb_payload
        # )
        ser = LanceDBServer()
        response = ser.insert_scraped_data(data=lancedb_payload)

        # Check if the data was saved successfully
        if response:
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "Website scraped and data saved successfully.",
                        "scraped_content": {
                            **scraped_data,
                            "summary": summary_text,  # Return AI-generated summary
                        },
                        "lancedb_response": response.json(),
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "error": "Failed to save data to LanceDB server.",
                        "status_code": 500,
                        "details": response.text,
                        # It's good practice to also return the data that failed to save
                        "scraped_content_that_failed_to_save": scraped_data,
                    }
                ),
                500,
            )

    except Exception as e:
        logger.error(f"Error in /scrape route: {e}")
        traceback.print_exc()
        return (
            jsonify({"error": "An internal server error occurred", "details": str(e)}),
            500,
        )


@scrape_agent_bps.route("/scrape-and-summarize", methods=["POST"])
def scrape_and_summarize_route():
    """
    Handles adding a new website or YouTube video: scrapes, summarizes, embeds, saves to LanceDB,
    extracts clarifications, and returns the result for the frontend.

    ASYNC APPROACH: All heavy processing happens in background thread.
    User gets immediate response while scraping/processing happens in background.
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url_to_scrape = data.get("url")

        if not api_key or not url_to_scrape:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Quick validation check only - no heavy processing
        is_youtube = is_youtube_video_url(url_to_scrape)

        # Check for duplicates only for websites (quick operation)
        if not is_youtube:
            website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
            existing_websites = load_yaml_from_s3(website_metadata_path) or []
            normalized_new_url = url_to_scrape.rstrip("/")

            for website in existing_websites:
                if website.get("status") == "active":
                    existing_url = website.get("url", "").rstrip("/")
                    if existing_url == normalized_new_url:
                        return (
                            jsonify(
                                {
                                    "error": "Duplicate website found",
                                    "message": f"Website '{url_to_scrape}' has already been added and processed.",
                                    "existing_entry": website,
                                }
                            ),
                            409,
                        )

        # RESPOND IMMEDIATELY - All heavy work happens in background
        from threading import Thread

        processing_thread = Thread(
            target=_scrape_and_process_async,
            args=(user_id, url_to_scrape, is_youtube),
            daemon=True,
        )
        processing_thread.start()

        # Return immediate response to frontend
        timestamp = datetime.now(timezone.utc).isoformat()
        return (
            jsonify(
                {
                    "status": "processing",
                    "message": "Your content is being scraped and processed in the background",
                    "url": url_to_scrape,
                    "timestamp": timestamp,
                    "note": "Check back in a few moments for the summary",
                }
            ),
            202,  # 202 Accepted - request accepted for processing but not completed
        )

    except Exception as e:
        logger.error(f"Error in /scrape-and-summarize route: {e}")
        traceback.print_exc()
        return (
            jsonify({"error": "An internal server error occurred", "details": str(e)}),
            500,
        )


@scrape_agent_bps.route("/get-website-details", methods=["POST"])
def get_website_details():
    """Fetches full website details with page hierarchy for a specific saved website."""
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Load all websites metadata
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        websites_data = load_yaml_from_s3(website_metadata_path)

        if websites_data is None:
            return jsonify({"error": "No scraped websites found"}), 404

        # Find the specific website
        website = None
        for w in websites_data:
            if w.get("url") == url:
                website = w
                break

        if not website:
            return jsonify({"error": "Website not found"}), 404

        # Decrypt main title

        try:
            decrypted_title = secure_kms.decrypt(
                user_id,
                website["title"]["encrypted_key"],
                website["title"]["iv"],
                website["title"]["ciphertext"]
            )
        except:
            decyrpted_title = website.get("title")
        pages_by_level = website.get("pages_by_level", {})

        # Decyrpt homepage summary
        level_0_pages = pages_by_level.get(0) or pages_by_level.get("0", [])
        if level_0_pages:
            enc_summary = level_0_pages[0].get("summary") or level_0_pages.get("content")

            try:
                homepage_summary = secure_kms.decrypt(
                    user_id,
                    enc_summary["encrypted_key"],
                    enc_summary["iv"],
                    enc_summary["ciphertext"]
                )
            except:
                homepage_summary = enc_summary
        else:
            homepage_summary = ""

        response_data = {
            "status": "success",
            "url": website.get("url"),
            "title": decrypted_title,
            "homepage_summary": homepage_summary,
            "pages_by_level": {},
            "total_pages": website.get("pages_count"),
            "scraping_time": website.get("scraping_time"),
            "timestamp": website.get("timestamp"),
        }
        
        # Decrypt each page
        for level_key in [0, 1, 2]:
            # Try integer key first (from YAML), then string key (from JSON)
            level_pages = pages_by_level.get(level_key) or pages_by_level.get(
                str(level_key), []
            )
            decrypted_pages = []
            for page in level_pages:
                try:
                    title = secure_kms.decrypt(
                        user_id,
                        page["title"]["encrypted_key"],
                        page["title"]["iv"],
                        page["title"]["ciphertext"]
                    )
                except:
                    summary = enc_summary
                decrypted_pages.append(
                    {
                        "url": page.get("url"),
                        "title": title,
                        "summary": summary,
                        "word_count": page.get("word_count", 0),
                        "depth": page.get("depth", level_key),
                        "has_sublinks": len(page.get("links", [])) > 0,
                    }
                )
            response_data["pages_by_level"][str(level_key)] = decrypted_pages

        logger.info(f"[GET_WEBSITE_DETAILS] Retrieved details for {url}")
        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"[GET_WEBSITE_DETAILS] Error: {e}")
        return jsonify({"error": str(e)}), 500


@scrape_agent_bps.route("/delete-website-summary", methods=["DELETE", "POST"])
def delete_website_summary():
    """Deletes a website summary and its related clarifications."""
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url_to_delete = data.get("url")

        if not api_key or not url_to_delete:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Step 1: Delete from LanceDB
        lance_client = LanceDBServer()
        delete_result = lance_client.delete_scraped_data(
            user_id=user_id, url=url_to_delete
        )

        if delete_result.get("status") != "success":
            return (
                jsonify(
                    {
                        "error": "Failed to delete from LanceDB",
                        "details": delete_result.get("message"),
                    }
                ),
                500,
            )

        # Step 2: Update website metadata
        website_metadata_path = f"{user_id}/yaml/scraped_websites.yaml"
        websites_data = load_yaml_from_s3(website_metadata_path) or []

        updated_websites = []
        for website in websites_data:
            if website.get("url") == url_to_delete:
                # website["status"] = "deleted"
                # website["deleted_at"] = datetime.now().isoformat()
                continue
            updated_websites.append(website)
        if len(updated_websites) == 0:
            delete_file_from_s3(website_metadata_path)
        else:
            save_yaml_to_s3(updated_websites, user_id, "scraped_websites.yaml")
        scraper = FastMultilevelScraper(user_id=user_id, credits=None, max_workers=3)

        # Check for duplicate
        duplicate = asyncio.run(scraper.clear_duplicate_scrape(url_to_delete))

        # Step 3: Delete related clarifications
        success = deletefilebasedData(url_to_delete, user_id)
        if not success:
            logger.warning(
                f"Failed to delete clarification entries for user {user_id}, URL {url_to_delete}"
            )

        return (
            jsonify(
                {
                    "message": "Website summary and related clarifications deleted successfully"
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error deleting summary: {e}")
        return jsonify({"error": str(e)}), 500


@scrape_agent_bps.route("/scrape-website-fast", methods=["POST"])
async def scrape_website_fast_stream():
    try:
        data = request.json
        api_key = data.get("api_key")
        url = data.get("url")
        level = data.get("level")

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        # ---- Check if user has an active scrape in-progress ----
        active_url = await get_scrape_lock(user_id)
        if active_url:
            active_url = (
                active_url.decode() if isinstance(active_url, bytes) else active_url
            )

            # If same URL → already processing
            if active_url == url:
                return {
                    "message": "scrape already running for this url",
                    "url": url,
                    "status": "processing",
                }, 200

            # If different URL → user cannot start a new scrape until old one finishes
            return {
                "message": "another scrape already running",
                "current_processing_url": active_url,
                "requested_url": url,
                "status": "processing",
            }, 409

        # ---- No lock → safe to start task ----
        run_back_scrape.delay(url=url, user_id=user_id, level=level)

        return {"message": "task started", "url": url}

    except Exception as e:
        logger.error(f"[FAST_SCRAPE] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/save-website-summary", methods=["POST"])
def save_website_summary():
    """
    Save scraped website summary to database

    Request body:
    {
        "api_key": "user_api_key",
        "url": "https://example.com",
        "title": "Website Title",
        "original_summary": "AI generated summary",
        "total_pages": 5,
        "total_words": 1500,
        "scrape_method": "fast_multilevel_concurrent",
        "scrape_duration_seconds": 12.5
    }
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")
       
        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400
        
        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        title = secure_kms.encrypt(user_id, data.get("title", ""))
        original_summary = secure_kms.encrypt(user_id, data.get("original_summary", ""))

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        try:
            import uuid
            from urllib.parse import urlparse

            # Check if table exists first
            check_table_query = """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scraped_websites'
            """
            cursor.execute(check_table_query)
            table_exists = cursor.fetchone()[0] > 0

            if not table_exists:
                logger.warning("[SAVE_SUMMARY] scraped_websites table does not exist")
                return (
                    jsonify(
                        {
                            "error": "Website summary table not found. Please create the scraped_websites table first."
                        }
                    ),
                    500,
                )

            scrape_id = str(uuid.uuid4())
            parsed_url = urlparse(url)
            normalized_url = (
                f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}".rstrip(
                    "/"
                ).lower()
            )

            cursor.execute(
                """
                INSERT INTO scraped_websites 
                (scrape_id, user_id_fk, url, normalized_url, title, original_summary, edited_summary, 
                 total_pages, total_words, scrape_method, scrape_duration_seconds, is_edited)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    original_summary = VALUES(original_summary),
                    title = VALUES(title),
                    total_pages = VALUES(total_pages),
                    total_words = VALUES(total_words),
                    scrape_method = VALUES(scrape_method),
                    scrape_duration_seconds = VALUES(scrape_duration_seconds),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    scrape_id,
                    user_id,
                    url,
                    normalized_url,
                    title,
                    original_summary,
                    original_summary,  # edited_summary initially same as original
                    data.get("total_pages", 0),
                    data.get("total_words", 0),
                    data.get("scrape_method", ""),
                    data.get("scrape_duration_seconds", 0),
                    False,
                ),
            )
            connection.commit()

            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "Website summary saved successfully",
                        "scrape_id": scrape_id,
                        "url": url,
                        "title": title,
                    }
                ),
                201,
            )

        finally:
            cursor.close()
            connection.close()

    except Exception as e:
        logger.error(f"[SAVE_SUMMARY] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/edit-website-summary", methods=["POST"])
def edit_website_summary():
    """
    Edit saved website summary

    Request body:
    {
        "api_key": "user_api_key",
        "url": "https://example.com",
        "edited_summary": "User edited summary text"
    }
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")
        edited_summary = secure_kms.encrypt(user_id, data.get("edited_summary", ""))

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        if not edited_summary:
            return jsonify({"error": "edited_summary is required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        try:
            from urllib.parse import urlparse

            # Check if table exists first
            check_table_query = """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scraped_websites'
            """
            cursor.execute(check_table_query)
            table_exists = cursor.fetchone()[0] > 0

            if not table_exists:
                logger.warning("[EDIT_SUMMARY] scraped_websites table does not exist")
                return jsonify({"error": "Website summary not found"}), 404

            parsed_url = urlparse(url)
            normalized_url = (
                f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}".rstrip(
                    "/"
                ).lower()
            )

            # Update the summary
            cursor.execute(
                """
                UPDATE scraped_websites 
                SET edited_summary = %s, is_edited = TRUE, updated_at = CURRENT_TIMESTAMP
                WHERE user_id_fk = %s AND normalized_url = %s
                """,
                (edited_summary, user_id, normalized_url),
            )
            connection.commit()

            if cursor.rowcount == 0:
                return jsonify({"error": "Website summary not found"}), 404

            _summary_update_lance_s3(user_id=user_id, url=url, summary=edited_summary)

            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "Website summary updated successfully",
                        "url": url,
                    }
                ),
                200,
            )

        finally:
            cursor.close()
            connection.close()

    except Exception as e:
        logger.error(f"[EDIT_SUMMARY] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/edit-internal_link-summary", methods=["POST"])
def edit_internal_link_summary():
    """ """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")
        internal_link = data.get("internal_link")
        edited_summary = secure_kms.encrypt(user_id, data.get("edited_summary", ""))

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        if not edited_summary:
            return jsonify({"error": "edited_summary is required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        _intenral_link_summary_update_lance_s3(
            user_id=user_id, url=url, inner_url=internal_link, summary=edited_summary
        )

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "internal link summary updated successfully",
                    "url": url,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[EDIT_SUMMARY] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/delete-internal_link-summary", methods=["POST"])
def delete_internal_link_summary():
    """ """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")
        internal_link = data.get("internal_link")

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        _internal_link_delete_lance_s3(
            user_id=user_id, url=url, inner_url=internal_link
        )

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "internal link summary updated successfully",
                    "url": url,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[EDIT_SUMMARY] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/get-website-summary", methods=["POST"])
def get_website_summary():

    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        try:
            from urllib.parse import urlparse

            # Check if table exists first
            check_table_query = """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scraped_websites'
            """
            cursor.execute(check_table_query)
            table_exists = cursor.fetchone()[0] > 0

            if not table_exists:
                logger.warning("[GET_SUMMARY] scraped_websites table does not exist")
                return jsonify({"error": "Website summary not found"}), 404

            parsed_url = urlparse(url)
            normalized_url = (
                f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}".rstrip(
                    "/"
                ).lower()
            )

            cursor.execute(
                """
                SELECT scrape_id, url, title, original_summary, edited_summary, 
                       total_pages, total_words, scrape_method, scrape_duration_seconds, 
                       is_edited, created_at, updated_at
                FROM scraped_websites 
                WHERE user_id_fk = %s AND normalized_url = %s
                """,
                (user_id, normalized_url),
            )

            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "Website summary not found"}), 404

            # Decrypt encrypted fields

            try:
                title = secure_kms.decrypt(
                    user_id,
                    row[2]["encrypted_key"],
                    row[2]["iv"],
                    row[2]["ciphertext"]
                )
            except:
                original_summary = row[3]
            edited_summary = None

            if row[4]:
                try:
                    edited_summary = secure_kms.decrypt(
                        user_id,
                        row[4]["encrypted_key"],
                        row[4]["iv"],
                        row[4]["ciphertext"]
                    )
                except:
                    edited_summary = row[4]

            return  jsonify(
                {
                    "status": "success",
                    "scrape_id": row[0],
                    "url": row[1],
                    "title": title,
                    "original_summary": row[3],
                    "edited_summary": row[4],
                    "total_pages": row[5],
                    "total_words": row[6],
                    "scrape_method": row[7],
                    "scrape_duration_seconds": row[8],
                    "is_edited": row[9],
                    "created_at": row[10].isoformat() if row[10] else None,
                    "updated_at": row[11].isoformat() if row[11] else None,
                    "current_summary": edited_summary if row[9] else original_summary
                      # Return edited if available, else original
                }
            ), 200,
            
        finally:
            cursor.close()
            connection.close()

    except Exception as e:
        logger.error(f"[GET_SUMMARY] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/list-scraped-websites", methods=["POST"])
def list_scraped_websites():
    """
    List all scraped websites for a user with pagination

    Request body:
    {
        "api_key": "user_api_key",
        "page": 1,
        "limit": 10,
        "filter_edited": false  // Optional: only show edited summaries
    }
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")

        if not api_key:
            return jsonify({"error": "api_key is required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        page = max(1, int(data.get("page", 1)))
        limit = min(100, int(data.get("limit", 10)))  # Max 100 per page
        offset = (page - 1) * limit
        filter_edited = data.get("filter_edited", False)

        connection = connect_to_rds()
        if connection is None:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        try:
            # Check if table exists first
            check_table_query = """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scraped_websites'
            """
            cursor.execute(check_table_query)
            table_exists = cursor.fetchone()[0] > 0

            if not table_exists:
                logger.warning("[LIST_WEBSITES] scraped_websites table does not exist")
                return (
                    jsonify(
                        {
                            "status": "success",
                            "total_count": 0,
                            "page": page,
                            "limit": limit,
                            "total_pages": 0,
                            "websites": [],
                            "message": "No scraped websites found. Please create the scraped_websites table first.",
                        }
                    ),
                    200,
                )

            # Get total count
            where_clause = "WHERE user_id_fk = %s"
            params = [user_id]

            if filter_edited:
                where_clause += " AND is_edited = TRUE"

            cursor.execute(
                f"SELECT COUNT(*) FROM scraped_websites {where_clause}", params
            )
            total_count = cursor.fetchone()[0]

            # Get paginated results
            cursor.execute(
                f"""
                SELECT scrape_id, url, title, original_summary, edited_summary,
                       total_pages, total_words, is_edited, created_at, updated_at
                FROM scraped_websites 
                {where_clause}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )

            websites = []
            for row in cursor.fetchall():
                websites.append(
                    {
                        "scrape_id": row[0],
                        "url": row[1],
                        "title": row[2],
                        "original_summary": row[3],
                        "edited_summary": row[4],
                        "total_pages": row[5],
                        "total_words": row[6],
                        "is_edited": row[7],
                        "created_at": row[8].isoformat() if row[8] else None,
                        "updated_at": row[9].isoformat() if row[9] else None,
                    }
                )

            return (
                jsonify(
                    {
                        "status": "success",
                        "total_count": total_count,
                        "page": page,
                        "limit": limit,
                        "total_pages": (total_count + limit - 1) // limit,
                        "websites": websites,
                    }
                ),
                200,
            )

        finally:
            cursor.close()
            connection.close()

    except Exception as e:
        logger.error(f"[LIST_WEBSITES] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/scrape-website-page", methods=["POST"])
def scrape_website_page_endpoint():
    """
    Get detailed summary for a specific scraped page

    This endpoint is called when user clicks on a scraped link
    It returns the full content/summary for that specific page
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        page_url = data.get("page_url")
        level = data.get("level")

        if not api_key or not page_url:
            return jsonify({"error": "api_key and page_url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        logger.info(f"[PAGE_DETAIL] Getting details for {page_url}")
        credits = Credits()
        # Quick scrape of just this page
        from training.scrape.fast_multilevel_scraper import FastMultilevelScraper

        scraper = FastMultilevelScraper(user_id=user_id, credits=credits, max_workers=1)
        page_data = scraper._scrape_page(page_url, depth=level)

        if not page_data:
            return jsonify({"error": "Failed to scrape page"}), 500

        # Encrypt Sensitive fields
        try:
            encrypted_title = secure_kms.encrypt(user_id, page_data["title"])
            encrypted_content = secure_kms.encrypt(user_id, page_data["content"])
        except Exception as e:
            logger.warning(f"[ENCRYPT_PAGE] Failed: {e}")
            encrypted_title = page_data["title"]
            encrypted_content = page_data["content"]

        # Decrypt before sending to frontend
        try:
            decrypted_title = secure_kms.decrypt(
                user_id, 
                encrypted_title["encrypted_key"],
                encrypted_title["iv"],
                encrypted_title["ciphertext"]
            )
        except:
            decrypted_title = page_data["title"]
        try:
            decrypted_content = secure_kms.decrypt(
                user_id,
                encrypted_content["encrypted_key"],
                encrypted_content["iv"],
                encrypted_content["ciphertext"]
            )
        except:
            decrypted_title = page_data["content"]
            
        response_data = {
            "status": "success",
            "url": page_data["url"],
            "title": decrypted_title,
            "content": decrypted_content,
            "word_count": page_data["word_count"],
            "sublinks": page_data.get("links", []),
        }

        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"[PAGE_DETAIL] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/scrape-and-summarize-fast", methods=["POST"])
def scrape_and_summarize_fast_endpoint():
    """
    Updated /scrape-and-summarize that uses the fast multi-level scraper for websites
    Keeps YouTube scraping as-is

    This is the main entry point - it detects if URL is YouTube or website
    """
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url_to_scrape = data.get("url")

        if not api_key or not url_to_scrape:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404

        is_youtube = is_youtube_video_url(url_to_scrape)

        if is_youtube:
            # Route to YouTube scraping
            logger.info(f"[FAST_SUMMARY] YouTube detected, using YouTube scraper")
            from threading import Thread

            processing_thread = Thread(
                target=_scrape_youtube_async, args=(user_id, url_to_scrape), daemon=True
            )
            processing_thread.start()
        else:
            # Route to website scraping with fast multi-level
            logger.info(f"[FAST_SUMMARY] Website detected, using fast scraper")
            from threading import Thread

            processing_thread = Thread(
                target=_scrape_website_fast_async,
                args=(user_id, url_to_scrape),
                daemon=True,
            )
            processing_thread.start()

        timestamp = datetime.now(timezone.utc).isoformat()
        return (
            jsonify(
                {
                    "status": "processing",
                    "message": "Content is being scraped and processed",
                    "url": url_to_scrape,
                    "type": "youtube" if is_youtube else "website",
                    "timestamp": timestamp,
                }
            ),
            202,
        )

    except Exception as e:
        logger.error(f"[FAST_SUMMARY] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@scrape_agent_bps.route("/update-contacts-scraped", methods=["POST"])
def update_contacts_scraped():
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")
        contacts = data.get("contacts")

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        # Load metadata lists
        websites_path = f"{user_id}/yaml/scraped_websites.yaml"
        youtube_path = f"{user_id}/yaml/scraped_youtube.yaml"

        websites_data = load_yaml_from_s3(websites_path) or []
        youtube_data = load_yaml_from_s3(youtube_path) or []

        # Try find matching entry
        web_entry = next((w for w in websites_data if w.get("url") == url), None)
        yt_entry = next((y for y in youtube_data if y.get("url") == url), None)

        # URL must exist in exactly one
        if not web_entry and not yt_entry:
            return jsonify({"error": "url not found"}), 404

        # Update in LanceDB
        LanceDBServer().update_scraped_contacts(
            user_id=user_id, url=url, contacts=contacts
        )
        # Update website entry
        if web_entry:
            web_entry["contacts"] = contacts
            save_yaml_to_s3(
                websites_data, user_id=user_id, filename="scraped_websites.yaml"
            )

        # Update YouTube entry
        if yt_entry:
            yt_entry["contacts"] = contacts
            save_yaml_to_s3(
                youtube_data, user_id=user_id, filename="scraped_youtube.yaml"
            )

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"[UPDATE_CONTACTS] Error: {e}")
        return jsonify({"error": str(e)}), 500


@scrape_agent_bps.route("/check-scrape-check", methods=["POST"])
async def check_scrape_base():
    data = request.json
    userid = data.get("user_id")
    question = data.get("query")
    service = LanceClient(user_id=userid)
    query_input = QueryInput(user_id=userid, query_text=question, top_k=3)
    vector = service.embeddings.embed_query(question)

    # ---------- calculate credits ----------

    total_input_chars = len(question)
    total_output_chars = 0
    # total_output_chars += sum(len(vec) for vec in vector)
    total_output_chars = len(vector)

    total_chars = total_input_chars + total_output_chars

    credits = Credits()
    await credits.update_ai_credits_redis(
        credit_type="embedding",
        total_chars=total_chars,
        user_id=userid,
        reference_id=inspect.stack()[0].function,
    )

    # -----------------------------------------

    scrape_results = await service.scrape_query_vector(
        sender_email="All", query_input=query_input, vector=vector
    )
    return jsonify(scrape_results)


@scrape_agent_bps.route("/update-scraped-status", methods=["POST"])
def update_scraped_status():
    try:
        data = request.get_json()
        api_key = data.get("api_key")
        url = data.get("url")
        status = data.get("status")

        if not api_key or not url:
            return jsonify({"error": "api_key and url are required"}), 400

        user_id = fetch_userid_from_launch(api_key)
        if not user_id:
            return jsonify({"error": "Invalid API Key"}), 401

        if not check_userid_valid(user_id):
            return jsonify({"error": "Invalid access"}), 404
        val = _update_status_lance_s3(url=url, user_id=user_id, status=status)
        if val:
            return jsonify({"status": status}), 200
        else:
            return jsonify({"error": "problem with the server"}), 500

    except Exception as e:
        logger.error(f"[EDIT_SUMMARY] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "Internal server error", "details": str(e)}), 500
