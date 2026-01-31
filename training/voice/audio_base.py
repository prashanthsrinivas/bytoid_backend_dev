import asyncio
from datetime import datetime
import json
import os
import uuid
from agent_route.ag_helperzz import deletefilebasedData
from agent_route.s_t_s import Speech2TextService
from agent_route.train_lance_agent import TrainLanceAgent
from agent_route.utils import extract_transcript_filename
from cust_helpers import pathconfig
from db.db_checkers import (
    check_userid_valid,
    fetch_document_link,
    get_user_agent_id,
    update_agent_document_link,
)
from db.lance_db_service import LanceDBServer
from flask import request, jsonify, Response, stream_with_context, Blueprint
from utils.base_logger import get_logger
from utils.fireworkzz import evaluate_transcript
from utils.normal import load_yaml_file
from utils.s3_utils import (
    attach_CLDFRNT_url,
    delete_file_from_s3,
    read_json_from_s3,
    upload_any_file,
)
from werkzeug.utils import secure_filename
from request_context import current_user_id
from credits_route.route import Credits
from db.rds_db import connect_to_rds



logger = get_logger(__name__)


audio_agent_bps = Blueprint("agent_audio", __name__)


def _ensure_tmp_dir_for_s3key(s3_key: str) -> str:
    """
    Given an s3 key like '109161/.../file.json' return a safe local /tmp basename path.
    Ensures /tmp exists and returns '/tmp/<basename>'.
    """
    os.makedirs("/tmp", exist_ok=True)
    return os.path.join("/tmp", os.path.basename(s3_key))


def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning("Failed to remove temp file %s: %s", path, e)


