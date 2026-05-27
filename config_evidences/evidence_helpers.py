import json
import os
import uuid
import re
import logging
from utils.normal import parse_composite_user_id
from utils.app_configs import ACCESSIBLE_IDS
from utils.s3_utils import save_any_s3, read_json_from_s3
from radar.radar_helpers import process_file_payloads
from utils.fireworkzz import get_extract_response
from credits_route.route import Credits
from db.rds_db import connect_to_rds
from db.lance_db_service import LanceDBServer
from utils.key_rotation_manager import SecureKMSService as _EvKMSService

logger = logging.getLogger(__name__)

_ev_kms = _EvKMSService()
_EV_TEXT_FIELDS = ("artifact", "nature", "primaryUse", "expectations")


def _enc_ev(user_id: str, v):
    if not v or not isinstance(v, str):
        return v
    return json.dumps(_ev_kms.encrypt(user_id, v))


def _dec_ev(user_id: str, v):
    if not v or not isinstance(v, str):
        return v
    try:
        d = json.loads(v)
        if isinstance(d, dict) and "ciphertext" in d:
            return _ev_kms.decrypt(user_id, d["encrypted_key"], d["iv"], d["ciphertext"])
    except Exception:  # noqa: S110
        pass
    return v


def _is_ev_enc(v) -> bool:
    try:
        d = json.loads(v)
        return isinstance(d, dict) and "ciphertext" in d
    except Exception:
        return False


def _encrypt_evidence_list(user_id: str, evidence_list: list) -> list:
    result = []
    for entry in evidence_list:
        enc = dict(entry)
        for f in _EV_TEXT_FIELDS:
            if enc.get(f):
                enc[f] = _enc_ev(user_id, enc[f])
        result.append(enc)
    return result


def _decrypt_evidence_list(user_id: str, evidence_list: list) -> tuple:
    was_migrated = False
    for entry in evidence_list:
        for f in _EV_TEXT_FIELDS:
            raw = entry.get(f, "")
            if raw and not _is_ev_enc(raw):
                was_migrated = True
            entry[f] = _dec_ev(user_id, raw)
    return evidence_list, was_migrated


# ============================================================
# Evidence CRUD Helpers
# ============================================================
def _load_default_evidence():
    try:
        file_path = os.path.join(os.path.dirname(__file__), "evidence_default.json")
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading default evidence: {e}", exc_info=True)
        return []


def _get_evidence_s3_key(user_id):
    return f"{user_id}/evidence/userevidence.json"


def _get_user_evidence(user_id):
    try:
        s3_key = _get_evidence_s3_key(user_id)
        user_evidence = read_json_from_s3(s3_key)
        if user_evidence:
            if isinstance(user_evidence, list):
                user_evidence, was_migrated = _decrypt_evidence_list(user_id, user_evidence)
                if was_migrated:
                    try:
                        _save_user_evidence(user_id, user_evidence)
                    except Exception:
                        pass
            return user_evidence, True
    except Exception as e:
        logger.info(f"User evidence not found or error reading: {e}")

    return _load_default_evidence(), False


def get_only_evidence(user_id):
    is_super_admin = user_id in ACCESSIBLE_IDS

    if is_super_admin:
        evidence = _load_default_evidence()
    else:
        evidence, _ = _get_user_evidence(user_id)
    return evidence


