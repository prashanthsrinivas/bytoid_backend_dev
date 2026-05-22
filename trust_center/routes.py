import asyncio
import io
import json
import os
import re
import threading
import traceback
import uuid
import yaml
from datetime import datetime, timezone

import boto3
import pymysql
import pymysql.cursors
from botocore.config import Config as BotocoreConfig
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required

from flask import Blueprint, g, jsonify, request

from websockets_custom.ws_instance import ws_service, msg_builder_main
from db.rds_db import connect_to_rds
from utils.base_logger import get_logger
from utils.s3_utils import (
    attach_CLDFRNT_url,
    delete_file_from_s3,
    generate_presigned_url,
    list_all_files,
    load_yaml_from_s3,
    s3bucket,
)
from services.audit_log_service import (
    log_audit_event,
    build_audit_actor,
    TRUST_CENTER_INTERNAL_SHARED,
    TRUST_CENTER_INTERNAL_SHARE_REVOKED,
)
from shared_configuration import (
    check_role_has_permission,
    core_assign_resource,
    core_list_resource_shares,
    core_revoke_resource,
    get_round_robin_user_for_resource,
    get_user_resource_access,
    get_user_shared_resources,
)

S3_BUCKET = os.getenv("S3_BUCKET")
logger = get_logger(__name__)

trust_center_bp = Blueprint("trust_center", __name__, url_prefix="/trust-center")

_jobs_lock = threading.Lock()

_bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-2",
    config=BotocoreConfig(
        read_timeout=300, connect_timeout=60, retries={"max_attempts": 2}
    ),
)
_THINK_MODEL = "moonshotai.kimi-k2.5"
_CHUNK_WORDS = 1000

DEFAULT_NDA_TEMPLATE = """NON-DISCLOSURE AGREEMENT

This Non-Disclosure Agreement ("Agreement") is entered into as of the date of electronic acceptance by the party viewing this Trust Center ("Recipient") and the organization providing access ("Disclosing Party").

1. CONFIDENTIAL INFORMATION
"Confidential Information" means any and all non-public information, including but not limited to compliance documentation, security policies, audit reports, certifications, system architecture details, and any other materials made available through this Trust Center.

2. OBLIGATIONS OF RECIPIENT
Recipient agrees to: (a) hold all Confidential Information in strict confidence; (b) not disclose Confidential Information to any third party without prior written consent of the Disclosing Party; (c) use Confidential Information solely to evaluate the Disclosing Party's security and compliance posture for the purpose of establishing or maintaining a business relationship.

3. PERMITTED USE
Recipient may use Confidential Information only for internal evaluation purposes. Recipient shall not copy, reproduce, or distribute any Confidential Information except as required for that evaluation.

4. TERM
This Agreement shall remain in effect for a period of two (2) years from the date of acceptance. The confidentiality obligations shall survive termination or expiration of this Agreement.

5. REMEDIES
Recipient acknowledges that any breach of this Agreement may cause irreparable harm to the Disclosing Party and that monetary damages may be inadequate. Accordingly, the Disclosing Party shall be entitled to seek equitable relief, including injunction and specific performance, in addition to all other remedies available at law or in equity.

6. GENERAL
This Agreement shall be governed by applicable law. If any provision of this Agreement is found to be unenforceable, the remaining provisions shall continue in full force and effect. This Agreement constitutes the entire agreement between the parties with respect to its subject matter.

By clicking "Accept & View", you agree to be bound by the terms of this Non-Disclosure Agreement.
"""


# ── S3 key helpers ────────────────────────────────────────────────────────────


def _wp_key(user_id: str) -> str:
    return f"{user_id}/trust-center/whitepaper.yaml"


def _doc_key(user_id: str, doc_id: str, ext: str) -> str:
    return f"{user_id}/trust-center/documents/{doc_id}.{ext}"


def _job_s3_key(job_id: str) -> str:
    return f"trust_center_jobs/{job_id}.json"


# ── S3 helpers ────────────────────────────────────────────────────────────────


def _write_yaml_to_s3(key: str, data: dict):
    s3 = s3bucket()
    yaml_bytes = yaml.safe_dump(data, sort_keys=False).encode("utf-8")
    s3.upload_fileobj(io.BytesIO(yaml_bytes), S3_BUCKET, key)


def _write_json_to_s3(key: str, data: dict):
    s3 = s3bucket()
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    s3.upload_fileobj(io.BytesIO(body), S3_BUCKET, key)