@audio_agent_bps.route("/process_audio", methods=["POST"])
def process_audio_stream():
    """
    Streaming SSE endpoint that yields progress messages and at the end emits:
      data: FINAL_RESPONSE:{"message": "...", ...}
    The route is synchronous (Flask/Werkzeug compatible) but runs an async generator inside a new loop.
    """

    api_key = request.form.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400

    userid, agentid = get_user_agent_id(api_key)
    if not userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    if "audio_file" not in request.files:
        return jsonify({"error": "Audio file is required"}), 400

    audio_file = request.files["audio_file"]
    filename = secure_filename(audio_file.filename)
    if not filename:
        return jsonify({"error": "Audio filename invalid"}), 400

    local_audio_path = os.path.join("/tmp", filename)
    audio_file.save(local_audio_path)
    duration_from_frontend = request.form.get("duration_seconds")
    logger.info("userid and agentid %s %s", userid, agentid)

    # container for results to include in final JSON
    result_container = {
        "audio_s3_path": None,
        "transcript_s3_path": None,
        "config_s3_key": None,
    }

    async def async_event_stream(userid):
        transcript_local_path = None
        config_local_path = None
        try:
            yield "data: Uploading audio...\n\n"
            # Upload audio
            audio_s3_path = upload_any_file(
                local_audio_path, user_id=userid, file_name=filename, type="audio"
            )
            result_container["audio_s3_path"] = audio_s3_path
            yield f"data: Audio uploaded: {audio_s3_path.get('s3_key', 'unknown')}\n\n"

            yield "data: Transcribing audio...\n\n"

            # token = current_user_id.set(userid)
            try:
                main_process = Speech2TextService(userid=userid)
                transcript_text = await main_process.transcribe_audio(local_audio_path)
                if not transcript_text:
                    yield "data: ERROR: Failed to transcribe audio\n\n"
                    return

                yield "data: Transcription done\n\n"

                # Evaluate transcript
                prompts = load_yaml_file(
                    path=pathconfig.agent_template
                )  # adjust if path param needed
                clean_transcription_prompt = prompts.get("clean_transcription_prompt")
                task_db = connect_to_rds()
                task_credits = Credits(task_db)
                val = await evaluate_transcript(
                    clean_transcription_prompt, transcript_text,task_credits, userid=userid
                )
                if not val:
                    yield "data: ERROR: Failed to evaluate transcript\n\n"
                    return
                yield "data: Transcript evaluation done\n\n"

                # Save transcript locally
                transcript_filename = f"{os.path.splitext(filename)[0]}_transcript.json"
                transcript_local_path = os.path.join("/tmp", transcript_filename)
                transcript_data = {
                    "id": str(uuid.uuid4().hex[:8]),
                    "filename": filename,
                    "date": datetime.utcnow().isoformat(timespec="seconds"),
                    "summary": val["clean_text"],
                    "transcript":transcript_text,
                    "heading": val["summary"],
                    "contacts": "All",
                }
                with open(transcript_local_path, "w", encoding="utf-8") as f:
                    json.dump(transcript_data, f, ensure_ascii=False, indent=2)
                yield "data: Transcript saved locally\n\n"

                # Embed transcript (async)
                ser = TrainLanceAgent(user_id=userid)
                await ser.embed_single_audio_json(
                    file_path=transcript_local_path, filename=transcript_filename, credits = task_credits
                )
                yield "data: Embedding complete\n\n"

                # Upload transcript
                transcript_s3_path = upload_any_file(
                    transcript_local_path,
                    user_id=userid,
                    file_name=transcript_filename,
                    type="audio",
                )
                result_container["transcript_s3_path"] = transcript_s3_path
                yield f"data: Transcript uploaded: {transcript_s3_path.get('s3_key','unknown')}\n\n"

                # Update or create config
                yield "data: Updating user config...\n\n"
                config_s3_key = fetch_document_link(agentid)
                if config_s3_key is None:
                    # create minimal config, write locally, upload and save link
                    config_obj = {"user_id": userid, "recordings": []}
                    config_basename = f"{uuid.uuid4().hex[:8]}.json"
                    config_local_path = os.path.join("/tmp", config_basename)
                    with open(config_local_path, "w", encoding="utf-8") as f:
                        json.dump(config_obj, f, ensure_ascii=False, indent=2)

                    uploaded = upload_any_file(
                        config_local_path,
                        user_id=userid,
                        file_name=config_basename,
                        type="audio",
                    )
                    # uploaded should contain s3_key; use update_agent_document_link to link
                    if not uploaded or "s3_key" not in uploaded:
                        yield "data: ERROR: Failed to upload new config\n\n"
                        return
                    update_agent_document_link(uploaded["s3_key"], agentid)
                    config_s3_key = uploaded["s3_key"]
                    # keep the local config path to overwrite soon after adding recording
                    config_local_path_for_overwrite = config_local_path
                else:
                    # config_s3_key may be the full key; fetch JSON from S3
                    # fetch_document_link may give list/tuple
                    config_s3_key = (
                        config_s3_key[0]
                        if isinstance(config_s3_key, (list, tuple))
                        else config_s3_key
                    )
                    config_obj = read_json_from_s3(config_s3_key) or {
                        "user_id": userid,
                        "recordings": [],
                    }
                    # prepare local path from basename
                    config_local_path_for_overwrite = _ensure_tmp_dir_for_s3key(
                        config_s3_key
                    )
                    # we'll write the updated config to this local path below before upload
            finally:
                # current_user_id.reset(token)
                print("commented current userid for audio stream")

            # Append new recording
            config_obj["recordings"].append(
                {
                    "id": transcript_data["id"],
                    "title": transcript_data["filename"],
                    "date": transcript_data["date"],
                    "preview": " ".join(transcript_text.split()[:20]),
                    "audio_location": audio_s3_path.get("s3_key"),
                    "transcript_location": transcript_s3_path.get("s3_key"),
                    "summary": val["summary"],
                    "clarifications": len(val.get("clarifications", [])),
                    "duration": duration_from_frontend or "unknown",
                    "contacts": transcript_data["contacts"],
                }
            )

            # Write updated config locally (use basename path) and upload with original S3 key
            with open(config_local_path_for_overwrite, "w", encoding="utf-8") as f:
                json.dump(config_obj, f, ensure_ascii=False, indent=2)

            # upload and preserve the original S3 key if present, otherwise use basename (uploaded earlier)
            upload_any_file(
                config_local_path_for_overwrite,
                user_id=userid,
                file_name=config_s3_key,  # keep same key so pointer remains valid
                type="audio",
            )
            yield "data: Config updated\n\n"

            result_container["config_s3_key"] = config_s3_key

            # Emit final JSON as FINAL_RESPONSE
            final_response = {
                "message": "Transcription successful",
                "audio_file": result_container["audio_s3_path"],
                "transcript_file": result_container["transcript_s3_path"],
                "config_updated": True,
                "config_s3_key": result_container.get("config_s3_key"),
            }
            yield f"data: FINAL_RESPONSE:{json.dumps(final_response)}\n\n"

        except Exception as e:
            logger.exception("Streaming error in process_audio")
            # send an error to the client via SSE and stop
            try:
                yield f"data: ERROR: {str(e)}\n\n"
            except Exception:
                pass
        finally:
            # cleanup
            _safe_remove(local_audio_path)
            _safe_remove(transcript_local_path)
            try:
                # config_local_path_for_overwrite might not exist in some branches
                _safe_remove(config_local_path_for_overwrite)
            except Exception:
                pass

    # wrapper to run async generator in sync context for Flask
    def wrapped_stream(userid):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        agen = async_event_stream(userid)
        try:
            while True:
                try:
                    chunk = loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break
                # ensure chunk is bytes/str
                if isinstance(chunk, str):
                    yield chunk
                else:
                    yield str(chunk)
        finally:
            try:
                loop.run_until_complete(agen.aclose())
            except Exception:
                pass
            loop.close()

    return Response(
        stream_with_context(wrapped_stream(userid)), content_type="text/event-stream"
    )


