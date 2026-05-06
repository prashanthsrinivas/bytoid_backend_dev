import io
import os
import uuid
import yaml
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from credits_route.route import Credits
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response2
from utils.s3_utils import s3bucket, load_yaml_from_s3, delete_file_from_s3, list_all_files

S3_BUCKET = os.getenv("S3_BUCKET")
logger = get_logger(__name__)
policy_hub_bp = Blueprint("policy_hub", __name__, url_prefix="/policy-hub")


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


# ── 1. GENERATE ──────────────────────────────────────────────────────────────
@policy_hub_bp.route("/generate", methods=["POST"])
async def generate_policy():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    prompt = body.get("prompt")
    doc_type = body.get("type", "policy")
    frameworks = body.get("frameworks", [])

    if not user_id or not prompt or not doc_type:
        return jsonify({"error": "user_id, prompt, and type are required"}), 400

    fw_list = ", ".join(frameworks) if frameworks else "general compliance"
    system_prompt = (
        f"You are a compliance policy writer. Generate a detailed, professional {doc_type} "
        f"(Policy or Procedure) document that satisfies the following compliance frameworks: {fw_list}.\n\n"
        f"User instruction: {prompt}\n\n"
        "Output the document in plain text with clear section headings using markdown "
        "(e.g., ## Purpose, ## Scope, ## Policy Statement, ## Responsibilities, "
        "## Enforcement, ## References). Be thorough and production-ready.\n"
        "Do NOT include any preamble — output only the document content."
    )

    credits = Credits()
    content = await get_fireworks_response2(
        user_id=user_id,
        user_message=system_prompt,
        role="user",
        credits=credits,
        temp=0.1,
    )

    if content == "INSUFFICIENT":
        return jsonify({"error": "Insufficient credits"}), 402

    policy_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    title = _extract_title(content, fallback=f"{doc_type.title()} Document")
    key = _s3_key(user_id, policy_id)

    item = {
        "policy_id": policy_id,
        "title": title,
        "type": doc_type,
        "frameworks": frameworks,
        "content": content,
        "s3_key": key,
        "created_at": created_at,
    }

    try:
        _write_policy_to_s3(key, item)
    except Exception as e:
        logger.error("Failed to save policy to S3: %s", e)
        return jsonify({"error": "Failed to save policy"}), 500

    return jsonify({"item": item}), 200


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