def _read_job(job_id: str) -> dict | None:
    try:
        s3 = s3bucket()
        obj = s3.get_object(Bucket=S3_BUCKET, Key=_job_s3_key(job_id))
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _save_job(job_id: str, state: dict):
    with _jobs_lock:
        _write_json_to_s3(_job_s3_key(job_id), state)


def _ws_emit(
    user_id: str,
    job_id: str,
    session_id: str | None,
    stage: str,
    message: str,
    progress: int,
):
    if not session_id:
        return
    try:
        msg = msg_builder_main.job_progress(
            job_id, session_id, stage, message, progress
        )
        asyncio.run(
            ws_service.emit(
                user_id=user_id,
                message=msg["message"],
                scope=msg["scope"],
                session_id=msg["session_id"],
                job_id=msg["job_id"],
                msg_type=msg["type"],
                stage=msg["stage"],
                progress=msg["progress"],
                feature="trust_center",
            )
        )
    except Exception:
        pass


# ── Auth helper ───────────────────────────────────────────────────────────────


def _get_user_id() -> str | None:
    return (
        getattr(g, "user_id", None)
        or getattr(g, "session_user_id", None)
        or request.form.get("user_id")
        or request.args.get("user_id")
        or (request.get_json(silent=True) or {}).get("user_id")
    )


def _check_trust_center_access(baseuser, need_level):
    """
    Resolve the trust-center owner and ensure the requester has the required access.

    `need_level` is 'view' or 'edit'. 'edit' grants 'view' implicitly; a 'view'
    share entry does NOT satisfy an 'edit' requirement.

    Returns (owner_user_id, error_tuple).
    """
    if not baseuser:
        return None, (jsonify({"error": "Unauthorized"}), 401)

    logged_in_user_id, owner_id = parse_composite_user_id(baseuser)
    if not owner_id:
        return None, (jsonify({"error": "Invalid user_id"}), 400)

    if not logged_in_user_id or logged_in_user_id == owner_id:
        return owner_id, None

    access = get_user_resource_access(
        "trust_center", owner_id, owner_id, logged_in_user_id
    )
    if not access.get("granted"):
        return None, (
            jsonify({"error": "Access to this trust center has not been granted"}),
            403,
        )
    if need_level == "edit" and access.get("level") != "edit":
        return None, (
            jsonify({"error": "Edit access required"}),
            403,
        )
    return owner_id, None


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_trust_center(owner_user_id: str) -> dict | None:
    conn = connect_to_rds()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "SELECT * FROM trust_centers WHERE owner_user_id=%s", (owner_user_id,)
        )
        row = cursor.fetchone()
    conn.close()
    return row


def _upsert_trust_center(owner_user_id: str, wp_key: str) -> str:
    tc_id = uuid.uuid4().hex[:12]
    conn = connect_to_rds()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "SELECT id FROM trust_centers WHERE owner_user_id=%s", (owner_user_id,)
        )
        existing = cursor.fetchone()
        if existing:
            tc_id = existing["id"]
            cursor.execute(
                "UPDATE trust_centers SET whitepaper_s3_key=%s, updated_at=NOW() WHERE id=%s",
                (wp_key, tc_id),
            )
        else:
            cursor.execute(
                """INSERT INTO trust_centers (id, owner_user_id, whitepaper_s3_key)
                   VALUES (%s, %s, %s)""",
                (tc_id, owner_user_id, wp_key),
            )
    conn.commit()
    conn.close()
    return tc_id


def _get_documents(trust_center_id: str) -> list:
    conn = connect_to_rds()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "SELECT * FROM trust_center_documents WHERE trust_center_id=%s ORDER BY uploaded_at DESC",
            (trust_center_id,),
        )
        rows = cursor.fetchall()
    conn.close()
    return rows or []