@audio_agent_bps.route("/update-transcript", methods=["POST"])
async def update_transcript():
    data = request.json or {}

    api_key = data.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    userid, agentid = get_user_agent_id(api_key)
    if not userid:
        return jsonify({"error": "User ID is required"}), 400

    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    filename = data.get("filename")  # title or file reference
    transcript_data = data.get("transcript")

    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    if not transcript_data:
        return jsonify({"error": "Transcript data is required"}), 400

    try:
        # Load user config JSON
        config_filename = fetch_document_link(agentid)
        if not config_filename:
            return jsonify({"error": "No audio config found for this user"}), 404

        config = read_json_from_s3(config_filename)
        if not config:
            return jsonify({"error": "User config file could not be read"}), 404

        update_loc = None
        for rec in config.get("recordings", []):
            #print("values", type(rec))
            #print("values", rec.get("title"), filename)
            if rec.get("title") == filename:
                update_loc = rec.get("transcript_location")
                rec["updated_date"] = datetime.utcnow().isoformat(timespec="seconds")
                break

        if not update_loc:
            return jsonify({"error": "Transcript not found in user config"}), 404

        # Load existing transcript
        transcript_maindata = read_json_from_s3(update_loc)
        if not transcript_maindata:
            return jsonify({"error": "Transcript data not found"}), 404

        # Update transcript text
        transcript_maindata["text"] = transcript_data

        # Save updated transcript locally
        local_transcript_path = "/tmp/temp_transcript.json"
        with open(local_transcript_path, "w", encoding="utf-8") as f:
            json.dump(transcript_maindata, f, ensure_ascii=False, indent=2)

        transcript_filename = f"{os.path.splitext(filename)[0]}_transcript.json"

        # re-embed (async)
        ser = TrainLanceAgent(user_id=userid)
        # token = current_user_id.set(userid)
        try:
            await ser.embed_single_audio_json(
                file_path=local_transcript_path, filename=transcript_filename
            )
        finally:
            # current_user_id.reset(token)
            print("commented update transcrpt current user id set")

        # Upload updated transcript
        upload_any_file(
            local_transcript_path,
            user_id=userid,
            file_name=extract_transcript_filename(update_loc),
            type="audio",
        )

        # Save updated config locally
        local_config_path = "/tmp/temp_config.json"
        with open(local_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        # Upload updated config
        upload_any_file(
            local_config_path,
            user_id=userid,
            file_name=config_filename,
            type="audio",
        )

        # Cleanup
        os.remove(local_config_path)
        os.remove(local_transcript_path)

        return (
            jsonify(
                {
                    "message": "Transcript updated successfully",
                    "changed_filename": filename,
                    "config_updated": True,
                }
            ),
            200,
        )

    except FileNotFoundError:
        return jsonify({"error": "Transcript file not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@audio_agent_bps.route("/update-audio-contacts", methods=["POST"])
async def update_audio_contacts():
    data = request.json or {}
    api_key = data.get("api_key")
    filename = data.get("filename")
    new_contacts = data.get("contacts")

    # --- Validations ---
    if not api_key:
        return jsonify({"error": "API key is required"}), 400

    userid, agentid = get_user_agent_id(api_key)
    if not userid:
        return jsonify({"error": "Invalid API key"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    if not new_contacts:
        return jsonify({"error": "Contacts is required"}), 400

    try:
        # --- 1. Fetch config.json linked to this agent ---
        config_s3_key = fetch_document_link(agentid)
        if not config_s3_key:
            return jsonify({"error": "Config not found for this agent"}), 404

        # config_s3_key may be inside a list/tuple
        config_s3_key = (
            config_s3_key[0]
            if isinstance(config_s3_key, (list, tuple))
            else config_s3_key
        )

        config_obj = read_json_from_s3(config_s3_key)
        if not config_obj:
            return jsonify({"error": "Failed to load config"}), 500

        recordings = config_obj.get("recordings", [])

        # --- 2. Locate recording by filename ---
        found = None
        for r in recordings:
            if r.get("title") == filename:
                found = r
                break

        if not found:
            return jsonify({"error": "Recording not found in config"}), 404

        # --- 3. Update contacts field ---
        found["contacts"] = new_contacts

        transcript_filename = f"{os.path.splitext(filename)[0]}_transcript.json"
        transcript_local_path = os.path.join("/tmp", transcript_filename)
        transcriptobj = read_json_from_s3(f"{userid}/aud_scripts/{transcript_filename}")
        transcriptobj["contacts"] = new_contacts

        with open(transcript_local_path, "w", encoding="utf-8") as f:
            json.dump(transcriptobj, f, ensure_ascii=False, indent=2)
        transcript_s3_path = upload_any_file(
            transcript_local_path,
            user_id=userid,
            file_name=transcript_filename,
            type="audio",
        )

        # --- 4. Save updated config locally ---
        config_local_path = _ensure_tmp_dir_for_s3key(config_s3_key)
        with open(config_local_path, "w", encoding="utf-8") as f:
            json.dump(config_obj, f, ensure_ascii=False, indent=2)

        # --- 5. Upload back to S3 keeping same key ---
        upload_any_file(
            config_local_path,
            user_id=userid,
            file_name=config_s3_key,
            type="audio",
        )

        # --- 6. Update LanceDB entry ---
        ser = LanceDBServer()
        await ser.update_contacts_for_audio(
            user_id=userid, filename=transcript_filename, contacts=new_contacts
        )

        return jsonify(
            {
                "message": "Contacts updated successfully",
                "filename": filename,
                "contacts": new_contacts,
                "config_s3_key": config_s3_key,
            }
        )

    except Exception as e:
        logger.exception("Error in update_audio_contacts")
        return jsonify({"error": str(e)}), 500


@audio_agent_bps.route("/get-audio-config", methods=["GET"])
def get_audio_config():
    api_key = request.args.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    userid, agentid = get_user_agent_id(api_key)
    if not userid:
        return jsonify({"error": "User ID is required"}), 400
    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    config_filename = fetch_document_link(agentid)
    #print("config filename", config_filename)
    if not config_filename:
        return jsonify({"error": "No audios found for this user"}), 404
    try:
        config = read_json_from_s3(config_filename)
        if config:
            for rec in config.get("recordings", []):
                # Convert S3 paths to public URLs
                rec["audio_location"] = attach_CLDFRNT_url(rec["audio_location"])
                rec["transcript_location"] = attach_CLDFRNT_url(
                    rec["transcript_location"]
                )
            return jsonify(config), 200
        else:
            return {"recordings": []}, 200
    except FileNotFoundError:
        return jsonify({"error": "No audio config found for this user"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@audio_agent_bps.route("/delete-audio", methods=["DELETE"])
async def delete_audio():
    data = request.json
    api_key = data.get("api_key")
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    userid, agentid = get_user_agent_id(api_key)

    if not userid:
        return jsonify({"error": "User ID is required"}), 400

    if not check_userid_valid(userid):
        return jsonify({"error": "Invalid access"}), 404

    audio_location = data.get("audio_location")
    if not audio_location:
        return jsonify({"error": "Audio location is required"}), 400

    try:
        # 🔹 Load user config
        config_filename = fetch_document_link(agentid)
        if not config_filename:
            return jsonify({"error": "No audio config found for this user"}), 404

        config = read_json_from_s3(config_filename)

        # 🔹 Find matching recording
        recording_to_delete = None
        filename = None
        for rec in config.get("recordings", []):
            if rec.get("audio_location") in audio_location:
                filename = rec.get("title")
                recording_to_delete = rec
                break

        if not recording_to_delete:
            return jsonify({"error": "Recording not found in config"}), 404

        ser = TrainLanceAgent(user_id=userid)
        await ser.delete_rec_lance(recording_to_delete["id"])

        # 🔹 Delete audio + transcript from S3
        delete_file_from_s3(recording_to_delete["audio_location"])
        delete_file_from_s3(recording_to_delete["transcript_location"])

        # 🔹 Remove from config
        config["recordings"].remove(recording_to_delete)

        # 🔹 Save updated config
        local_config_path = "/tmp/temp_config.json"
        with open(local_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        upload_any_file(
            local_config_path, user_id=userid, file_name=config_filename, type="audio"
        )
        # delete_agent_document_link(userid)
        os.remove(local_config_path)
        if filename:
            success = deletefilebasedData(filename, userid)
            if not success:
                logger.warning(
                    f"Failed to delete question entries for user {userid}, file {filename}"
                )

        return (
            jsonify(
                {
                    "message": "Audio and transcript deleted successfully",
                    "config_updated": True,
                }
            ),
            200,
        )

    except FileNotFoundError:
        return jsonify({"error": "Config file not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


async def test_queryies():
    pass
