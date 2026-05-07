import asyncio
import io
import json
import os
import re
import threading
import uuid
import yaml
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from credits_route.route import Credits
from utils.app_configs import ALLOWED_ORIGINS, IS_DEV
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response2
from utils.s3_utils import s3bucket, load_yaml_from_s3, read_json_from_s3, delete_file_from_s3, list_all_files

S3_BUCKET = os.getenv("S3_BUCKET")
logger = get_logger(__name__)
policy_hub_bp = Blueprint("policy_hub", __name__, url_prefix="/policy-hub")

_jobs_lock = threading.Lock()


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3_key(user_id: str, policy_id: str) -> str:
    return f"{user_id}/policies/{policy_id}.yaml"


def _job_s3_key(job_id: str) -> str:
    return f"policy_hub_jobs/{job_id}.json"


def _write_yaml_to_s3(key: str, data: dict):
    s3 = s3bucket()
    yaml_bytes = yaml.safe_dump(data, sort_keys=False).encode("utf-8")
    s3.upload_fileobj(io.BytesIO(yaml_bytes), S3_BUCKET, key)


def _write_json_to_s3(key: str, data: dict):
    s3 = s3bucket()
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    s3.upload_fileobj(io.BytesIO(body), S3_BUCKET, key)


def _read_job(job_id: str) -> dict | None:
    return read_json_from_s3(_job_s3_key(job_id))


def _save_job(job_id: str, state: dict):
    with _jobs_lock:
        _write_json_to_s3(_job_s3_key(job_id), state)


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _extract_title(content: str, fallback: str) -> str:
    # Try HTML <h1> first
    m = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    # Fallback: markdown # heading
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


