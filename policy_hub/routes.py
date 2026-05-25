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

import pandas as pd
import pymysql

from flask import Blueprint, request, jsonify, g

from credits_route.route import Credits
from utils.normal import parse_composite_user_id
from utils.permission_required import permission_required_body
from db.db_checkers import get_email_by_id
from db.rds_db import connect_to_rds
from db.lance_db_service import LanceDBServer, VectorData, QueryData
from utils.app_configs import FRAMEWORK_OWNER, policy_hub_v2_enabled, statement_reid_threshold
from utils.base_logger import get_logger
from policy_hub.templates import get_template, validate as validate_template
from policy_hub.structured import (
    parse_document_html,
    reconcile_statement_ids,
    sync_statements_to_lance,
)
from policy_hub.extract import extract_any
from utils.fireworkzz import get_fireworks_response2, get_firework_embedding
from utils.s3_utils import (
    s3bucket,
    load_yaml_from_s3,
    read_json_from_s3,
    delete_file_from_s3,
    list_all_files,
)
from services.audit_log_service import (
    log_audit_event,
    build_audit_actor,
    POLICY_SHARED,
    POLICY_SHARE_REVOKED,
    POLICY_UPLOADED,
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
policy_hub_bp = Blueprint("policy_hub", __name__, url_prefix="/policy-hub")

_jobs_lock = threading.Lock()


# ── Share access helper ──────────────────────────────────────────────────────


def _check_policy_share_access(baseuser, policy_id):
    """Resolve owner and ensure the requester has access. Returns (owner_id, err_tuple)."""
    logged_in_user_id, owner_id = parse_composite_user_id(baseuser)
    if not owner_id:
        return None, (jsonify({"error": "Invalid user_id"}), 400)
    if not logged_in_user_id or logged_in_user_id == owner_id:
        return owner_id, None
    access = get_user_resource_access("policy", owner_id, policy_id, logged_in_user_id)
    if not access.get("granted"):
        return None, (
            jsonify({"error": "Access to this policy has not been granted"}),
            403,
        )
    return owner_id, None


# ── S3 helpers ────────────────────────────────────────────────────────────────


def _s3_key(user_id: str, policy_id: str) -> str:
    return f"{user_id}/policies/{policy_id}.yaml"


def _raw_file_key(user_id: str, policy_id: str, ext: str) -> str:
    """S3 key for the original uploaded file archived alongside the YAML."""
    if ext and not ext.startswith("."):
        ext = "." + ext
    return f"{user_id}/policies/raw/{policy_id}{ext}"


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


def _v2_section_requirements(doc_type: str) -> str:
    """Build the ordered section list injected into the V2 generation prompt."""
    try:
        template = get_template(doc_type)
    except KeyError:
        return ""
    lines = []
    for sec in template:
        required_tag = "" if sec.required else " (optional)"
        lines.append(
            f'  <div data-section-id="{sec.id}">\n'
            f'    <h2 data-section-id="{sec.id}">{sec.title}{required_tag}</h2>\n'
            f"    <!-- {sec.prompt_help} -->"
        )
        if sec.kind in ("statements", "steps"):
            tag = "ol" if sec.kind == "steps" else "ul"
            lines.append(
                f"    <!-- Each item MUST use: <li data-statement-id=\"{{NEW_UUID}}\">…</li> -->\n"
                f"    <{tag}>\n"
                f'      <li data-statement-id="{{NEW_UUID}}">first item</li>\n'
                f"    </{tag}>"
            )
        lines.append("  </div>")
    return "\n".join(lines)


def _doc_generation_prompt(
    title: str,
    doc_type: str,
    description: str,
    fw_list: str,
    user_context: str,
    controls: str = "",
    v2: bool = False,
) -> str:
    stmt_heading = "Policy Statement" if doc_type == "policy" else "Procedure Steps"
    enforce_heading = "Enforcement" if doc_type == "policy" else "Compliance Monitoring"

    if controls:
        controls_block = (
            "━━━ AUTHORIZED CONTROLS — SOLE PERMITTED SOURCE FOR ALL CITATIONS ━━━\n"
            "The rows below were retrieved verbatim from the uploaded framework files.\n"
            "They are the ONLY control references you are allowed to use anywhere in this document.\n\n"
            + controls
            + "\n\n"
            "━━━ END OF AUTHORIZED CONTROLS ━━━\n\n"
        )
        control_quality_rule = (
            "2. [NON-NEGOTIABLE — GROUNDING] Every control ID, clause number, requirement "
            "reference, or framework citation in this document MUST appear verbatim in the "
            "AUTHORIZED CONTROLS list above. You MUST NOT invent, infer, paraphrase, or "
            "supplement with any control from your training data — this includes widely known "
            "identifiers such as 'ISO 27001 A.8.3', 'HIPAA §164.312', 'SOC 2 CC6.1', or "
            "'NIST AC-2' unless they are explicitly present in the list above. "
            "If a section cannot be supported by the listed controls, write only what the "
            "controls directly state — never fabricate coverage.\n"
        )
        output_gate = (
            "MANDATORY PRE-OUTPUT CHECK: Before writing the HTML, mentally scan every sentence "
            "that contains a control ID, clause number, or requirement identifier. "
            "Verify each one exists in the AUTHORIZED CONTROLS list. "
            "Remove any that do not. There are no exceptions.\n\n"
        )
    else:
        controls_block = ""
        control_quality_rule = (
            "2. Every control or requirement is tied to a named framework clause "
            "(e.g., ISO 27001:2022 Annex A.8.3, HIPAA §164.312(a)(1), SOC 2 CC6.1, NIST SP 800-53 AC-2)\n"
        )
        output_gate = ""

    return (
        f"You are a world-class compliance officer, legal counsel, and technical writer with 20+ years of "
        f"experience authoring enterprise-grade {doc_type} documents for Fortune 500 companies and regulated "
        f"startups. Your output must score 99/100 on a professional compliance audit — meaning it is "
        f"indistinguishable from a document produced by a Big 4 consulting firm.\n\n"
        f'Create a complete, audit-ready {doc_type} document titled "{title}" '
        f"for an organization that must comply with: {fw_list}.\n\n"
        f"Document purpose: {description}\n"
        f"Organization context: {user_context}\n\n"
        + controls_block
        + "QUALITY STANDARDS (every standard must be met — failure on any = unacceptable quality):\n"
        "1. Every section contains substantive, specific content — zero generic filler or placeholder text\n"
        + control_quality_rule
        + "3. Policy/procedure statements are written in clear imperative language "
        '("The organization SHALL...", "All employees MUST...")\n'
        "4. Roles are named precisely (e.g., CISO, IT Security Team, System Owners, Data Custodians) "
        "with distinct, non-overlapping responsibilities\n"
        "5. The enforcement/compliance section specifies concrete consequences and audit mechanisms\n"
        "6. The document reads as if it has already passed an external compliance audit\n"
        "7. Minimum depth: each major section must contain at least 3–5 specific, actionable sub-points\n\n"
        + output_gate
        + "Output the document as a self-contained HTML fragment (no <html>, <head>, or <body> tags). "
        "Use only inline CSS styles. Follow this exact structure and styling:\n\n"
        "<div style=\"font-family: 'Segoe UI', Arial, sans-serif; max-width: 860px; "
        'margin: 0 auto; color: #1a202c; line-height: 1.7; padding: 32px;">\n\n'
        f'  <h1 style="font-size: 26px; font-weight: 700; color: #1a365d; '
        'border-bottom: 3px solid #2b6cb0; padding-bottom: 12px; margin-bottom: 8px;">'
        f"{title}</h1>\n\n"
        '  <p style="font-size: 13px; color: #718096; margin-bottom: 32px;">'
        f"Type: {doc_type.title()} &nbsp;|&nbsp; Frameworks: {fw_list}</p>\n\n"
        "  <!-- Section heading -->\n"
        '  <h2 style="font-size: 18px; font-weight: 600; color: #2c5282; '
        "margin-top: 32px; margin-bottom: 10px; border-left: 4px solid #2b6cb0; "
        'padding-left: 12px;">Section Title</h2>\n'
        '  <p style="margin: 0 0 16px 0;">Section content...</p>\n\n'
        "  <!-- For lists use: -->\n"
        '  <ul style="margin: 0 0 16px 20px; padding: 0;">\n'
        '    <li style="margin-bottom: 6px;">Item</li>\n'
        "  </ul>\n\n"
        "  <!-- For sub-sections within a section use h3: -->\n"
        '  <h3 style="font-size: 15px; font-weight: 600; color: #2d3748; margin-top: 20px; '
        'margin-bottom: 8px;">Sub-section</h3>\n\n'
        "  <!-- Document Control table -->\n"
        '  <table style="border-collapse: collapse; width: 100%; margin-top: 8px;">\n'
        "    <thead>\n"
        "      <tr>\n"
        '        <th style="background: #2b6cb0; color: #fff; text-align: left; '
        'padding: 10px 14px; font-size: 13px;">Field</th>\n'
        '        <th style="background: #2b6cb0; color: #fff; text-align: left; '
        'padding: 10px 14px; font-size: 13px;">Information</th>\n'
        "      </tr>\n"
        "    </thead>\n"
        "    <tbody>\n"
        '      <tr style="background: #f7fafc;">\n'
        '        <td style="padding: 9px 14px; border-bottom: 1px solid #e2e8f0; '
        'font-weight: 600; font-size: 13px;">Version</td>\n'
        '        <td style="padding: 9px 14px; border-bottom: 1px solid #e2e8f0; '
        'font-size: 13px;">1.0</td>\n'
        "      </tr>\n"
        "    </tbody>\n"
        "  </table>\n\n"
        "</div>\n\n"
        + (
            # V2: inject the ordered section list with data-section-id / data-statement-id requirements
            (
                "REQUIRED DOCUMENT STRUCTURE — follow exactly:\n"
                "Wrap each section in <div data-section-id=\"{section_id}\"> and use an <h2> for the heading.\n"
                "For Policy Statements and Procedure Steps sections, each item MUST be:\n"
                '  <li data-statement-id="{NEW_UUID}">…</li>\n'
                "where {NEW_UUID} is a freshly generated UUID (e.g., 3f2a1b4c-...). "
                "Never reuse UUIDs across items.\n\n"
                "Section order and IDs:\n"
                + _v2_section_requirements(doc_type)
                + "\n\n"
            )
            if v2
            else (
                f"Include these sections in order: Purpose, Scope, {stmt_heading}, "
                f"Roles and Responsibilities, {enforce_heading}, References, Document Control.\n"
                "Document Control table rows: Version (1.0), Effective Date ([Insert Date]), "
                "Review Cycle, Document Owner, Classification (e.g., Internal / Confidential).\n"
            )
        )
        + "Output ONLY the HTML fragment. No markdown. No preamble. No code fences. "
        "Do not truncate or summarize — write every section in full."
    )


# ── V2 helpers (no-op when flag is off) ──────────────────────────────────────


def _enrich_v2(item: dict, content: str, doc_type: str, loop: asyncio.AbstractEventLoop) -> dict:
    """Parse HTML into structured sections, validate, and add V2 fields to *item*.

    Mutates and returns *item*. On any failure, leaves V2 fields absent so the
    document degrades gracefully to legacy mode.
    """
    try:
        parsed = parse_document_html(content, doc_type)
        validation = validate_template(content, doc_type)

        # If template validation fails, retry once with a corrective nudge by
        # re-prompting is out of scope here — just mark needs_review and carry on.
        item["template_version"] = 1
        item["validation_status"] = "ok" if validation.ok else "needs_review"
        item["migration_status"] = "ok"

        # Build the sections list for storage alongside the legacy content blob.
        # Sections are stored as plain dicts so PyYAML can serialise them.
        sections_data = []
        for sec in parsed.sections:
            sec_dict: dict = {
                "id": sec.id,
                "title": sec.title,
                "kind": sec.kind,
                "body_html": sec.body_html,
            }
            if sec.statements:
                sec_dict["statements"] = [
                    {
                        "id": s.id,
                        "text": s.text,
                        "seq": s.seq,
                        "section_id": s.section_id,
                        "status": s.status,
                    }
                    for s in sec.statements
                ]
            sections_data.append(sec_dict)

        item["sections"] = sections_data
        item["metadata"] = parsed.metadata

        if not validation.ok:
            logger.warning(
                "Template validation needs_review for policy=%s: missing=%s",
                item.get("policy_id"),
                validation.missing_sections,
            )
    except Exception as exc:
        logger.error(
            "_enrich_v2 failed for policy=%s: %s", item.get("policy_id"), exc
        )
    return item


def _sync_statements(item: dict, user_id: str, doc_type: str, loop: asyncio.AbstractEventLoop) -> None:
    """Sync policy statements to LanceDB in the background thread's event loop."""
    from policy_hub.structured import Statement

    policy_id = item.get("policy_id", "")
    version = item.get("metadata", {}).get("version", "1.0")
    statements: list[Statement] = []

    for sec in item.get("sections", []):
        for s in sec.get("statements", []):
            statements.append(
                Statement(
                    id=s["id"],
                    text=s["text"],
                    seq=s["seq"],
                    section_id=s.get("section_id", sec["id"]),
                )
            )

    if not statements:
        return

    try:
        loop.run_until_complete(
            sync_statements_to_lance(
                policy_id=policy_id,
                doc_type=doc_type,
                version=version,
                statements=statements,
            )
        )
    except Exception as exc:
        logger.error("_sync_statements failed for policy=%s: %s", policy_id, exc)


# ── Background generation worker ──────────────────────────────────────────────


async def _fetch_framework_controls(
    framework_ids: list, title: str, description: str
) -> str:
    """Query LanceDB for controls relevant to this document from each selected framework."""
    if not framework_ids:
        return ""

    query_text = f"{title} — {description}"
    embeddings = await get_firework_embedding()
    vec = await asyncio.to_thread(embeddings.embed_query, query_text)

    lance = LanceDBServer()
    query = QueryData(user_id=FRAMEWORK_LANCE_USER, embedding=vec, top_k=10)

    seen: set = set()
    snippets: list = []
    for fw_id in framework_ids:
        try:
            results = await lance.query_vector_filename(query, fw_id)
            for r in results:
                t = r.get("text", "")
                if t and t not in seen:
                    seen.add(t)
                    snippets.append(t)
        except Exception as e:
            logger.warning("LanceDB query failed for framework %s: %s", fw_id, e)

    return "\n".join(f"- {s}" for s in snippets[:30])


def _generation_worker(
    user_id: str,
    job_id: str,
    docs: list,
    frameworks: list,
    framework_ids: list,
    prompt: str,
    fw_list: str,
    doc_type_filter,
):
    """
    Runs in a background thread. Generates every document in `docs`,
    saves each as a separate YAML file, and updates the job state in S3
    after each one so the frontend can poll for progress.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    v2 = policy_hub_v2_enabled(user_id)
    try:
        credits = Credits()
        total = len(docs)

        for i, doc in enumerate(docs):
            title = doc.get("title", "Compliance Document")
            d_type = doc.get("type", doc_type_filter or "policy")
            description = doc.get("description", "")

            controls = ""
            if framework_ids:
                try:
                    controls = loop.run_until_complete(
                        _fetch_framework_controls(framework_ids, title, description)
                    )
                except Exception as e:
                    logger.warning(
                        "Framework controls fetch failed for '%s': %s", title, e
                    )

            try:
                content = loop.run_until_complete(
                    get_fireworks_response2(
                        user_id=user_id,
                        user_message=_doc_generation_prompt(
                            title, d_type, description, fw_list, prompt, controls,
                            v2=v2,
                        ),
                        role="user",
                        credits=credits,
                        temp=0.1,
                    )
                )

                if content == "INSUFFICIENT":
                    logger.warning(
                        "Insufficient credits — stopping generation at index %d", i
                    )
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
                    "etag": str(uuid.uuid4()),
                }

                if v2:
                    item = _enrich_v2(item, content, d_type, loop)

                _write_yaml_to_s3(key, item)

                if v2:
                    _sync_statements(item, user_id, d_type, loop)

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

        logger.info(
            "Policy generation complete for user %s: %d documents", user_id, total
        )

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


# ── Upload helpers ───────────────────────────────────────────────────────────

UPLOAD_ALLOWED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm"}
UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB per file
UPLOAD_MAX_FILES_PER_REQUEST = 20
UPLOAD_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".htm": "text/html",
}
VALID_DOC_TYPES = {"policy", "procedure", "standard"}


def _resolve_user_id_multi_source():
    """Resolve the target user_id from g, form, args, or JSON body.

    Returns (logged_in_user_id, user_id) or (None, None) when no source has it.
    Mirrors the chain used by `_require_framework_owner` but without the email
    gate so non-admin users can upload too.
    """
    raw = (
        getattr(g, "user_id", None)
        or getattr(g, "session_user_id", None)
        or request.form.get("user_id")
        or request.args.get("user_id")
        or (request.get_json(silent=True) or {}).get("user_id")
    )
    if not raw:
        return None, None
    return parse_composite_user_id(raw)


def _strip_html_to_text(html: str, max_chars: int = 2000) -> str:
    """Strip tags to plain text for the cheap classification call."""
    from bs4 import BeautifulSoup

    try:
        text = BeautifulSoup(html or "", "lxml").get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _classification_prompt(text_sample: str) -> str:
    return (
        "Classify the following compliance document as exactly ONE of: policy, procedure, standard.\n"
        "- 'policy' states organizational rules and principles (uses 'shall', 'must').\n"
        "- 'procedure' lists ordered operational steps to accomplish a task.\n"
        "- 'standard' specifies technical or measurable requirements (e.g., minimum key length, password complexity).\n\n"
        "Return ONLY one word — policy, procedure, or standard — with no punctuation or explanation.\n\n"
        f"DOCUMENT CONTENT:\n{text_sample}\n\nAnswer:"
    )


async def _classify_doc_type_via_llm(html: str) -> str:
    """Ask Fireworks to classify the document. Falls back to 'policy' on any failure."""
    sample = _strip_html_to_text(html, max_chars=2000)
    if not sample:
        return "policy"
    try:
        resp = await get_fireworks_response2(
            user_id="upload-classify",
            user_message=_classification_prompt(sample),
            role="user",
            credits=None,
            temp=0.0,
        )
        if not isinstance(resp, str):
            return "policy"
        first = resp.strip().lower().split()[0] if resp.strip() else ""
        first = re.sub(r"[^a-z]", "", first)
        if first in VALID_DOC_TYPES:
            return first
    except Exception as exc:
        logger.warning("Doc-type classification failed: %s", exc)
    return "policy"


_UPLOAD_SCHEMA_DOC = """
{
  "template_version": 1,
  "metadata": {
    "document_id": "POL-001",
    "version": "1.0",
    "effective_date": "2026-01-01",
    "classification": "Internal",
    "title": "Document Title"
  },
  "sections": [
    {"id": "<section_id>", "title": "<title>", "kind": "text", "body_html": "<p>…</p>"},
    {"id": "<section_id>", "title": "<title>", "kind": "statements",
     "statements": [{"id": "<uuid>", "text": "Statement text.", "seq": 1}]}
  ]
}
"""


def _upload_extraction_prompt(html: str, doc_type: str, filename: str) -> str:
    """Build the Fireworks prompt that maps uploaded HTML to V2 structured sections."""
    try:
        template = get_template(doc_type)
        sections_desc = "\n".join(
            f"  - id={s.id}  title={s.title!r}  kind={s.kind}  required={s.required}"
            for s in template
        )
    except KeyError:
        sections_desc = "(unknown template)"

    ext = os.path.splitext(filename or "")[1].lower().lstrip(".") or "file"

    return (
        "You are a compliance document structuring assistant. The HTML below was extracted "
        f"from a user-uploaded {ext} file ({filename!r}). It may contain layout artifacts: "
        "stray page numbers, repeated headers/footers, hyphenated word splits across lines, "
        "and inconsistent heading hierarchy. Clean these as needed but preserve the "
        "substantive content faithfully. Do NOT invent content that is not present.\n\n"
        "If source content does not clearly map to a section in the target template, place "
        "the closest content into the most-applicable section and leave any non-matching "
        "content in that section's body_html.\n\n"
        "TARGET SCHEMA:\n"
        f"{_UPLOAD_SCHEMA_DOC}\n\n"
        "TEMPLATE SECTIONS (map each heading to the closest section id):\n"
        f"{sections_desc}\n\n"
        "RULES:\n"
        "- Assign every statement / step <li> a unique UUID v4 in the `id` field.\n"
        "- For text sections, preserve cleaned content in body_html.\n"
        "- Include every section from the template; use empty body_html (or empty statements) for missing ones.\n"
        "- Return ONLY valid JSON — no markdown, no code fences, no explanation.\n\n"
        f"SOURCE HTML:\n{(html or '')[:80000]}\n\n"
        "JSON:"
    )


def _render_upload_sections_to_html(sections: list) -> str:
    """Render structured sections back to canonical HTML for template validation."""
    parts = []
    for sec in sections or []:
        sec_id = sec.get("id", "")
        title = sec.get("title", "")
        parts.append(f'<div data-section-id="{sec_id}">')
        parts.append(f'<h2 data-section-id="{sec_id}">{title}</h2>')
        statements = sec.get("statements") or []
        if statements:
            parts.append("<ul>")
            for stmt in statements:
                sid = stmt.get("id", "")
                stmt_text = stmt.get("text", "")
                attr = f' data-statement-id="{sid}"' if sid else ""
                parts.append(f"<li{attr}>{stmt_text}</li>")
            parts.append("</ul>")
        if sec.get("body_html"):
            parts.append(sec["body_html"])
        parts.append("</div>")
    return "\n".join(parts)


def _parse_llm_json(raw: str) -> dict | None:
    """Tolerant JSON parse: strip fences, fall back to regex-extracted object."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ── Upload worker ────────────────────────────────────────────────────────────


def _upload_worker(
    user_id: str,
    job_id: str,
    files_payload: list,
    frameworks: list,
    remote_addr: str | None,
    actor_email: str | None,
):
    """Background worker. For each uploaded file: extract → classify → write YAML → LLM-map → index."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    v2 = policy_hub_v2_enabled(user_id)
    try:
        total = len(files_payload)
        for i, file_info in enumerate(files_payload):
            policy_id = file_info["policy_id"]
            filename = file_info["filename"]
            ext = file_info["ext"]
            file_bytes = file_info["file_bytes"]
            type_hint = file_info.get("type_hint")
            raw_key = file_info.get("raw_key")
            raw_archive_error = file_info.get("raw_archive_error")

            item = None
            try:
                # 1. Extract
                try:
                    html = extract_any(file_bytes, filename)
                except Exception as exc:
                    logger.error("Extraction crashed for %s: %s", filename, exc)
                    html = ""

                extraction_failed = not html or not html.strip()

                # 2. Resolve doc type
                if type_hint and type_hint in VALID_DOC_TYPES:
                    doc_type = type_hint
                elif extraction_failed:
                    doc_type = "policy"
                else:
                    doc_type = loop.run_until_complete(_classify_doc_type_via_llm(html))

                # 3. Build baseline item
                created_at = datetime.now(timezone.utc).isoformat()
                key = _s3_key(user_id, policy_id)
                fallback_title = os.path.splitext(filename)[0] or "Uploaded Document"
                title = (
                    _extract_title(html, fallback=fallback_title)
                    if not extraction_failed
                    else fallback_title
                )
                item = {
                    "policy_id": policy_id,
                    "title": title,
                    "type": doc_type,
                    "frameworks": frameworks,
                    "content": html,
                    "s3_key": key,
                    "created_at": created_at,
                    "etag": str(uuid.uuid4()),
                    "source_file": {
                        "filename": filename,
                        "s3_key": raw_key,
                        "content_type": UPLOAD_MIME_TYPES.get(
                            ext, "application/octet-stream"
                        ),
                        "size_bytes": len(file_bytes),
                        "uploaded_at": created_at,
                    },
                }
                if raw_archive_error:
                    item["source_file"]["archive_error"] = raw_archive_error

                # 3a. Persist the baseline YAML immediately so /list shows the
                # document right away — before any LLM work that may take
                # minutes or fail outright. Don't set migration_status here so
                # the frontend treats it like a regular policy until enrichment
                # decides otherwise.
                if extraction_failed:
                    item["migration_status"] = "extraction_failed"
                try:
                    _write_yaml_to_s3(key, item)
                except Exception as exc:
                    logger.error(
                        "Baseline YAML write failed for %s: %s", filename, exc
                    )

                if not extraction_failed:
                    # 4. LLM-map to V2 structured sections (when V2 is enabled)
                    if v2:
                        prompt = _upload_extraction_prompt(html, doc_type, filename)
                        try:
                            raw = loop.run_until_complete(
                                get_fireworks_response2(
                                    user_id=user_id,
                                    user_message=prompt,
                                    role="user",
                                    credits=None,
                                    temp=0.0,
                                )
                            )
                        except Exception as exc:
                            logger.error("LLM mapping crashed for %s: %s", filename, exc)
                            raw = None

                        if raw == "INSUFFICIENT":
                            item["migration_status"] = "needs_review"
                            item["error"] = "Insufficient credits during LLM mapping"
                        else:
                            structured = _parse_llm_json(raw) if raw else None
                            if structured and isinstance(structured.get("sections"), list):
                                item["template_version"] = 1
                                item["metadata"] = structured.get(
                                    "metadata", {}
                                )
                                item["sections"] = structured.get("sections", [])
                                sections_html = _render_upload_sections_to_html(
                                    item["sections"]
                                )
                                vr = validate_template(sections_html, doc_type)
                                item["validation_status"] = "ok" if vr.ok else "needs_review"
                                item["migration_status"] = "ok"
                            else:
                                # Fall back to legacy enrichment so the doc still has some structure
                                logger.warning(
                                    "LLM mapping returned unparseable JSON for %s — falling back to _enrich_v2",
                                    filename,
                                )
                                _enrich_v2(item, html, doc_type, loop)
                                item.setdefault("migration_status", "needs_review")
                    else:
                        item["migration_status"] = "ok"

                    # 4a. Re-write the enriched YAML so /list returns the
                    # final structured version.
                    try:
                        _write_yaml_to_s3(key, item)
                    except Exception as exc:
                        logger.error(
                            "Enriched YAML write failed for %s: %s", filename, exc
                        )

                    # 5. Sync statements to LanceDB (only when we have structured sections)
                    if v2 and item.get("sections"):
                        _sync_statements(item, user_id, doc_type, loop)

                # 6. Audit log
                try:
                    log_audit_event(
                        action=POLICY_UPLOADED,
                        endpoint="/policy-hub/upload",
                        ip=remote_addr,
                        status="success" if not extraction_failed else "partial",
                        actor_user_id=user_id,
                        actor_email=actor_email,
                        metadata={
                            "policy_id": policy_id,
                            "filename": filename,
                            "type": doc_type,
                            "size_bytes": len(file_bytes),
                            "migration_status": item.get("migration_status"),
                        },
                    )
                except Exception:
                    pass

            except Exception as exc:
                logger.error("Upload worker failed for %s: %s\n%s", filename, exc, traceback.format_exc())
                item = item or {
                    "policy_id": policy_id,
                    "title": filename,
                    "type": type_hint or "policy",
                    "error": str(exc),
                    "migration_status": "needs_review",
                }

            # 7. Update job state
            job = _read_job(job_id) or {"items": [], "completed": 0}
            job["completed"] = i + 1
            if item:
                job["items"].append(item)
            if i + 1 >= total:
                job["status"] = "done"
            _save_job(job_id, job)

        logger.info(
            "Policy upload complete for user %s: %d files", user_id, total
        )
    except Exception as exc:
        logger.error("Upload worker crashed for job %s: %s", job_id, exc)
        try:
            job = _read_job(job_id) or {}
            job["status"] = "error"
            job["error"] = str(exc)
            _save_job(job_id, job)
        except Exception:
            pass
    finally:
        loop.close()


# ── 1. GENERATE ───────────────────────────────────────────────────────────────


@policy_hub_bp.route("/generate", methods=["POST"])
@permission_required_body("policyhub.create")
async def generate_policy():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    prompt = body.get("prompt")
    doc_type = body.get(
        "type"
    )  # kept for metadata only — generation always covers both types
    frameworks = body.get("frameworks", [])
    framework_ids = body.get(
        "framework_ids", []
    )  # UUIDs of uploaded frameworks to draw controls from

    if not user_id or not prompt:
        return jsonify({"error": "user_id and prompt are required"}), 400

    # Resolve uploaded framework names from S3 — never trust client-supplied names for these
    uploaded_fw_names = []
    for fw_id in framework_ids:
        try:
            meta = load_yaml_from_s3(_fw_key(fw_id))
            if meta and meta.get("name"):
                uploaded_fw_names.append(meta["name"])
        except Exception:
            pass

    all_frameworks = (
        frameworks + uploaded_fw_names
    )  # static/custom + S3-resolved uploaded
    fw_list = ", ".join(all_frameworks) if all_frameworks else "general compliance"
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
        "frameworks": all_frameworks,
        "framework_ids": framework_ids,
        "error": None,
    }
    _save_job(job_id, job_state)

    # Phase 2: generate all documents in background — runs to completion regardless of client
    thread = threading.Thread(
        target=_generation_worker,
        args=(
            user_id,
            job_id,
            docs,
            all_frameworks,
            framework_ids,
            prompt,
            fw_list,
            doc_type,
        ),
        daemon=True,
    )
    thread.start()

    return (
        jsonify(
            {
                "job_id": job_id,
                "status": "PROCESSING",
                "total": len(docs),
                "documents": docs,
            }
        ),
        202,
    )


# ── 1b. GENERATE STATUS (polling) ─────────────────────────────────────────────


@policy_hub_bp.route("/status", methods=["GET"])
@permission_required_body("policyhub.view")
def generate_status():
    job_id = request.args.get("job_id")

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    job = _read_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return (
        jsonify(
            {
                "job_id": job_id,
                "status": job.get("status", "processing").upper(),
                "total": job.get("total", 0),
                "completed": job.get("completed", 0),
                "items": job.get("items", []),
                "documents": job.get("documents", []),
                "error": job.get("error"),
            }
        ),
        200,
    )


# ── 1d. UPLOAD ────────────────────────────────────────────────────────────────


@policy_hub_bp.route("/upload", methods=["POST"])
@permission_required_body("policyhub.create")
def upload_policies():
    """Upload one or more PDF/DOCX/HTML files as Policy Hub documents.

    Multipart form fields:
      files        (required, repeatable) — file parts
      user_id      (required) — target user id (composite or raw)
      frameworks   (optional) — JSON-encoded list of framework names
      types        (optional) — JSON-encoded map of filename -> doc type
      default_type (optional) — fallback doc type for files without a hint
    """
    logged_in_user_id, user_id = _resolve_user_id_multi_source()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "At least one file is required"}), 400
    if len(files) > UPLOAD_MAX_FILES_PER_REQUEST:
        return (
            jsonify(
                {
                    "error": f"Too many files. Maximum is {UPLOAD_MAX_FILES_PER_REQUEST} per request."
                }
            ),
            400,
        )

    try:
        frameworks = json.loads(request.form.get("frameworks") or "[]")
        if not isinstance(frameworks, list):
            frameworks = []
    except json.JSONDecodeError:
        return jsonify({"error": "frameworks must be a JSON array of strings"}), 400

    try:
        types_map = json.loads(request.form.get("types") or "{}")
        if not isinstance(types_map, dict):
            types_map = {}
    except json.JSONDecodeError:
        return jsonify({"error": "types must be a JSON object mapping filename to type"}), 400

    default_type = (request.form.get("default_type") or "").strip().lower()
    if default_type and default_type not in VALID_DOC_TYPES:
        return (
            jsonify(
                {"error": f"default_type must be one of {sorted(VALID_DOC_TYPES)}"}
            ),
            400,
        )

    # Validate and read each file
    files_payload = []
    response_files = []
    s3 = s3bucket()

    for f in files:
        if not f or not f.filename:
            continue
        filename = f.filename
        ext = os.path.splitext(filename)[1].lower()
        if ext not in UPLOAD_ALLOWED_EXTENSIONS:
            return (
                jsonify(
                    {
                        "error": f"Unsupported file type '{ext}' for {filename}. Accepted: {sorted(UPLOAD_ALLOWED_EXTENSIONS)}"
                    }
                ),
                400,
            )

        file_bytes = f.read()
        if not file_bytes:
            return jsonify({"error": f"File '{filename}' is empty"}), 400
        if len(file_bytes) > UPLOAD_MAX_BYTES:
            return (
                jsonify(
                    {
                        "error": f"File '{filename}' exceeds maximum size of {UPLOAD_MAX_BYTES // (1024 * 1024)}MB",
                        "filename": filename,
                    }
                ),
                413,
            )

        policy_id = str(uuid.uuid4())
        raw_key = _raw_file_key(user_id, policy_id, ext)
        mimetype = UPLOAD_MIME_TYPES.get(ext, "application/octet-stream")
        raw_archive_error = None
        try:
            s3.upload_fileobj(
                io.BytesIO(file_bytes),
                S3_BUCKET,
                raw_key,
                ExtraArgs={"ContentType": mimetype},
            )
        except Exception as exc:
            logger.error("Raw file archival failed for %s: %s", filename, exc)
            raw_archive_error = str(exc)

        # Resolve type hint: explicit map → default_type → None (worker classifies)
        type_hint = types_map.get(filename) or default_type or None
        if type_hint and type_hint not in VALID_DOC_TYPES:
            type_hint = None

        files_payload.append(
            {
                "policy_id": policy_id,
                "filename": filename,
                "ext": ext,
                "file_bytes": file_bytes,
                "type_hint": type_hint,
                "raw_key": raw_key,
                "raw_archive_error": raw_archive_error,
            }
        )
        response_files.append(
            {
                "policy_id": policy_id,
                "filename": filename,
                "type_hint": type_hint,
                "size_bytes": len(file_bytes),
            }
        )

    if not files_payload:
        return jsonify({"error": "No valid files in request"}), 400

    # Create job
    job_id = str(uuid.uuid4())
    job_state = {
        "job_id": job_id,
        "user_id": user_id,
        "status": "processing",
        "total": len(files_payload),
        "completed": 0,
        "items": [],
        "files": response_files,
        "frameworks": frameworks,
        "error": None,
    }
    _save_job(job_id, job_state)

    # Capture context that the background thread can't read off `request`
    remote_addr = request.remote_addr
    try:
        actor_email = get_email_by_id(user_id)
    except Exception:
        actor_email = None

    threading.Thread(
        target=_upload_worker,
        args=(user_id, job_id, files_payload, frameworks, remote_addr, actor_email),
        daemon=True,
    ).start()

    return (
        jsonify(
            {
                "job_id": job_id,
                "status": "PROCESSING",
                "total": len(files_payload),
                "files": response_files,
            }
        ),
        202,
    )


@policy_hub_bp.route("/upload-status", methods=["GET"])
@permission_required_body("policyhub.view")
def upload_status():
    """Poll the state of a /upload job."""
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    job = _read_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return (
        jsonify(
            {
                "job_id": job_id,
                "status": job.get("status", "processing").upper(),
                "total": job.get("total", 0),
                "completed": job.get("completed", 0),
                "items": job.get("items", []),
                "files": job.get("files", []),
                "error": job.get("error"),
            }
        ),
        200,
    )


@policy_hub_bp.route("/download-raw", methods=["GET"])
@permission_required_body("policyhub.view")
def download_raw_policy():
    """Return a presigned S3 URL for the original uploaded file."""
    baseuser = request.args.get("user_id")
    policy_id = request.args.get("policy_id")
    if not baseuser or not policy_id:
        return jsonify({"error": "user_id and policy_id are required"}), 400

    user_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    data = load_yaml_from_s3(_s3_key(user_id, policy_id))
    if not data:
        return jsonify({"error": "Policy not found"}), 404

    source = data.get("source_file") or {}
    raw_key = source.get("s3_key")
    if not raw_key:
        return jsonify({"error": "No raw file archived for this policy"}), 404

    try:
        url = s3bucket().generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": raw_key},
            ExpiresIn=300,
        )
    except Exception as exc:
        logger.error("Failed to presign raw download for %s: %s", policy_id, exc)
        return jsonify({"error": "Failed to generate download URL"}), 500

    return (
        jsonify(
            {
                "url": url,
                "filename": source.get("filename"),
                "content_type": source.get("content_type"),
                "size_bytes": source.get("size_bytes"),
                "expires_in": 300,
            }
        ),
        200,
    )


# ── Section helpers ───────────────────────────────────────────────────────────


def _split_sections(html: str) -> list[tuple[str, int, int]]:
    """Split document into (section_html, start, end) chunks at <h2> boundaries."""
    h2_iter = list(re.finditer(r"<h2[\s>]", html, re.IGNORECASE))
    if not h2_iter:
        return [(html, 0, len(html))]
    sections = []
    if h2_iter[0].start() > 0:
        sections.append((html[: h2_iter[0].start()], 0, h2_iter[0].start()))
    for i, m in enumerate(h2_iter):
        start = m.start()
        end = h2_iter[i + 1].start() if i + 1 < len(h2_iter) else len(html)
        sections.append((html[start:end], start, end))
    return sections


def _find_section(html: str, needle: str) -> tuple[str, int, int] | None:
    """Return (section_html, start, end) for the <h2> section containing needle."""
    if not needle:
        return None
    needle_plain = re.sub(r"<[^>]+>", "", needle).strip()[:80]
    for section_html, start, end in _split_sections(html):
        if needle_plain and needle_plain in re.sub(r"<[^>]+>", "", section_html):
            return section_html, start, end
    return None


# ── 1c. EDIT ──────────────────────────────────────────────────────────────────


def _build_statement_id_instructions(section_html: str) -> str:
    """Extract existing data-statement-id values from *section_html* and build the
    preservation instruction block injected into the edit prompt."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(section_html, "lxml")
    existing_ids = [li.get("data-statement-id") for li in soup.find_all("li") if li.get("data-statement-id")]
    if not existing_ids:
        return ""
    ids_list = "\n".join(f"  - {sid}" for sid in existing_ids)
    return (
        "\n\nSTATEMENT ID PRESERVATION RULES (mandatory):\n"
        "The <li> elements in this section have stable data-statement-id attributes that\n"
        "MUST be preserved verbatim in your output. The existing IDs are:\n"
        + ids_list
        + "\n- Keep each id on the rewritten item that corresponds to it.\n"
        "- If you split a statement, keep the original id on the first part; add a NEW UUID on the second.\n"
        "- If you merge statements, keep one id; omit the others (they'll be marked superseded).\n"
        "- If you delete a statement, simply omit its <li> entirely.\n"
        "- New statements you add must each have a NEW UUID data-statement-id.\n"
    )


def _edit_worker(
    user_id,
    job_id,
    policy_id,
    document_title,
    document_content,
    instruction,
    selected_text,
    section_title,
):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    v2 = policy_hub_v2_enabled(user_id)
    try:
        credits = Credits()
        needle = selected_text or section_title
        section_result = _find_section(document_content, needle) if needle else None

        if section_result:
            section_html, sec_start, sec_end = section_result
            before = document_content[:sec_start]
            after = document_content[sec_end:]
            focus = (
                f'The user selected: "{selected_text[:120]}"\n'
                if selected_text
                else f'Target section: "{section_title}"\n'
            )
            stmt_id_block = _build_statement_id_instructions(section_html) if v2 else ""
            ai_prompt = (
                "You are an expert GRC policy writer editing a compliance document section.\n\n"
                f"Document title: {document_title}\n"
                f"Instruction: {instruction}\n"
                + focus
                + "\nRewrite this section per the instruction:\n\n"
                + section_html
                + stmt_id_block
                + "\n\nReturn EXACTLY:\n"
                "[EXPLANATION]\n1–2 sentence summary.\n[/EXPLANATION]\n"
                "[SECTION]\nRewritten section HTML only — no surrounding document, no code fences.\n[/SECTION]\n\n"
                "Rules:\n- Preserve all inline styles and heading tags\n"
                "- Keep framework citations intact unless instructed to change\n- Do not truncate"
            )
            response = loop.run_until_complete(
                get_fireworks_response2(
                    user_id=user_id,
                    user_message=ai_prompt,
                    role="user",
                    credits=credits,
                    temp=0.1,
                )
            )
            if response == "INSUFFICIENT":
                _save_job(job_id, {"status": "error", "error": "Insufficient credits"})
                return
            explanation = _extract_tag(response, "EXPLANATION")
            new_section = _extract_tag(response, "SECTION") or response.strip()
            updated_content = before + new_section + after
        else:
            ai_prompt = (
                "You are an expert GRC policy writer editing a compliance document.\n\n"
                f"Document title: {document_title}\n"
                f"Instruction: {instruction}\n\n"
                "Return EXACTLY:\n"
                "[EXPLANATION]\n1–2 sentence summary.\n[/EXPLANATION]\n"
                "[HTML]\nFull updated document HTML.\n[/HTML]\n\n"
                "Rules:\n- Return ONLY valid HTML — no markdown, no code fences\n"
                "- Preserve all inline styles and structure\n"
                "- Keep framework citations intact unless instructed\n"
                "- Do not truncate\n\n"
                f"Current document HTML:\n{document_content}"
            )
            response = loop.run_until_complete(
                get_fireworks_response2(
                    user_id=user_id,
                    user_message=ai_prompt,
                    role="user",
                    credits=credits,
                    temp=0.1,
                )
            )
            if response == "INSUFFICIENT":
                _save_job(job_id, {"status": "error", "error": "Insufficient credits"})
                return
            explanation = _extract_tag(response, "EXPLANATION")
            updated_content = _extract_tag(response, "HTML")
            if not updated_content:
                _save_job(
                    job_id, {"status": "error", "error": "AI did not return valid HTML"}
                )
                return

        key = _s3_key(user_id, policy_id)
        existing = load_yaml_from_s3(key)
        if existing:
            existing["content"] = updated_content
            existing["updated_at"] = datetime.now(timezone.utc).isoformat()
            existing["etag"] = str(uuid.uuid4())

            if v2:
                # Reconcile statement IDs and rebuild structured sections
                doc_type = existing.get("type", "policy")
                threshold = statement_reid_threshold(user_id)
                existing = _reconcile_and_enrich_edit(
                    existing, updated_content, doc_type, threshold, loop
                )

            try:
                _write_yaml_to_s3(key, existing)
            except Exception as e:
                logger.error("Failed to persist edit for policy %s: %s", policy_id, e)

        _save_job(
            job_id,
            {
                "status": "done",
                "updated_content": updated_content,
                "explanation": explanation
                or "The document has been updated per your instruction.",
            },
        )

    except Exception as e:
        logger.error("Edit worker crashed for job %s: %s", job_id, e)
        try:
            _save_job(job_id, {"status": "error", "error": str(e)})
        except Exception:
            pass
    finally:
        loop.close()


def _reconcile_and_enrich_edit(
    existing: dict,
    updated_content: str,
    doc_type: str,
    threshold: float,
    loop: asyncio.AbstractEventLoop,
) -> dict:
    """Reconcile statement IDs after an edit, rebuild sections, sync to LanceDB."""
    from policy_hub.structured import Statement

    policy_id = existing.get("policy_id", "")
    version = existing.get("metadata", {}).get("version", "1.0")

    try:
        # Build old statements index from existing YAML sections
        old_stmts_by_section: dict[str, list] = {}
        for sec in existing.get("sections", []):
            raw_stmts = sec.get("statements", [])
            if raw_stmts:
                old_stmts_by_section[sec["id"]] = [
                    Statement(
                        id=s["id"],
                        text=s["text"],
                        seq=s["seq"],
                        section_id=s.get("section_id", sec["id"]),
                    )
                    for s in raw_stmts
                ]

        # Parse the updated HTML into new sections
        parsed = parse_document_html(updated_content, doc_type)
        validation = validate_template(updated_content, doc_type)
        existing["validation_status"] = "ok" if validation.ok else "needs_review"

        all_active: list[Statement] = []
        all_superseded: list[Statement] = []
        new_sections_data = []

        for sec in parsed.sections:
            old = old_stmts_by_section.get(sec.id, [])
            if sec.statements and old:
                # Reconcile IDs for this section
                sec_html = sec.body_html
                active, superseded = reconcile_statement_ids(
                    old, sec_html, sec.id, similarity_threshold=threshold
                )
                # Replace the parsed statements with reconciled ones
                sec.statements = active
                all_active.extend(active)
                all_superseded.extend(superseded)
            else:
                all_active.extend(sec.statements)

            sec_dict: dict = {
                "id": sec.id,
                "title": sec.title,
                "kind": sec.kind,
                "body_html": sec.body_html,
            }
            if sec.statements:
                sec_dict["statements"] = [
                    {
                        "id": s.id,
                        "text": s.text,
                        "seq": s.seq,
                        "section_id": s.section_id,
                        "status": s.status,
                    }
                    for s in sec.statements
                ]
            new_sections_data.append(sec_dict)

        existing["sections"] = new_sections_data

        # Sync to LanceDB
        try:
            loop.run_until_complete(
                sync_statements_to_lance(
                    policy_id=policy_id,
                    doc_type=doc_type,
                    version=version,
                    statements=all_active,
                    superseded=all_superseded,
                )
            )
        except Exception as exc:
            logger.error(
                "_reconcile_and_enrich_edit LanceDB sync failed for policy=%s: %s",
                policy_id,
                exc,
            )

    except Exception as exc:
        logger.error(
            "_reconcile_and_enrich_edit failed for policy=%s: %s", policy_id, exc
        )

    return existing


@policy_hub_bp.route("/edit", methods=["POST"])
@permission_required_body("policyhub.edit")
def edit_policy():
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id")
    policy_id = body.get("policy_id")
    document_title = body.get("document_title", "")
    document_content = body.get("document_content", "")
    instruction = body.get("instruction", "")
    selected_text = body.get("selected_text", "").strip()
    section_title = body.get("section_title", "").strip()

    if not baseuser or not policy_id or not document_content or not instruction:
        return (
            jsonify(
                {
                    "error": "user_id, policy_id, document_content, and instruction are required"
                }
            ),
            400,
        )
    user_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    job_id = str(uuid.uuid4())
    _save_job(job_id, {"status": "processing"})

    thread = threading.Thread(
        target=_edit_worker,
        args=(
            user_id,
            job_id,
            policy_id,
            document_title,
            document_content,
            instruction,
            selected_text,
            section_title,
        ),
        daemon=True,
    )
    thread.start()

    return jsonify({"edit_job_id": job_id, "status": "PROCESSING"}), 202


@policy_hub_bp.route("/edit-status", methods=["GET"])
@permission_required_body("policyhub.view")
@permission_required_body("policyhub.view")
def edit_status():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    job = _read_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return (
        jsonify(
            {
                "status": job.get("status", "processing").upper(),
                "updated_content": job.get("updated_content"),
                "explanation": job.get("explanation"),
                "error": job.get("error"),
            }
        ),
        200,
    )


# ── 2. LIST ───────────────────────────────────────────────────────────────────


@policy_hub_bp.route("/list", methods=["GET"])
@permission_required_body("policyhub.view")
@permission_required_body("policyhub.view")
def list_policies():
    raw_user_id = request.args.get("user_id")
    if not raw_user_id:
        return jsonify({"error": "user_id is required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(raw_user_id)

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

    # Union any policies shared TO `user_id` (the resolved owner from the parsed
    # request — equals the requester for plain user_ids, equals the impersonation
    # target for composite). This must run for both cases, otherwise composite
    # admin views miss their shared-to-them policies.
    try:
        shared_index = get_user_shared_resources(user_id, "policy") or {}
    except Exception:
        shared_index = {}
    for policy_id, entry in shared_index.items():
        owner_id = entry.get("mainuser_id")
        if not owner_id or owner_id == user_id:
            continue
        owner_policy = load_yaml_from_s3(_s3_key(owner_id, policy_id))
        if not owner_policy:
            continue
        owner_policy = {**owner_policy, "owner_user_id": owner_id, "shared": True}
        items.append(owner_policy)

    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify({"items": items}), 200


# ── 3. UPDATE ─────────────────────────────────────────────────────────────────


@policy_hub_bp.route("/update", methods=["POST"])
@permission_required_body("policyhub.edit")
def update_policy():
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id")
    policy_id = body.get("policy_id")

    if not baseuser or not policy_id:
        return jsonify({"error": "user_id and policy_id are required"}), 400
    user_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    key = _s3_key(user_id, policy_id)
    existing = load_yaml_from_s3(key)
    if not existing:
        return jsonify({"error": "Policy not found"}), 404

    v2 = policy_hub_v2_enabled(user_id)

    # Optimistic locking: clients that send an etag must match the stored one.
    if v2 and "etag" in body:
        if body["etag"] != existing.get("etag"):
            return jsonify(
                {
                    "error": "Document was modified since you last loaded it. Please reload.",
                    "current_etag": existing.get("etag"),
                }
            ), 409

    content_updated = False
    if "title" in body:
        existing["title"] = body["title"]
    if "content" in body:
        existing["content"] = body["content"]
        content_updated = True
    if "frameworks" in body:
        existing["frameworks"] = body["frameworks"]
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing["etag"] = str(uuid.uuid4())

    if v2 and content_updated:
        doc_type = existing.get("type", "policy")
        threshold = statement_reid_threshold(user_id)
        loop = asyncio.new_event_loop()
        try:
            existing = _reconcile_and_enrich_edit(
                existing, existing["content"], doc_type, threshold, loop
            )
        finally:
            loop.close()

    try:
        _write_yaml_to_s3(key, existing)
    except Exception as e:
        logger.error("Failed to update policy in S3: %s", e)
        return jsonify({"error": "Failed to update policy"}), 500

    return jsonify({"status": "ok"}), 200


# ── 4. DELETE ─────────────────────────────────────────────────────────────────


@policy_hub_bp.route("/delete", methods=["DELETE"])
@permission_required_body("policyhub.delete")
def delete_policy():
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id")
    policy_id = body.get("policy_id")

    if not baseuser or not policy_id:
        return jsonify({"error": "user_id and policy_id are required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(baseuser)
    if logged_in_user_id and logged_in_user_id != user_id:
        # Only the owner may delete a policy.
        return jsonify({"error": "Only the owner can delete a policy"}), 403

    key = _s3_key(user_id, policy_id)

    # Look up any archived raw upload BEFORE deleting the YAML so we can clean it up too.
    raw_key = None
    try:
        existing = load_yaml_from_s3(key)
        if existing and isinstance(existing.get("source_file"), dict):
            raw_key = existing["source_file"].get("s3_key")
    except Exception as exc:
        logger.warning("Could not inspect source_file before delete for %s: %s", policy_id, exc)

    ok = delete_file_from_s3(key)
    if not ok:
        return jsonify({"error": "Delete failed or file not found"}), 500

    if raw_key:
        try:
            delete_file_from_s3(raw_key)
        except Exception as exc:
            logger.warning(
                "Raw file cleanup failed for policy=%s raw_key=%s: %s",
                policy_id, raw_key, exc,
            )

    return jsonify({"status": "ok"}), 200


# ── 5. FRAMEWORKS (service@bytoid.ca only) ────────────────────────────────────


ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".xlsb", ".xlsm", ".ods", ".tsv"}
FRAMEWORK_LANCE_USER = "frameworks"  # LanceDB table: index_frameworks


def _fw_key(framework_id: str) -> str:
    return f"{FRAMEWORK_OWNER}/frameworks/{framework_id}.yaml"


def _require_framework_owner():
    # Resolution order: session middleware → Flask session → request (form/args/body)
    user_id = (
        getattr(g, "user_id", None)
        or getattr(g, "session_user_id", None)
        or request.form.get("user_id")
        or request.args.get("user_id")
        or (request.get_json(silent=True) or {}).get("user_id")
    )
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    logged_in_user_id, user_id = parse_composite_user_id(user_id)
    user = getattr(g, "user", None) or {}
    email = user.get("email")
    if not email:
        try:
            email = get_email_by_id(user_id)
        except Exception:
            # DB unavailable — fail fast rather than blocking the worker
            return jsonify({"error": "Unauthorized"}), 401
    if email != FRAMEWORK_OWNER:
        return jsonify({"error": "Access denied"}), 403
    return None


def _parse_framework_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Parse an Excel/CSV file into a list of row dicts."""
    import math

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
    elif ext == ".tsv":
        df = pd.read_csv(io.BytesIO(file_bytes), sep="\t", dtype=str)
    else:
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)

    records = df.to_dict(orient="records")
    # pandas may leave float NaN in cells even with dtype=str; NaN is not valid JSON
    return [
        {
            k: (None if (isinstance(v, float) and math.isnan(v)) else v)
            for k, v in row.items()
        }
        for row in records
    ]


async def _async_index_framework(framework_id: str, rows: list[dict]):
    """Embed every row and upsert into LanceDB index_frameworks table."""
    texts = []
    for row in rows:
        parts = [
            f"{k}: {v}" for k, v in row.items() if v is not None and str(v).strip()
        ]
        if parts:
            texts.append(" | ".join(parts))
    if not texts:
        return

    embeddings = await get_firework_embedding()
    vecs = await asyncio.to_thread(embeddings.embed_documents, texts)

    vectors = [
        VectorData(
            user_id=FRAMEWORK_LANCE_USER,
            id=str(uuid.uuid4()),
            text=text,
            embedding=[float(x) for x in vec],
            foldername=framework_id,
        )
        for text, vec in zip(texts, vecs)
    ]
    lance = LanceDBServer()
    await lance.insert_batch(vectors)
    logger.info(
        "Indexed %d rows for framework %s in LanceDB", len(vectors), framework_id
    )


def _lance_index_worker(framework_id: str, rows: list[dict]):
    """Daemon thread: run LanceDB indexing without blocking the HTTP response."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_index_framework(framework_id, rows))
    except Exception as e:
        logger.error("LanceDB framework indexing failed for %s: %s", framework_id, e)
    finally:
        loop.close()


@policy_hub_bp.route("/frameworks/available", methods=["GET"])
@permission_required_body("policyhub.framework.view")
def list_available_frameworks():
    """Return all framework names + IDs for the Select Frameworks dropdown.

    No admin check — any authenticated user (user_id present) may call this.
    Returns only metadata; row content stays in LanceDB.
    """
    user_id = (
        getattr(g, "user_id", None)
        or getattr(g, "session_user_id", None)
        or request.args.get("user_id")
    )
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    logged_in_user_id, user_id = parse_composite_user_id(user_id)

    prefix = f"{FRAMEWORK_OWNER}/frameworks/"
    objects = list_all_files(folder=prefix)

    frameworks = []
    for obj in objects:
        key = obj.get("Key", "")
        if not key.endswith(".yaml"):
            continue
        data = load_yaml_from_s3(key)
        if data:
            frameworks.append(
                {
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "row_count": data.get("row_count", 0),
                }
            )

    frameworks.sort(key=lambda x: (x.get("name") or "").lower())
    return jsonify({"frameworks": frameworks}), 200


@policy_hub_bp.route("/frameworks/access", methods=["GET"])
@permission_required_body("policyhub.framework.view")
def check_framework_access():
    """Return whether the authenticated session has framework access.

    Always returns 200 so the frontend can check the flag without error handling.
    """
    denied = _require_framework_owner()
    if denied:
        return jsonify({"has_access": False}), 200
    return jsonify({"has_access": True}), 200


@policy_hub_bp.route("/frameworks", methods=["GET"])
@permission_required_body("policyhub.framework.view")
def list_frameworks():
    denied = _require_framework_owner()
    if denied:
        return denied

    prefix = f"{FRAMEWORK_OWNER}/frameworks/"
    objects = list_all_files(folder=prefix)

    frameworks = []
    for obj in objects:
        key = obj.get("Key", "")
        if not key.endswith(".yaml"):
            continue
        data = load_yaml_from_s3(key)
        if data:
            # Rows live in LanceDB — strip them from the listing response
            meta = {k: v for k, v in data.items()}
            frameworks.append(meta)

    frameworks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify({"frameworks": frameworks}), 200


@policy_hub_bp.route("/frameworks/list", methods=["GET"])
@permission_required_body("policyhub.framework.view")
def list_frameworks_rows():
    # denied = _require_framework_owner()
    # if denied:
    #     return denied

    prefix = f"{FRAMEWORK_OWNER}/frameworks/"
    objects = list_all_files(folder=prefix)

    frameworks = []
    for obj in objects:
        key = obj.get("Key", "")
        if not key.endswith(".yaml"):
            continue
        data = load_yaml_from_s3(key)
        if data:
            # Rows live in LanceDB — strip them from the listing response
            meta = {k: v for k, v in data.items()}
            frameworks.append(meta)

    frameworks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify({"frameworks": frameworks}), 200


@policy_hub_bp.route("/frameworks/upload", methods=["POST"])
@permission_required_body("policyhub.framework.create")
def upload_framework_preview():
    """Parse an uploaded file and return a preview — nothing is saved yet."""
    denied = _require_framework_owner()
    if denied:
        return denied

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return (
            jsonify(
                {
                    "error": f"Unsupported file type '{ext}'. Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
                }
            ),
            400,
        )

    try:
        rows = _parse_framework_file(file.read(), file.filename)
    except Exception as e:
        logger.error("Framework parse error: %s", e)
        return (
            jsonify(
                {
                    "error": "Could not parse file. Please check the format and try again."
                }
            ),
            422,
        )

    return (
        jsonify(
            {
                "rows": rows,
                "columns": list(rows[0].keys()) if rows else [],
                "row_count": len(rows),
                "source_filename": file.filename,
            }
        ),
        200,
    )


@policy_hub_bp.route("/frameworks/save", methods=["POST"])
@permission_required_body("policyhub.framework.create")
def save_framework():
    """Confirm and persist a framework (new or update)."""
    denied = _require_framework_owner()
    if denied:
        return denied

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    rows = body.get("rows")
    source_filename = body.get("source_filename", "")
    framework_id = body.get("framework_id") or str(uuid.uuid4())

    if not name:
        return jsonify({"error": "Framework name is required"}), 400
    if not isinstance(rows, list):
        return jsonify({"error": "rows must be a list"}), 400

    now = datetime.now(timezone.utc).isoformat()
    key = _fw_key(framework_id)

    existing = load_yaml_from_s3(key)
    record = {
        "id": framework_id,
        "name": name,
        "source_filename": source_filename,
        "rows": rows,
        "columns": list(rows[0].keys()) if rows else [],
        "row_count": len(rows),
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }

    try:
        _write_yaml_to_s3(key, record)
    except Exception as e:
        logger.error("Failed to save framework %s: %s", framework_id, e)
        return jsonify({"error": "Failed to save framework"}), 500

    # Index rows in LanceDB in the background — don't block the HTTP response
    threading.Thread(
        target=_lance_index_worker,
        args=(framework_id, rows),
        daemon=True,
    ).start()

    return jsonify({"framework": record}), 200


@policy_hub_bp.route("/frameworks/search", methods=["GET"])
@permission_required_body("policyhub.framework.view")
async def search_frameworks():
    """Semantic search over framework rows stored in LanceDB."""
    denied = _require_framework_owner()
    if denied:
        return denied

    q = request.args.get("q", "").strip()
    framework_id = request.args.get("framework_id", "").strip()
    top_k = min(int(request.args.get("top_k", 20)), 100)

    if not q:
        return jsonify({"error": "q (query) is required"}), 400

    embeddings = await get_firework_embedding()
    vec = await asyncio.to_thread(embeddings.embed_query, q)

    lance = LanceDBServer()
    query = QueryData(user_id=FRAMEWORK_LANCE_USER, embedding=vec, top_k=top_k)

    if framework_id:
        results = await lance.query_vector_filename(query, framework_id)
    else:
        results = await lance.query_vector(query)

    return (
        jsonify(
            {
                "results": [
                    {
                        "text": r["text"],
                        "framework_id": r.get("foldername"),
                        "score": r.get("_distance"),
                    }
                    for r in results
                ],
                "query": q,
                "total": len(results),
            }
        ),
        200,
    )


@policy_hub_bp.route("/frameworks/<framework_id>", methods=["GET"])
@permission_required_body("policyhub.framework.view")
def get_framework(framework_id: str):
    denied = _require_framework_owner()
    if denied:
        return denied

    data = load_yaml_from_s3(_fw_key(framework_id))
    if not data:
        return jsonify({"error": "Framework not found"}), 404
    return jsonify({"framework": data}), 200


@policy_hub_bp.route("/frameworks/<framework_id>", methods=["DELETE"])
@permission_required_body("policyhub.framework.delete")
async def delete_framework(framework_id: str):
    denied = _require_framework_owner()
    if denied:
        return denied

    ok = delete_file_from_s3(_fw_key(framework_id))
    if not ok:
        return jsonify({"error": "Delete failed or framework not found"}), 500

    # Remove vectors from LanceDB
    try:
        lance = LanceDBServer()
        await lance.delete_folder_async(FRAMEWORK_LANCE_USER, framework_id)
    except Exception as e:
        logger.error("LanceDB delete failed for framework %s: %s", framework_id, e)

    return jsonify({"status": "ok"}), 200


# ─────────────────────────────────────────────────────────────
# Policy share / assign access
# ─────────────────────────────────────────────────────────────


@policy_hub_bp.route("/share", methods=["POST"])
@permission_required_body("policyhub.edit")
def share_policy():
    data = request.get_json() or {}
    baseuser = data.get("user_id")
    policy_id = data.get("policy_id")
    policy_name = data.get("policy_name")
    assignment_type = data.get("assignment_type")
    client_user_id = data.get("client_user_id")
    role_id = data.get("role_id")

    if not baseuser or not policy_id or not assignment_type:
        return (
            jsonify({"error": "user_id, policy_id, assignment_type required"}),
            400,
        )

    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400

    if not policy_name:
        owner_policy = load_yaml_from_s3(_s3_key(admin_id, policy_id))
        if owner_policy:
            policy_name = owner_policy.get("title") or policy_id
        else:
            policy_name = policy_id

    conn = None
    try:
        conn = connect_to_rds()
        required_permission = "policyhub.view"

        if assignment_type == "manual":
            if not client_user_id:
                return jsonify({"error": "client_user_id required for manual"}), 400
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT email FROM users WHERE user_id=%s", (client_user_id,)
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404
                user_email = row["email"]

        elif assignment_type == "role":
            if not role_id:
                return jsonify({"error": "role_id required for role"}), 400
            if not check_role_has_permission(
                conn, admin_id, role_id, required_permission
            ):
                return (
                    jsonify({"error": "Role does not have policy view permission"}),
                    403,
                )
            user_obj, error_msg = get_round_robin_user_for_resource(
                admin_id, role_id, "policy", conn, required_permission
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
            "policy",
            admin_id,
            admin_email,
            client_user_id,
            user_email,
            policy_id,
            policy_name,
            conn,
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
            action=POLICY_SHARED,
            endpoint="/policy-hub/share",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "policy_id": policy_id,
                "target_user_id": client_user_id,
                "assignment_type": assignment_type,
                "role_id": role_id,
            },
        )
        g.audit_logged = True

        return (
            jsonify(
                {
                    "success": True,
                    "policy_id": policy_id,
                    "client_user_id": client_user_id,
                    "sharing_access": sharing_access,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error("share_policy error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@policy_hub_bp.route("/revoke-share", methods=["POST"])
@permission_required_body("policyhub.edit")
def revoke_policy_share():
    data = request.get_json() or {}
    baseuser = data.get("user_id")
    client_user_id = data.get("client_user_id")
    policy_id = data.get("policy_id")

    if not baseuser or not client_user_id or not policy_id:
        return (
            jsonify({"error": "user_id, client_user_id, policy_id required"}),
            400,
        )

    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400

    try:
        sharing_access, error = core_revoke_resource(
            "policy", admin_id, client_user_id, policy_id
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
            action=POLICY_SHARE_REVOKED,
            endpoint="/policy-hub/revoke-share",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            acting_on_behalf_of_user_id=acting_on_behalf_of_user_id,
            acting_on_behalf_of_email=acting_on_behalf_of_email,
            metadata={
                "policy_id": policy_id,
                "target_user_id": client_user_id,
            },
        )
        g.audit_logged = True

        return jsonify({"success": True, "sharing_access": sharing_access}), 200
    except Exception as e:
        logger.error("revoke_policy_share error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@policy_hub_bp.route("/sharing/<policy_id>", methods=["GET"])
@permission_required_body("policyhub.view")
def get_policy_sharing(policy_id):
    baseuser = request.args.get("user_id")
    if not baseuser:
        return jsonify({"error": "user_id query param required"}), 400
    _, admin_id = parse_composite_user_id(baseuser)
    if not admin_id:
        return jsonify({"error": "Invalid user_id"}), 400
    try:
        sharing_access, _ = core_list_resource_shares("policy", admin_id, policy_id)
        return jsonify({"sharing_access": sharing_access}), 200
    except Exception as e:
        logger.error("get_policy_sharing error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@policy_hub_bp.route("/shared", methods=["GET"])
@permission_required_body("policyhub.view")
def list_shared_policies():
    """List policies shared TO the requesting user."""
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    logged_in_user_id, target_user_id = parse_composite_user_id(user_id)
    requester = logged_in_user_id or target_user_id
    try:
        shared = get_user_shared_resources(requester, "policy")
        return (
            jsonify({"user_id": requester, "shared_policies": list(shared.values())}),
            200,
        )
    except Exception as e:
        logger.error("list_shared_policies error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# ── Legacy migration admin endpoint ──────────────────────────────────────────


@policy_hub_bp.route("/admin/migrate", methods=["POST"])
def admin_migrate_policies():
    """Queue legacy policy migration for an org.

    Gated to FRAMEWORK_OWNER. Supports:
      ?dry_run=true   — count only, no writes
      ?policy_id=X    — target a single policy (by policy_id, not full S3 key)
    """
    guard = _require_framework_owner()
    if guard is not None:
        return guard

    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id") or request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    dry_run = request.args.get("dry_run", "").lower() == "true"
    single_policy_id = request.args.get("policy_id") or body.get("policy_id")

    try:
        from utils.celery_base import migrate_legacy_policies_org

        task = migrate_legacy_policies_org.delay(
            user_id=user_id,
            dry_run=dry_run,
            policy_id=single_policy_id,
        )
        return jsonify({
            "status": "queued",
            "task_id": task.id,
            "dry_run": dry_run,
            "policy_id": single_policy_id,
        }), 202
    except Exception as exc:
        logger.error("admin_migrate_policies error: %s", exc)
        return jsonify({"error": str(exc)}), 500