# ── White paper generation ────────────────────────────────────────────────────


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _parse_ai_result(text: str) -> dict:
    text = re.sub(r"```json", "", text.strip())
    text = re.sub(r"```", "", text)
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _summarize_policy(title: str, ptype: str, content: str) -> dict:
    try:
        plain_text = _strip_html(content)
        if not plain_text:
            return {"summary": "", "category": "Other"}

        words = plain_text.split()
        chunks = [
            " ".join(words[i : i + _CHUNK_WORDS])
            for i in range(0, len(words), _CHUNK_WORDS)
        ]

        if not chunks:
            return {"summary": "", "category": "Other"}

        num_chunks = len(chunks)
        conversation_history = []

        for i, chunk in enumerate(chunks):
            if i < num_chunks - 1:
                chunk_prompt = f"""Here is part {i + 1}/{num_chunks} of a {ptype} titled '{title}'. Read and understand it fully. Briefly acknowledge the key points covered.

{chunk}"""
                conversation_history.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": chunk_prompt}],
                    }
                )
            else:
                final_prompt = f"""You are a compliance expert writing for a business audience — executives, strategists, and enterprise customers.

This is the final part ({i + 1}/{num_chunks}) of a {ptype} titled '{title}'.

Based on everything you have read, return a JSON object with exactly two keys:

1. "category": Assign this document to exactly ONE of these categories based on its primary focus:
   - "Governance & Compliance"
   - "Access & Identity Security"
   - "Data Protection & Privacy"
   - "Operational Resilience"
   - "Threat & Incident Management"
   - "People & Third-Party Security"

2. "summary": Write a 2–3 paragraph executive overview in plain, confident business prose.
   - No bullet points, no headings, no markdown inside the summary value.
   - Write from the angle that makes THIS document distinctive — its specific purpose, scope, or protection.
   - Vary your vocabulary and sentence openings. Do not use templated openers like "This policy establishes..." or "This procedure ensures..." across multiple documents.
   - Speak to what a senior executive or enterprise customer would find most relevant.

Return ONLY valid JSON. No extra text, no markdown fences.

Text:
{chunk}"""
                conversation_history.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": final_prompt}],
                    }
                )

            payload = {
                "messages": conversation_history,
                "temperature": 0.1,
                "top_p": 0.95,
                "max_tokens": 4096,
            }

            response = _bedrock.invoke_model(
                modelId=_THINK_MODEL,
                body=json.dumps(payload),
                contentType="application/json",
                accept="application/json",
            )

            body = json.loads(response["body"].read())
            response_text = (
                body.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )

            if response_text:
                conversation_history.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": response_text}],
                    }
                )

        if conversation_history and len(conversation_history) > 0:
            last_response = conversation_history[-1].get("content", [{}])
            if isinstance(last_response, list) and len(last_response) > 0:
                response_text = last_response[0].get("text", "").strip()
                result = _parse_ai_result(response_text)
                return {
                    "summary": result.get("summary", "").strip(),
                    "category": result.get("category", "Other").strip(),
                }

        return {"summary": "", "category": "Other"}
    except Exception as exc:
        logger.warning("Policy summarization failed: %s", exc)
        return {
            "summary": _strip_html(content)[:800],
            "category": "Other",
        }