def _extract_tag(text: str, tag: str) -> str:
    """Extract content between [TAG]...[/TAG] delimiters, stripping whitespace."""
    m = re.search(rf"\[{tag}\](.*?)\[/{tag}\]", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


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
        f"You are a world-class compliance officer, legal counsel, and technical writer with 20+ years of "
        f"experience authoring enterprise-grade {doc_type} documents for Fortune 500 companies and regulated "
        f"startups. Your output must score 99/100 on a professional compliance audit — meaning it is "
        f"indistinguishable from a document produced by a Big 4 consulting firm.\n\n"
        f"Create a complete, audit-ready {doc_type} document titled \"{title}\" "
        f"for an organization that must comply with: {fw_list}.\n\n"
        f"Document purpose: {description}\n"
        f"Organization context: {user_context}\n\n"
        "QUALITY STANDARDS (every standard must be met — failure on any = unacceptable quality):\n"
        "1. Every section contains substantive, specific content — zero generic filler or placeholder text\n"
        "2. Every control or requirement is tied to a named framework clause "
        "(e.g., ISO 27001:2022 Annex A.8.3, HIPAA §164.312(a)(1), SOC 2 CC6.1, NIST SP 800-53 AC-2)\n"
        "3. Policy/procedure statements are written in clear imperative language "
        "(\"The organization SHALL...\", \"All employees MUST...\")\n"
        "4. Roles are named precisely (e.g., CISO, IT Security Team, System Owners, Data Custodians) "
        "with distinct, non-overlapping responsibilities\n"
        "5. The enforcement/compliance section specifies concrete consequences and audit mechanisms\n"
        "6. The document reads as if it has already passed an external compliance audit\n"
        "7. Minimum depth: each major section must contain at least 3–5 specific, actionable sub-points\n\n"
        "Output the document as a self-contained HTML fragment (no <html>, <head>, or <body> tags). "
        "Use only inline CSS styles. Follow this exact structure and styling:\n\n"
        "<div style=\"font-family: 'Segoe UI', Arial, sans-serif; max-width: 860px; "
        "margin: 0 auto; color: #1a202c; line-height: 1.7; padding: 32px;\">\n\n"
        f"  <h1 style=\"font-size: 26px; font-weight: 700; color: #1a365d; "
        "border-bottom: 3px solid #2b6cb0; padding-bottom: 12px; margin-bottom: 8px;\">"
        f"{title}</h1>\n\n"
        "  <p style=\"font-size: 13px; color: #718096; margin-bottom: 32px;\">"
        f"Type: {doc_type.title()} &nbsp;|&nbsp; Frameworks: {fw_list}</p>\n\n"
        "  <!-- Section heading -->\n"
        "  <h2 style=\"font-size: 18px; font-weight: 600; color: #2c5282; "
        "margin-top: 32px; margin-bottom: 10px; border-left: 4px solid #2b6cb0; "
        "padding-left: 12px;\">Section Title</h2>\n"
        "  <p style=\"margin: 0 0 16px 0;\">Section content...</p>\n\n"
        "  <!-- For lists use: -->\n"
        "  <ul style=\"margin: 0 0 16px 20px; padding: 0;\">\n"
        "    <li style=\"margin-bottom: 6px;\">Item</li>\n"
        "  </ul>\n\n"
        "  <!-- For sub-sections within a section use h3: -->\n"
        "  <h3 style=\"font-size: 15px; font-weight: 600; color: #2d3748; margin-top: 20px; "
        "margin-bottom: 8px;\">Sub-section</h3>\n\n"
        "  <!-- Document Control table -->\n"
        "  <table style=\"border-collapse: collapse; width: 100%; margin-top: 8px;\">\n"
        "    <thead>\n"
        "      <tr>\n"
        "        <th style=\"background: #2b6cb0; color: #fff; text-align: left; "
        "padding: 10px 14px; font-size: 13px;\">Field</th>\n"
        "        <th style=\"background: #2b6cb0; color: #fff; text-align: left; "
        "padding: 10px 14px; font-size: 13px;\">Information</th>\n"
        "      </tr>\n"
        "    </thead>\n"
        "    <tbody>\n"
        "      <tr style=\"background: #f7fafc;\">\n"
        "        <td style=\"padding: 9px 14px; border-bottom: 1px solid #e2e8f0; "
        "font-weight: 600; font-size: 13px;\">Version</td>\n"
        "        <td style=\"padding: 9px 14px; border-bottom: 1px solid #e2e8f0; "
        "font-size: 13px;\">1.0</td>\n"
        "      </tr>\n"
        "    </tbody>\n"
        "  </table>\n\n"
        "</div>\n\n"
        f"Include these sections in order: Purpose, Scope, {stmt_heading}, "
        f"Roles and Responsibilities, {enforce_heading}, References, Document Control.\n"
        "Document Control table rows: Version (1.0), Effective Date ([Insert Date]), "
        "Review Cycle, Document Owner, Classification (e.g., Internal / Confidential).\n\n"
        "Output ONLY the HTML fragment. No markdown. No preamble. No code fences. "
        "Do not truncate or summarize — write every section in full."
    )


# ── Background generation worker ──────────────────────────────────────────────