def _save_user_evidence(user_id, data):
    try:
        enc_data = _encrypt_evidence_list(user_id, data) if isinstance(data, list) else data
        tmp_path = f"/tmp/userevidence_{user_id}_{uuid.uuid4()}.json"
        with open(tmp_path, "w") as f:
            json.dump(enc_data, f)

        s3_key = _get_evidence_s3_key(user_id)
        result = save_any_s3(tmp_path, s3_key)

        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        return result
    except Exception as e:
        logger.error(f"Error saving user evidence: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


def _update_entry_by_id(evidence_list, entry_id, expectations):
    for entry in evidence_list:
        if entry.get("id") == entry_id:
            entry["expectations"] = expectations
            return evidence_list

    raise ValueError(f"Evidence entry not found: id={entry_id}")


def _delete_entry_by_id(evidence_list, entry_id):
    for i, entry in enumerate(evidence_list):
        if entry.get("id") == entry_id:
            deleted_entry = evidence_list.pop(i)
            return evidence_list, deleted_entry

    raise ValueError(f"Evidence entry not found: id={entry_id}")


def _validate_evidence_entry(entry_data):
    required_keys = {
        "type",
        "number",
        "artifact",
        "nature",
        "primaryUse",
        "expectations",
    }
    missing_keys = required_keys - set(entry_data.keys())
    if missing_keys:
        raise ValueError(f"Missing required keys: {', '.join(sorted(missing_keys))}")
    return True


def _add_entry(evidence_list, entry_data):
    _validate_evidence_entry(entry_data)

    if not isinstance(entry_data.get("number"), int):
        raise ValueError("number must be an integer")

    new_entry = {
        "id": str(max([int(e.get("id", 0)) for e in evidence_list]) + 1),
        **entry_data,
    }

    evidence_list.append(new_entry)
    return evidence_list, new_entry


# ============================================================
# Evidence Check Analysis
# ============================================================
async def handle_evidence_data(
    extracted_payload, evidence_list, allowed, disallowed, user_id, credits
):
    """
    LLM analysis function for evidence compliance (like reduce_data_for_report).
    Handles large files via chunking through get_extract_response.
    """
    file_text = "\n\n".join(
        [
            e.get("content", "") or e.get("text", "")
            for e in extracted_payload
            if isinstance(e, dict)
        ]
    )

    prompt_template = """You are an evidence compliance auditor.

EVIDENCE EXPECTATIONS LIST (what each artifact should contain):
{evidence_list}

ALLOWED EVIDENCE TYPES: {allowed_list}
DISALLOWED EVIDENCE TYPES: {disallowed_list}

DOCUMENT CONTENT:
{{data}}

TASK:
Analyze the document and generate a JSON array of confirmation questions:
1. For each DISALLOWED evidence artifact found in the document — ask what to do with it.
2. For each REQUIRED artifact from the expectations list that is MISSING — ask what to do.
3. Verify found artifacts match their Expectations field; flag mismatches as questions.

Return ONLY a valid JSON array (no markdown fences):
[
  {{
    "section": "<evidence type category>",
    "subsection": "<artifact name>",
    "question_number": "<N>",
    "question": "<specific question>",
    "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
    "discard_process": ["<letters that mean discard/skip>"],
    "user_answer": null,
    "comment": null
  }}
]

For DISALLOWED evidence found, use these options:
  A: "I will remove it manually"
  B: "Don't use it (discard)"
  C: "Use it anyway"
  D: "Custom explanation"
  discard_process: ["B"]

For MISSING required section, use:
  A: "I will upload a new file for this section"
  B: "This section is not needed"
  C: "Custom explanation"
  discard_process: ["B"]

For EXPECTATION MISMATCH, use:
  A: "I will fix and re-upload"
  B: "Accept as-is"
  C: "Custom explanation"
  discard_process: ["A"]
""".format(
        evidence_list=json.dumps(evidence_list, indent=2),
        allowed_list=json.dumps([c.get("artifact") for c in allowed]),
        disallowed_list=json.dumps([c.get("artifact") for c in disallowed]),
    )

    result = await get_extract_response(
        prompt_template=prompt_template,
        data=file_text,
        user_id=user_id,
        credits=credits,
    )
    return result


def _extract_json_array(text):
    """Extract JSON array from text (handles markdown fences)."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    return match.group(0) if match else "[]"


async def run_evidence_check_job(data, job_id=None):
    """
    Background job: analyze uploaded file against evidence config.
    """
    try:
        user_id = data.get("user_id")
        logged_in_user_id, user_id = parse_composite_user_id(user_id)
        runbook_id = data.get("runbook_id")
        file_data = data.get("file")

        # Step 1: Extract file via process_file_payloads
        inp_links = []
        extracted_payload = []
        process_file_payloads(
            user_id=user_id,
            files=[file_data],
            inp_links=inp_links,
            extracted_payload=extracted_payload,
        )

        # Step 2: Fetch runbook + parse evidence config
        dbserver = LanceDBServer()
        runbook_list = await dbserver.get_runbook_by_id(user_id, runbook_id)
        runbook = runbook_list[0] if runbook_list else {}

        raw_config = runbook.get("runbook_evidence_config", "") or ""
        evidence_config_raw = json.loads(raw_config) if raw_config else []

        if isinstance(evidence_config_raw, list):
            configurations = evidence_config_raw
            existing_meta = {}
        else:
            configurations = evidence_config_raw.get("configurations", [])
            existing_meta = evidence_config_raw

        allowed = [c for c in configurations if c.get("Decision") == True]
        disallowed = [c for c in configurations if c.get("Decision") == False]

        # Step 3: Get user evidence expectations
        user_evidence = get_only_evidence(user_id)

        # Step 4: Run AI analysis
        conn = connect_to_rds()
        credits = Credits(conn)
        raw_response = await handle_evidence_data(
            extracted_payload, user_evidence, allowed, disallowed, user_id, credits
        )

        # Step 5: Parse Q&A JSON
        qa_list = json.loads(_extract_json_array(raw_response))

        # Step 6: Save Q&A back to runbook
        existing_meta["user_allowed_details"] = qa_list
        if configurations:
            existing_meta["configurations"] = configurations

        await dbserver.update_runbook(
            user_id, runbook_id, {"runbook_evidence_config": json.dumps(existing_meta)}
        )

        # Step 7: Return
        return {
            "status": "completed",
            "qa_list": qa_list,
            "total_questions": len(qa_list),
        }

    except Exception as e:
        logger.error(f"Evidence check job failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}
