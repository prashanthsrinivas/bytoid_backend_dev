import asyncio
import io
import json
import os
import re
import uuid
import yaml
from datetime import datetime, timezone

from flask import Blueprint, Response, request, jsonify, stream_with_context

from credits_route.route import Credits
from utils.app_configs import ALLOWED_ORIGINS, IS_DEV
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response2
from utils.s3_utils import s3bucket, load_yaml_from_s3, delete_file_from_s3, list_all_files

S3_BUCKET = os.getenv("S3_BUCKET")
logger = get_logger(__name__)
policy_hub_bp = Blueprint("policy_hub", __name__, url_prefix="/policy-hub")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _s3_key(user_id: str, policy_id: str) -> str:
    return f"{user_id}/policies/{policy_id}.yaml"


def _write_policy_to_s3(key: str, data: dict):
    s3 = s3bucket()
    yaml_bytes = yaml.safe_dump(data, sort_keys=False).encode("utf-8")
    s3.upload_fileobj(io.BytesIO(yaml_bytes), S3_BUCKET, key)


def _extract_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _parse_docs_list(response: str) -> list:
    text = response.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text.strip())
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def _enumeration_prompt(prompt: str, fw_list: str, type_filter: str) -> str:
    return (
        f"You are a compliance expert. An organization needs to comply with: {fw_list}.\n"
        f"Organization context: {prompt}\n\n"
        "List ALL compliance documents (policies and procedures) that must be created for full compliance.\n"
        f"{type_filter}\n\n"
        "Return ONLY a valid JSON array — no other text — where each element has:\n"
        '  "title": document title (e.g., "Access Control Policy")\n'
        '  "type": "policy" or "procedure"\n'
        '  "description": one sentence on the document\'s purpose\n\n'
        "JSON array:"
    )


def _doc_generation_prompt(title: str, doc_type: str, description: str,
                            fw_list: str, user_context: str) -> str:
    stmt_heading = "Policy Statement" if doc_type == "policy" else "Procedure Steps"
    enforce_heading = "Enforcement" if doc_type == "policy" else "Compliance Monitoring"
    return (
        f"You are a senior compliance officer and policy writer. "
        f"Create a complete, production-ready {doc_type} document titled \"{title}\" "
        f"for an organization that must comply with: {fw_list}.\n\n"
        f"Document purpose: {description}\n"
        f"Organization context: {user_context}\n\n"
        "Requirements:\n"
        f"- First line must be exactly: # {title}\n"
        "- Include these sections in order:\n"
        "  ## Purpose\n"
        "  ## Scope\n"
        f"  ## {stmt_heading}\n"
        "  ## Roles and Responsibilities\n"
        f"  ## {enforce_heading}\n"
        "  ## References\n"
        "  ## Document Control\n"
        "- Write specific, actionable content — no generic filler\n"
        "- Reference relevant framework clauses where applicable "
        "(e.g., ISO 27001 Annex A.9, HIPAA §164.312, SOC 2 CC6)\n"
        "- Document Control must include: Version, Effective Date (placeholder), "
        "Review Cycle, Document Owner\n"
        "- Professional tone appropriate for a regulated organization\n\n"
        "Output ONLY the document content in markdown. No preamble."
    )


# ── 1. GENERATE (SSE streaming) ───────────────────────────────────────────────