def _generation_worker(user_id: str, job_id: str, docs: list,
                       frameworks: list, prompt: str, fw_list: str, doc_type_filter):
    """
    Runs in a background thread. Generates every document in `docs`,
    saves each as a separate YAML file, and updates the job state in S3
    after each one so the frontend can poll for progress.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        credits = Credits()
        total = len(docs)

        for i, doc in enumerate(docs):
            title = doc.get("title", "Compliance Document")
            d_type = doc.get("type", doc_type_filter or "policy")
            description = doc.get("description", "")

            try:
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
                    logger.warning("Insufficient credits — stopping generation at index %d", i)
                    job = _read_job(job_id) or {}
                    job["status"] = "error"
                    job["error"] = "Insufficient credits"
                    _save_job(job_id, job)
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
                _write_yaml_to_s3(key, item)

            except Exception as e:
                logger.error("Failed to generate '%s': %s", title, e)
                item = None

            # Update job state with completed item
            job = _read_job(job_id) or {"items": [], "completed": 0}
            job["completed"] = i + 1
            if item:
                job["items"].append(item)
            if i + 1 >= total:
                job["status"] = "done"
            _save_job(job_id, job)

        logger.info("Policy generation complete for user %s: %d documents", user_id, total)

    except Exception as e:
        logger.error("Generation worker crashed for job %s: %s", job_id, e)
        try:
            job = _read_job(job_id) or {}
            job["status"] = "error"
            job["error"] = str(e)
            _save_job(job_id, job)
        except Exception:
            pass
    finally:
        loop.close()


# ── 1. GENERATE ───────────────────────────────────────────────────────────────

@policy_hub_bp.route("/generate", methods=["POST"])
async def generate_policy():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    prompt = body.get("prompt")
    doc_type = body.get("type")          # kept for metadata only — generation always covers both types
    frameworks = body.get("frameworks", [])

    if not user_id or not prompt:
        return jsonify({"error": "user_id and prompt are required"}), 400

    fw_list = ", ".join(frameworks) if frameworks else "general compliance"
    # Always enumerate both policies AND procedures regardless of the tab the frontend is on
    type_filter = "Include both policies and procedures."

    # Phase 1: enumerate all required documents (fast, completes well within timeout)
    credits = Credits()
    enum_resp = await get_fireworks_response2(
        user_id=user_id,
        user_message=_enumeration_prompt(prompt, fw_list, type_filter),
        role="user",
        credits=credits,
        temp=0.1,
    )

    if enum_resp == "INSUFFICIENT":
        return jsonify({"error": "Insufficient credits"}), 402

    docs = _parse_docs_list(enum_resp)
    if not docs:
        return jsonify({"error": "Could not enumerate documents — try again"}), 500

    # Create job and save initial state to S3
    job_id = str(uuid.uuid4())
    job_state = {
        "job_id": job_id,
        "user_id": user_id,
        "status": "processing",
        "total": len(docs),
        "completed": 0,
        "items": [],
        "documents": docs,
        "frameworks": frameworks,
        "error": None,
    }
    _save_job(job_id, job_state)

    # Phase 2: generate all documents in background — runs to completion regardless of client
    thread = threading.Thread(
        target=_generation_worker,
        args=(user_id, job_id, docs, frameworks, prompt, fw_list, doc_type),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "PROCESSING",
        "total": len(docs),
        "documents": docs,
    }), 202


# ── 1b. GENERATE STATUS (polling) ─────────────────────────────────────────────

@policy_hub_bp.route("/status", methods=["GET"])
def generate_status():
    job_id = request.args.get("job_id")

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    job = _read_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "job_id": job_id,
        "status": job.get("status", "processing").upper(),
        "total": job.get("total", 0),
        "completed": job.get("completed", 0),
        "items": job.get("items", []),
        "documents": job.get("documents", []),
        "error": job.get("error"),
    }), 200


# ── Section helpers ───────────────────────────────────────────────────────────

def _split_sections(html: str) -> list[tuple[str, int, int]]:
    """Split document into (section_html, start, end) chunks at <h2> boundaries."""
    h2_iter = list(re.finditer(r'<h2[\s>]', html, re.IGNORECASE))
    if not h2_iter:
        return [(html, 0, len(html))]
    sections = []
    if h2_iter[0].start() > 0:
        sections.append((html[:h2_iter[0].start()], 0, h2_iter[0].start()))
    for i, m in enumerate(h2_iter):
        start = m.start()
        end = h2_iter[i + 1].start() if i + 1 < len(h2_iter) else len(html)
        sections.append((html[start:end], start, end))
    return sections


def _find_section(html: str, needle: str) -> tuple[str, int, int] | None:
    """Return (section_html, start, end) for the <h2> section containing needle."""
    if not needle:
        return None
    needle_plain = re.sub(r'<[^>]+>', '', needle).strip()[:80]
    for section_html, start, end in _split_sections(html):
        if needle_plain and needle_plain in re.sub(r'<[^>]+>', '', section_html):
            return section_html, start, end
    return None


# ── 1c. EDIT ──────────────────────────────────────────────────────────────────

@policy_hub_bp.route("/edit", methods=["POST"])
async def edit_policy():
    body = request.get_json(silent=True) or {}
    user_id          = body.get("user_id")
    policy_id        = body.get("policy_id")
    document_title   = body.get("document_title", "")
    document_content = body.get("document_content", "")
    instruction      = body.get("instruction", "")
    selected_text    = body.get("selected_text", "").strip()
    section_title    = body.get("section_title", "").strip()

    if not user_id or not policy_id or not document_content or not instruction:
        return jsonify({"error": "user_id, policy_id, document_content, and instruction are required"}), 400

    credits = Credits()

    # Isolate the relevant <h2> section so AI only reads/writes ~500-2000 tokens
    needle = selected_text or section_title
    section_result = _find_section(document_content, needle) if needle else None

    if section_result:
        section_html, sec_start, sec_end = section_result
        before = document_content[:sec_start]
        after  = document_content[sec_end:]

        focus = (
            f"The user selected: \"{selected_text[:120]}\"\n"
            if selected_text else
            f"Target section: \"{section_title}\"\n"
        )
        ai_prompt = (
            "You are an expert GRC policy writer editing a compliance document section.\n\n"
            f"Document title: {document_title}\n"
            f"Instruction: {instruction}\n"
            + focus +
            "\nRewrite this section per the instruction:\n\n"
            + section_html +
            "\n\nReturn EXACTLY:\n"
            "[EXPLANATION]\n1–2 sentence summary.\n[/EXPLANATION]\n"
            "[SECTION]\nRewritten section HTML only — no surrounding document, no code fences.\n[/SECTION]\n\n"
            "Rules:\n"
            "- Preserve all inline styles and heading tags\n"
            "- Keep framework citations intact unless instructed to change\n"
            "- Do not truncate"
        )
        response = await get_fireworks_response2(
            user_id=user_id, user_message=ai_prompt,
            role="user", credits=credits, temp=0.1,
        )
        if response == "INSUFFICIENT":
            return jsonify({"error": "Insufficient credits"}), 402

        explanation     = _extract_tag(response, "EXPLANATION")
        new_section     = _extract_tag(response, "SECTION") or response.strip()
        updated_content = before + new_section + after

    else:
        # Full-document edit (no selection, no section title)
        ai_prompt = (
            "You are an expert GRC policy writer editing a compliance document.\n\n"
            f"Document title: {document_title}\n"
            f"Instruction: {instruction}\n\n"
            "Return EXACTLY:\n"
            "[EXPLANATION]\n1–2 sentence summary.\n[/EXPLANATION]\n"
            "[HTML]\nFull updated document HTML.\n[/HTML]\n\n"
            "Rules:\n"
            "- Return ONLY valid HTML — no markdown, no code fences\n"
            "- Preserve all inline styles and structure\n"
            "- Keep framework citations intact unless instructed\n"
            "- Do not truncate\n\n"
            f"Current document HTML:\n{document_content}"
        )
        response = await get_fireworks_response2(
            user_id=user_id, user_message=ai_prompt,
            role="user", credits=credits, temp=0.1,
        )
        if response == "INSUFFICIENT":
            return jsonify({"error": "Insufficient credits"}), 402

        explanation     = _extract_tag(response, "EXPLANATION")
        updated_content = _extract_tag(response, "HTML")
        if not updated_content:
            return jsonify({"error": "AI did not return valid HTML"}), 500

    # Persist to S3
    key = _s3_key(user_id, policy_id)
    existing = load_yaml_from_s3(key)
    if existing:
        existing["content"]    = updated_content
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _write_yaml_to_s3(key, existing)
        except Exception as e:
            logger.error("Failed to persist edit for policy %s: %s", policy_id, e)

    return jsonify({
        "updated_content": updated_content,
        "explanation": explanation or "The document has been updated per your instruction.",
    }), 200


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
        # skip job state files
        if not key.endswith(".yaml") or "/jobs/" in key:
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
        _write_yaml_to_s3(key, existing)
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