def _build_whitepaper_html(policies: list) -> str:
    from collections import defaultdict

    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    _CATEGORY_ORDER = [
        "Governance & Compliance",
        "Access & Identity Security",
        "Data Protection & Privacy",
        "Operational Resilience",
        "Threat & Incident Management",
        "People & Third-Party Security",
    ]

    if not policies:
        policies_html = """
        <section class="section">
            <p class="placeholder">No compliance policies have been added yet.
            You can write your white paper content manually below.</p>
        </section>
        """
    else:
        categories = defaultdict(list)
        for p in policies:
            cat = p.get("ai_category", "Other")
            categories[cat].append(p)

        sections = []
        for cat in _CATEGORY_ORDER:
            if cat not in categories:
                continue
            sections.append(f'<h2 class="category-heading">{cat}</h2>')
            for p in categories[cat]:
                title = p.get("title", "Untitled Policy")
                ptype = p.get("type", "")
                summary = p.get("ai_summary") or _strip_html(p.get("content", ""))[:800]
                sections.append(f"""
            <section class="section">
                <h3>{title}</h3>
                <p>{summary}</p>
            </section>
            """)

        for cat in sorted(categories.keys()):
            if cat in _CATEGORY_ORDER:
                continue
            sections.append(f'<h2 class="category-heading">{cat}</h2>')
            for p in categories[cat]:
                title = p.get("title", "Untitled Policy")
                ptype = p.get("type", "")
                summary = p.get("ai_summary") or _strip_html(p.get("content", ""))[:800]
                sections.append(f"""
            <section class="section">
                <h3>{title}</h3>
                <p>{summary}</p>
            </section>
            """)

        policies_html = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<style>
  body {{ font-family: 'Segoe UI', sans-serif; max-width: 900px; margin: 0 auto; padding: 40px; color: #1a1a2e; }}
  h1 {{ font-size: 2rem; border-bottom: 2px solid #6366f1; padding-bottom: 12px; }}
  h2 {{ font-size: 1.2rem; color: #4338ca; margin-top: 28px; }}
  h3 {{ font-size: 1rem; color: #1a1a2e; margin-top: 16px; margin-bottom: 8px; }}
  .category-heading {{ font-size: 1.3rem; color: #1a1a2e; border-bottom: 2px solid #6366f1; padding-bottom: 6px; margin-top: 40px; margin-bottom: 16px; }}
  .meta {{ color: #6b7280; font-size: 0.9rem; margin-bottom: 32px; }}
  .section {{ margin-bottom: 24px; padding: 16px 20px; border-left: 3px solid #e0e7ff; background: #fafafa; border-radius: 4px; }}
  .badge {{ display: inline-block; background: #e0e7ff; color: #4338ca; font-size: 0.75rem;
            padding: 2px 8px; border-radius: 999px; margin-bottom: 8px; }}
  .type-badge {{ display: inline-block; background: #f3f4f6; color: #6b7280; font-size: 0.7rem; padding: 2px 8px; border-radius: 999px; margin-left: 6px; }}
  .placeholder {{ color: #9ca3af; font-style: italic; }}
</style>
</head>
<body>
  <h1>Trust &amp; Compliance White Paper</h1>
  <p class="meta">Generated: {now_str} &nbsp;|&nbsp; Policies: {len(policies)}</p>
  {policies_html}
</body>
</html>"""


def _generate_whitepaper(user_id: str, job_id: str, session_id: str | None = None):
    try:
        _save_job(job_id, {"status": "running", "job_id": job_id})
        _ws_emit(
            user_id,
            job_id,
            session_id,
            "loading",
            "Loading your compliance policies...",
            5,
        )

        keys = list_all_files(folder=f"{user_id}/policies/")
        yaml_keys = [k["Key"] for k in (keys or []) if k["Key"].endswith(".yaml")]

        policies = []
        for key in yaml_keys:
            try:
                data = load_yaml_from_s3(key)
                if data:
                    policies.append(data)
            except Exception:
                continue

        num_policies = len(policies)
        for i, policy in enumerate(policies):
            title = policy.get("title", "Untitled")
            ptype = policy.get("type", "policy")
            progress = 10 + int((i / num_policies) * 75) if num_policies > 0 else 10
            _ws_emit(
                user_id,
                job_id,
                session_id,
                "summarizing",
                f"Summarizing {ptype} '{title}'... ({i + 1}/{num_policies})",
                progress,
            )
            result = _summarize_policy(
                title,
                ptype,
                policy.get("content", ""),
            )
            policy["ai_summary"] = result.get("summary", "")
            policy["ai_category"] = result.get("category", "Other")

        _ws_emit(
            user_id, job_id, session_id, "building", "Building your whitepaper...", 88
        )
        html = _build_whitepaper_html(policies)
        wp_key = _wp_key(user_id)

        _ws_emit(user_id, job_id, session_id, "saving", "Saving to cloud...", 95)
        _write_yaml_to_s3(
            wp_key,
            {
                "title": "Trust & Compliance White Paper",
                "content": html,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "policy_count": len(policies),
            },
        )

        tc_id = _upsert_trust_center(user_id, wp_key)
        _save_job(
            job_id, {"status": "done", "job_id": job_id, "trust_center_id": tc_id}
        )
        _ws_emit(
            user_id,
            job_id,
            session_id,
            "done",
            "Your Trust Center whitepaper is ready!",
            100,
        )
    except Exception as exc:
        logger.exception("Whitepaper generation failed: %s", exc)
        _save_job(job_id, {"status": "error", "job_id": job_id, "error": str(exc)})


# ── Endpoints ─────────────────────────────────────────────────────────────────


@trust_center_bp.route("", methods=["GET"])
@permission_required("trustcenter.view")
def get_trust_center():
    baseuser = _get_user_id()
    user_id, err = _check_trust_center_access(baseuser, "view")
    if err:
        return err

    tc = _get_trust_center(user_id)

    if tc and tc.get("whitepaper_s3_key"):
        try:
            wp_data = load_yaml_from_s3(tc["whitepaper_s3_key"]) or {}
        except Exception:
            wp_data = {}

        documents = _get_documents(tc["id"])
        doc_list = []
        for doc in documents:
            doc_list.append(
                {
                    "id": doc["id"],
                    "label": doc["label"],
                    "file_type": doc["file_type"],
                    "uploaded_at": str(doc["uploaded_at"]),
                    "download_url": generate_presigned_url(doc["s3_key"]),
                    "view_url": attach_CLDFRNT_url(doc["s3_key"]),
                }
            )

        return jsonify(
            {
                "trust_center_id": tc["id"],
                "whitepaper": wp_data,
                "documents": doc_list,
                "nda_content": tc.get("nda_content") or DEFAULT_NDA_TEMPLATE,
                "generating": False,
            }
        )

    # Need to generate whitepaper
    job_id = uuid.uuid4().hex[:12]
    session_id = request.args.get("session_id") or (
        request.get_json(silent=True) or {}
    ).get("session_id")
    thread = threading.Thread(
        target=_generate_whitepaper, args=(user_id, job_id, session_id), daemon=True
    )
    thread.start()

    return jsonify({"generating": True, "job_id": job_id})


@trust_center_bp.route("/status", methods=["GET"])
@permission_required("trustcenter.view")
def get_status():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    state = _read_job(job_id)
    if not state:
        return jsonify({"status": "not_found"}), 404

    if state.get("status") == "done":
        user_id = _get_user_id()
        tc = _get_trust_center(user_id) if user_id else None
        if tc:
            try:
                wp_data = load_yaml_from_s3(tc["whitepaper_s3_key"]) or {}
            except Exception:
                wp_data = {}
            documents = _get_documents(tc["id"])
            doc_list = [
                {
                    "id": d["id"],
                    "label": d["label"],
                    "file_type": d["file_type"],
                    "uploaded_at": str(d["uploaded_at"]),
                    "download_url": generate_presigned_url(d["s3_key"]),
                    "view_url": attach_CLDFRNT_url(d["s3_key"]),
                }
                for d in documents
            ]
            return jsonify(
                {
                    "status": "done",
                    "trust_center_id": tc["id"],
                    "whitepaper": wp_data,
                    "documents": doc_list,
                    "nda_content": tc.get("nda_content") or DEFAULT_NDA_TEMPLATE,
                }
            )

    return jsonify(state)


@trust_center_bp.route("/whitepaper", methods=["PATCH"])
@permission_required("trustcenter.whitepaper.edit")
def patch_whitepaper():
    data = request.get_json(silent=True) or {}
    baseuser = _get_user_id()
    user_id, err = _check_trust_center_access(baseuser, "edit")
    if err:
        return err

    content = data.get("content")
    if not content:
        return jsonify({"error": "content required"}), 400

    wp_key = _wp_key(user_id)
    try:
        existing = load_yaml_from_s3(wp_key) or {}
    except Exception:
        existing = {}

    existing["content"] = content
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()

    _write_yaml_to_s3(wp_key, existing)
    _upsert_trust_center(user_id, wp_key)

    return jsonify({"ok": True})


@trust_center_bp.route("/whitepaper/regenerate", methods=["POST"])
@permission_required("trustcenter.whitepaper.regenerate")
def regenerate_whitepaper():
    baseuser = _get_user_id()
    user_id, err = _check_trust_center_access(baseuser, "edit")
    if err:
        return err
    job_id = uuid.uuid4().hex[:12]
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    thread = threading.Thread(
        target=_generate_whitepaper, args=(user_id, job_id, session_id), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "generating": True})


@trust_center_bp.route("/whitepaper/pdf", methods=["GET"])
@permission_required("trustcenter.document.download")
def get_whitepaper_pdf():
    baseuser = _get_user_id()
    user_id, err = _check_trust_center_access(baseuser, "view")
    if err:
        return err
    wp_key = _wp_key(user_id)
    try:
        data = load_yaml_from_s3(wp_key) or {}
        html = data.get("content", "<p>No content</p>")
    except Exception:
        html = "<p>No content</p>"

    from flask import Response

    return Response(html, mimetype="text/html")


@trust_center_bp.route("/documents", methods=["POST"])
@permission_required("trustcenter.document.upload")
def upload_document():
    baseuser = _get_user_id()
    user_id, err = _check_trust_center_access(baseuser, "edit")
    if err:
        return err

    file = request.files.get("file")
    label = request.form.get("label", "").strip()

    if not file:
        return jsonify({"error": "file required"}), 400
    if not label:
        return jsonify({"error": "label required"}), 400

    mime = file.mimetype or ""
    if not (mime == "application/pdf" or mime.startswith("image/")):
        return jsonify({"error": "Only PDF and image files are supported"}), 400

    file_type = "pdf" if mime == "application/pdf" else "image"
    original_name = file.filename or "upload"
    ext = (
        original_name.rsplit(".", 1)[-1].lower()
        if "." in original_name
        else ("pdf" if file_type == "pdf" else "png")
    )

    doc_id = uuid.uuid4().hex[:10]
    s3_key = _doc_key(user_id, doc_id, ext)

    try:
        s3 = s3bucket()
        file_bytes = file.read()
        s3.upload_fileobj(
            io.BytesIO(file_bytes),
            S3_BUCKET,
            s3_key,
            ExtraArgs={"ContentType": mime},
        )
    except Exception as exc:
        logger.exception("S3 upload failed: %s", exc)
        return jsonify({"error": "Upload failed"}), 500

    try:
        tc = _get_trust_center(user_id)
        if not tc:
            tc_id = _upsert_trust_center(user_id, "")
        else:
            tc_id = tc["id"]

        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """INSERT INTO trust_center_documents (id, trust_center_id, label, s3_key, file_type)
                   VALUES (%s, %s, %s, %s, %s)""",
                (doc_id, tc_id, label, s3_key, file_type),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.exception("DB insert failed: %s", exc)
        delete_file_from_s3(s3_key)
        return jsonify({"error": "Failed to save document record"}), 500

    return jsonify(
        {
            "id": doc_id,
            "label": label,
            "file_type": file_type,
            "download_url": generate_presigned_url(s3_key),
            "view_url": attach_CLDFRNT_url(s3_key),
        }
    )


@trust_center_bp.route("/documents/<doc_id>", methods=["DELETE"])
@permission_required("trustcenter.document.delete")
def delete_document(doc_id: str):
    baseuser = _get_user_id()
    user_id, err = _check_trust_center_access(baseuser, "edit")
    if err:
        return err

    conn = connect_to_rds()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            """SELECT tcd.id, tcd.s3_key FROM trust_center_documents tcd
               JOIN trust_centers tc ON tc.id = tcd.trust_center_id
               WHERE tcd.id=%s AND tc.owner_user_id=%s""",
            (doc_id, user_id),
        )
        doc = cursor.fetchone()

    if not doc:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    try:
        delete_file_from_s3(doc["s3_key"])
    except Exception:
        pass

    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM trust_center_documents WHERE id=%s", (doc_id,))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@trust_center_bp.route("/documents/<doc_id>/download", methods=["GET"])
@permission_required("trustcenter.document.download")
def download_document(doc_id: str):
    baseuser = _get_user_id()
    if not baseuser:
        return jsonify({"error": "Unauthorized"}), 401

    conn = connect_to_rds()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            """SELECT tcd.s3_key, tc.owner_user_id
               FROM trust_center_documents tcd
               JOIN trust_centers tc ON tc.id = tcd.trust_center_id
               WHERE tcd.id=%s""",
            (doc_id,),
        )
        doc = cursor.fetchone()
    conn.close()

    if not doc:
        return jsonify({"error": "Not found"}), 404

    # Rewrite the baseuser to target the document's actual owner so the share
    # lookup runs against the right trust center (not whichever owner the
    # caller's composite happened to encode).
    logged_in_user_id, _ = parse_composite_user_id(baseuser)
    requester = logged_in_user_id or baseuser
    owner_id = doc["owner_user_id"]
    effective_baseuser = (
        requester
        if requester == owner_id
        else f"{requester}##SU##{owner_id}"
    )
    _, err = _check_trust_center_access(effective_baseuser, "view")
    if err:
        return err

    url = generate_presigned_url(doc["s3_key"])
    return jsonify({"url": url})


@trust_center_bp.route("/share", methods=["POST"])
@permission_required("trustcenter.share")
def share_trust_center():
    data = request.get_json(silent=True) or {}
    baseuser = _get_user_id()
    user_id, err = _check_trust_center_access(baseuser, "edit")
    if err:
        return err

    emails = data.get("emails", [])
    if not emails:
        return jsonify({"error": "emails required"}), 400

    tc = _get_trust_center(user_id)
    if not tc:
        tc_id = _upsert_trust_center(user_id, "")
    else:
        tc_id = tc["id"]

    granted = []
    email_errors = []

    conn = connect_to_rds()
    for email in emails:
        email = email.strip().lower()
        if not email:
            continue
        access_id = uuid.uuid4().hex[:12]
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT id FROM trust_center_access WHERE trust_center_id=%s AND granted_to_email=%s",
                    (tc_id, email),
                )
                existing = cursor.fetchone()
                if not existing:
                    cursor.execute(
                        """INSERT INTO trust_center_access (id, trust_center_id, granted_to_email)
                           VALUES (%s, %s, %s)""",
                        (access_id, tc_id, email),
                    )
            conn.commit()
            granted.append(email)
        except Exception as exc:
            logger.exception("Failed to grant access to %s: %s", email, exc)
            continue

    conn.close()

    # Send invitation emails
    frontend_base = os.getenv("FRONTEND_URL", "https://app.bytoid.ai")
    share_url = f"{frontend_base}/trust-center/shared/{user_id}"

    for email in granted:
        try:
            from services.gmail_service import GmailService

            gmail = GmailService(user_id)
            subject = "You've been invited to view a Trust Center"
            body = f"""<p>You have been granted access to a Trust &amp; Compliance Center.</p>
<p>Click the link below to view it. You will be asked to accept a Non-Disclosure Agreement before viewing.</p>
<p><a href="{share_url}">{share_url}</a></p>"""
            gmail.send_email(email, subject, body)
        except Exception as exc:
            logger.warning("Email send failed to %s: %s", email, exc)
            email_errors.append(email)

    status = 207 if email_errors else 200
    return jsonify({"granted": granted, "email_errors": email_errors}), status


@trust_center_bp.route("/shared/<owner_user_id>", methods=["GET"])
def get_shared_trust_center(owner_user_id: str):
    viewer_email = request.args.get("email", "").strip().lower()
    if not viewer_email:
        return jsonify({"access": False, "reason": "email required"}), 400
    logged_in_user_id, owner_user_id = parse_composite_user_id(owner_user_id)
    tc = _get_trust_center(owner_user_id)
    if not tc:
        return jsonify({"access": False, "reason": "not_found"}), 404

    conn = connect_to_rds()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            """SELECT * FROM trust_center_access
               WHERE trust_center_id=%s AND granted_to_email=%s""",
            (tc["id"], viewer_email),
        )
        access_row = cursor.fetchone()
    conn.close()

    if not access_row:
        return jsonify({"access": False})

    nda_content = tc.get("nda_content") or DEFAULT_NDA_TEMPLATE

    if not access_row.get("nda_accepted"):
        return jsonify(
            {"access": True, "nda_required": True, "nda_content": nda_content}
        )

    # NDA accepted — return full data
    try:
        wp_data = load_yaml_from_s3(tc["whitepaper_s3_key"]) or {}
    except Exception:
        wp_data = {}

    documents = _get_documents(tc["id"])
    doc_list = [
        {
            "id": d["id"],
            "label": d["label"],
            "file_type": d["file_type"],
            "uploaded_at": str(d["uploaded_at"]),
            "download_url": generate_presigned_url(d["s3_key"]),
            "view_url": attach_CLDFRNT_url(d["s3_key"]),
        }
        for d in documents
    ]

    return jsonify(
        {
            "access": True,
            "nda_required": False,
            "nda_accepted": True,
            "whitepaper": wp_data,
            "documents": doc_list,
        }
    )


@trust_center_bp.route("/shared/<owner_user_id>/accept-nda", methods=["POST"])
def accept_nda(owner_user_id: str):
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    logged_in_user_id, owner_user_id = parse_composite_user_id(owner_user_id)
    tc = _get_trust_center(owner_user_id)
    if not tc:
        return jsonify({"error": "Trust center not found"}), 404

    conn = connect_to_rds()
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            """UPDATE trust_center_access
               SET nda_accepted=1, nda_accepted_at=NOW()
               WHERE trust_center_id=%s AND granted_to_email=%s""",
            (tc["id"], email),
        )
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────
# Trust Center internal share (user/role with view or edit access)
#
# This is separate from the external email+NDA share above. The internal
# share grants organisation users (by id or role) view-only or edit access
# to the same trust center, identified by the owner's user_id.
# ─────────────────────────────────────────────────────────────


@trust_center_bp.route("/internal-share", methods=["POST"])
@permission_required("trustcenter.share")
def internal_share_trust_center():
    data = request.get_json(silent=True) or {}
    baseuser = _get_user_id()
    if not baseuser:
        return jsonify({"error": "Unauthorized"}), 401

    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400

    assignment_type = data.get("assignment_type")
    client_user_id = data.get("client_user_id")
    role_id = data.get("role_id")
    level = (data.get("level") or "").strip().lower()

    if level not in ("view", "edit"):
        return jsonify({"error": "level must be 'view' or 'edit'"}), 400
    if not assignment_type:
        return jsonify({"error": "assignment_type required"}), 400

    conn = None
    try:
        conn = connect_to_rds()
        required_permission = "trustcenter.view"

        if assignment_type == "manual":
            if not client_user_id:
                return jsonify({"error": "client_user_id required for manual"}), 400
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute("SELECT email FROM users WHERE user_id=%s", (client_user_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404
                user_email = row["email"]

        elif assignment_type == "role":
            if not role_id:
                return jsonify({"error": "role_id required for role"}), 400
            if not check_role_has_permission(conn, admin_id, role_id, required_permission):
                return (
                    jsonify({"error": "Role does not have trust center view permission"}),
                    403,
                )
            user_obj, error_msg = get_round_robin_user_for_resource(
                admin_id, role_id, "trust_center", conn, required_permission
            )
            if not user_obj:
                return jsonify({"error": error_msg or "No eligible users"}), 400
            client_user_id = user_obj["user_id"]
            user_email = user_obj["email"]
        else:
            return jsonify({"error": "assignment_type must be 'manual' or 'role'"}), 400

        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT email FROM users WHERE user_id=%s", (admin_id,))
            admin_row = cur.fetchone()
            if not admin_row:
                return jsonify({"error": "Admin not found"}), 404
            admin_email = admin_row["email"]

        sharing_access, error = core_assign_resource(
            "trust_center",
            admin_id,
            admin_email,
            client_user_id,
            user_email,
            admin_id,  # resource_id = owner_user_id (one TC per admin)
            "Trust Center",
            conn,
            level=level,
        )
        if error:
            return (
                jsonify({"error": error}),
                403 if "permission" in error.lower() else 400,
            )

        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRUST_CENTER_INTERNAL_SHARED,
            endpoint="/trust-center/internal-share",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "target_user_id": client_user_id,
                "assignment_type": assignment_type,
                "role_id": role_id,
                "level": level,
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "success": True,
                    "client_user_id": client_user_id,
                    "level": level,
                    "sharing_access": sharing_access,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error("internal_share_trust_center error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@trust_center_bp.route("/internal-revoke-share", methods=["POST"])
@permission_required("trustcenter.share")
def internal_revoke_trust_center_share():
    data = request.get_json(silent=True) or {}
    baseuser = _get_user_id()
    if not baseuser:
        return jsonify({"error": "Unauthorized"}), 401

    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400

    client_user_id = data.get("client_user_id")
    if not client_user_id:
        return jsonify({"error": "client_user_id required"}), 400

    try:
        sharing_access, error = core_revoke_resource(
            "trust_center", admin_id, client_user_id, admin_id
        )
        if error:
            return jsonify({"error": error}), 400

        (
            actor_user_id,
            actor_email,
            acting_on_behalf_of_user_id,
            acting_on_behalf_of_email,
        ) = build_audit_actor(baseuser)
        log_audit_event(
            action=TRUST_CENTER_INTERNAL_SHARE_REVOKED,
            endpoint="/trust-center/internal-revoke-share",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={"target_user_id": client_user_id},
        )
        g.audit_logged = True

        return jsonify({"success": True, "sharing_access": sharing_access}), 200
    except Exception as e:
        logger.error("internal_revoke_trust_center_share error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@trust_center_bp.route("/internal-sharing", methods=["GET"])
@permission_required("trustcenter.view")
def get_trust_center_internal_sharing():
    baseuser = _get_user_id()
    if not baseuser:
        return jsonify({"error": "Unauthorized"}), 401
    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400
    try:
        sharing_access, _ = core_list_resource_shares(
            "trust_center", admin_id, admin_id
        )
        return jsonify({"sharing_access": sharing_access}), 200
    except Exception as e:
        logger.error("get_trust_center_internal_sharing error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@trust_center_bp.route("/internal-shared", methods=["GET"])
@permission_required("trustcenter.view")
def list_trust_centers_shared_to_me():
    """List trust centers shared TO the requesting user (internal share)."""
    baseuser = _get_user_id()
    if not baseuser:
        return jsonify({"error": "Unauthorized"}), 401
    logged_in_user_id, target_user_id = parse_composite_user_id(baseuser)
    requester = logged_in_user_id or target_user_id
    try:
        shared = get_user_shared_resources(requester, "trust_center")
        return (
            jsonify(
                {
                    "user_id": requester,
                    "shared_trust_centers": list(shared.values()),
                }
            ),
            200,
        )
    except Exception as e:
        logger.error("list_trust_centers_shared_to_me error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500