@policy_hub_bp.route("/generate", methods=["POST"])
def generate_policy():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    prompt = body.get("prompt")
    doc_type = body.get("type")          # optional filter: "policy" | "procedure"
    frameworks = body.get("frameworks", [])

    if not user_id or not prompt:
        return jsonify({"error": "user_id and prompt are required"}), 400

    fw_list = ", ".join(frameworks) if frameworks else "general compliance"
    type_filter = (
        f"Only include documents of type '{doc_type}'." if doc_type
        else "Include both policies and procedures."
    )

    def event_stream():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            credits = Credits()

            # Phase 1: enumerate all required documents
            enum_resp = loop.run_until_complete(
                get_fireworks_response2(
                    user_id=user_id,
                    user_message=_enumeration_prompt(prompt, fw_list, type_filter),
                    role="user",
                    credits=credits,
                    temp=0.1,
                )
            )
            if enum_resp == "INSUFFICIENT":
                yield f"data: {json.dumps({'error': 'Insufficient credits'})}\n\n"
                return

            docs = _parse_docs_list(enum_resp)
            if not docs:
                yield f"data: {json.dumps({'error': 'Could not enumerate documents — try again'})}\n\n"
                return

            yield f"data: {json.dumps({'total': len(docs), 'documents': docs})}\n\n"

            # Phase 2: generate each document individually
            for i, doc in enumerate(docs):
                title = doc.get("title", "Compliance Document")
                d_type = doc.get("type", doc_type or "policy")
                description = doc.get("description", "")

                content = loop.run_until_complete(
                    get_fireworks_response2(
                        user_id=user_id,
                        user_message=_doc_generation_prompt(
                            title, d_type, description, fw_list, prompt
                        ),
                        role="user",
                        credits=credits,
                        temp=0.1,
                    )
                )
                if content == "INSUFFICIENT":
                    yield f"data: {json.dumps({'error': 'Insufficient credits', 'index': i})}\n\n"
                    return

                policy_id = str(uuid.uuid4())
                created_at = datetime.now(timezone.utc).isoformat()
                key = _s3_key(user_id, policy_id)
                item = {
                    "policy_id": policy_id,
                    "title": _extract_title(content, fallback=title),
                    "type": d_type,
                    "frameworks": frameworks,
                    "content": content,
                    "s3_key": key,
                    "created_at": created_at,
                }
                try:
                    _write_policy_to_s3(key, item)
                except Exception as e:
                    logger.error("S3 save failed for '%s': %s", title, e)
                    yield f"data: {json.dumps({'error': f'Failed to save {title}', 'index': i})}\n\n"
                    continue

                yield f"data: {json.dumps({'item': item, 'index': i, 'total': len(docs)})}\n\n"

            yield f"data: {json.dumps({'done': True, 'total': len(docs)})}\n\n"

        except Exception as e:
            logger.error("Policy generation stream error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            loop.close()

    origin = request.headers.get("Origin", "")
    resp_headers = {
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    if origin and (
        origin.rstrip("/") in ALLOWED_ORIGINS
        or (IS_DEV and origin.startswith("http://localhost:"))
    ):
        resp_headers["Access-Control-Allow-Origin"] = origin
        resp_headers["Access-Control-Allow-Credentials"] = "true"
        resp_headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers=resp_headers,
    )


# ── 2. LIST ───────────────────────────────────────────────────────────────────

@policy_hub_bp.route("/list", methods=["GET"])
def list_policies():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    prefix = f"{user_id}/policies/"
    s3_objects = list_all_files(folder=prefix)

    items = []
    for obj in s3_objects:
        key = obj.get("Key", "")
        if not key.endswith(".yaml"):
            continue
        data = load_yaml_from_s3(key)
        if data:
            items.append(data)

    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify({"items": items}), 200


# ── 3. UPDATE ─────────────────────────────────────────────────────────────────

@policy_hub_bp.route("/update", methods=["POST"])
def update_policy():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    policy_id = body.get("policy_id")

    if not user_id or not policy_id:
        return jsonify({"error": "user_id and policy_id are required"}), 400

    key = _s3_key(user_id, policy_id)
    existing = load_yaml_from_s3(key)
    if not existing:
        return jsonify({"error": "Policy not found"}), 404

    if "title" in body:
        existing["title"] = body["title"]
    if "content" in body:
        existing["content"] = body["content"]
    if "frameworks" in body:
        existing["frameworks"] = body["frameworks"]
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        _write_policy_to_s3(key, existing)
    except Exception as e:
        logger.error("Failed to update policy in S3: %s", e)
        return jsonify({"error": "Failed to update policy"}), 500

    return jsonify({"status": "ok"}), 200


# ── 4. DELETE ─────────────────────────────────────────────────────────────────

@policy_hub_bp.route("/delete", methods=["DELETE"])
def delete_policy():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    policy_id = body.get("policy_id")

    if not user_id or not policy_id:
        return jsonify({"error": "user_id and policy_id are required"}), 400

    key = _s3_key(user_id, policy_id)
    ok = delete_file_from_s3(key)
    if not ok:
        return jsonify({"error": "Delete failed or file not found"}), 500

    return jsonify({"status": "ok"}), 200
