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
from policy_hub.templates import (
    get_template,
    get_default_template,
    serialize_section,
    deserialize_section,
    section_abbr_map,
    validate as validate_template,
)
from policy_hub.template_storage import (
    load_custom_template,
    save_custom_template,
    delete_custom_template,
    get_custom_template_metadata,
)
from policy_hub.structured import (
    parse_document_html,
    reconcile_statement_ids,
    sync_statements_to_lance,
)
from policy_hub.extract import extract_any
from policy_hub.titles import extract_title
from policy_hub.doc_ref import mint_doc_ref
from policy_hub.doc_types import (
    display_doc_ref,
    enforce_heading as doc_type_enforce_heading,
    enumeration_type_filter,
    statement_display_number,
    stmt_heading as doc_type_stmt_heading,
)
from policy_hub.workflow_autosubmit import auto_submit_policy
from workflow_route.state_machine import get_user_org_id
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
    TEMPLATE_REPLICATED,
    TEMPLATE_EDITED,
    TEMPLATE_RESET,
    TEMPLATE_APPLIED,
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

from utils.key_rotation_manager import SecureKMSService as _PhKMSService

S3_BUCKET = os.getenv("S3_BUCKET")
logger = get_logger(__name__)
policy_hub_bp = Blueprint("policy_hub", __name__, url_prefix="/policy-hub")

# ── Application-level encryption helpers ─────────────────────────────────────
_ph_kms = _PhKMSService()


def _enc_ph(user_id: str, v):
    if not v or not isinstance(v, str):
        return v
    return json.dumps(_ph_kms.encrypt(user_id, v))


def _dec_ph(user_id: str, v):
    if not v or not isinstance(v, str):
        return v
    try:
        d = json.loads(v)
        if isinstance(d, dict) and "ciphertext" in d:
            return _ph_kms.decrypt(user_id, d["encrypted_key"], d["iv"], d["ciphertext"])
    except Exception:
        pass
    return v


def _is_ph_enc(v) -> bool:
    try:
        d = json.loads(v)
        return isinstance(d, dict) and "ciphertext" in d
    except Exception:
        return False


def _encrypt_policy_fields(user_id: str, item: dict) -> dict:
    """Return a copy of item with title, content, sections encrypted."""
    enc = dict(item)
    if enc.get("title"):
        enc["title"] = _enc_ph(user_id, enc["title"])
    if enc.get("content"):
        enc["content"] = _enc_ph(user_id, enc["content"])
    if "sections" in enc:
        enc["sections"] = _enc_ph(user_id, json.dumps(enc["sections"]))
    return enc


def _decrypt_policy_fields(user_id: str, item: dict) -> tuple:
    """Decrypt title, content, sections in-place. Returns (item, was_migrated)."""
    was_migrated = False
    for field in ("title", "content"):
        raw = item.get(field, "")
        if raw and not _is_ph_enc(raw):
            was_migrated = True
        item[field] = _dec_ph(user_id, raw)
    raw_sec = item.get("sections")
    if raw_sec is not None:
        if isinstance(raw_sec, str):
            if not _is_ph_enc(raw_sec):
                was_migrated = True
            dec = _dec_ph(user_id, raw_sec)
            try:
                item["sections"] = json.loads(dec)
            except Exception:
                item["sections"] = []
    return item, was_migrated


def _write_policy_yaml(user_id: str, key: str, item: dict):
    _write_yaml_to_s3(key, _encrypt_policy_fields(user_id, item))
    # Write-through the lightweight metadata index so /list stays a single
    # indexed query. Best-effort: an index failure must never block the
    # authoritative S3 write — the nightly reconcile heals any gap.
    try:
        from policy_hub.doc_index import upsert_document
        upsert_document(user_id, item)
    except Exception as idx_exc:
        logger.warning("doc_index upsert failed for key=%s: %s", key, idx_exc)


def _read_policy_yaml(user_id: str, key: str, strict: bool = False) -> dict | None:
    data = load_yaml_from_s3(key, strict=strict)
    if not data:
        return data
    data, migrated = _decrypt_policy_fields(user_id, data)
    if migrated:
        try:
            _write_policy_yaml(user_id, key, data)
        except Exception as _e:
            logger.warning("Policy lazy-migration re-save failed for %s: %s", key, _e)
    return data


def _encrypt_framework_fields(record: dict) -> dict:
    enc = dict(record)
    if enc.get("name"):
        enc["name"] = _enc_ph(FRAMEWORK_OWNER, enc["name"])
    if "rows" in enc:
        enc["rows"] = _enc_ph(FRAMEWORK_OWNER, json.dumps(enc["rows"]))
    return enc


def _decrypt_framework_fields(record: dict) -> tuple:
    was_migrated = False
    raw_name = record.get("name", "")
    if raw_name and not _is_ph_enc(raw_name):
        was_migrated = True
    record["name"] = _dec_ph(FRAMEWORK_OWNER, raw_name)
    raw_rows = record.get("rows")
    if raw_rows is not None:
        if isinstance(raw_rows, str):
            if not _is_ph_enc(raw_rows):
                was_migrated = True
            dec = _dec_ph(FRAMEWORK_OWNER, raw_rows)
            try:
                record["rows"] = json.loads(dec)
            except Exception:
                record["rows"] = []
    return record, was_migrated


def _write_framework_yaml(key: str, record: dict):
    _write_yaml_to_s3(key, _encrypt_framework_fields(record))


def _read_framework_yaml(key: str) -> dict | None:
    data = load_yaml_from_s3(key)
    if not data:
        return data
    data, migrated = _decrypt_framework_fields(data)
    if migrated:
        try:
            _write_framework_yaml(key, data)
        except Exception as _e:
            logger.warning("Framework lazy-migration re-save failed for %s: %s", key, _e)
    return data

_jobs_lock = threading.Lock()


# ── Share access helper ──────────────────────────────────────────────────────


def _resolve_workflow_owner(policy_id: str, user_id: str) -> str | None:
    """Return the document owner if ``user_id`` is a workflow-assigned reviewer/
    approver for ``policy_id`` (any supported doc type), else None.

    A doc "shared for review" goes through the workflow (document_workflow), not
    the shared_users table — the assigned reviewer never owns a copy and sends a
    plain user_id. We resolve the real owner the same way /policy-hub/list does,
    so any doc visible in the reviewer's list is also openable here.
    """
    try:
        from policy_hub.workflow_autosubmit import WORKFLOW_SUPPORTED_DOC_TYPES
        from workflow_route.state_machine import get_docs_assigned_to_user

        for dt in WORKFLOW_SUPPORTED_DOC_TYPES:
            for assignment in get_docs_assigned_to_user(dt, user_id, include_published=True):
                if assignment.get("doc_id") == policy_id and assignment.get("owner_user_id"):
                    return assignment["owner_user_id"]
    except Exception as exc:
        logger.warning(
            "workflow owner resolution failed for policy=%s user=%s: %s",
            policy_id, user_id, exc,
        )
    return None


def _check_policy_share_access(baseuser, policy_id):
    """Resolve owner and ensure the requester has access. Returns (owner_id, err_tuple).

    Grants access to (a) the document owner, (b) explicit shared_users recipients
    (composite user_id), and (c) workflow-assigned reviewers/approvers — who send
    a plain user_id and own no copy, so the real owner is resolved from the active
    workflow row.
    """
    logged_in_user_id, owner_id = parse_composite_user_id(baseuser)
    if not owner_id:
        return None, (jsonify({"error": "Invalid user_id"}), 400)
    if not logged_in_user_id or logged_in_user_id == owner_id:
        # Plain caller: usually the owner, but may be a reviewer/approver for a
        # doc owned by someone else — resolve the real owner from the workflow.
        wf_owner = _resolve_workflow_owner(policy_id, owner_id)
        if wf_owner and wf_owner != owner_id:
            return wf_owner, None
        return owner_id, None
    access = get_user_resource_access("policy", owner_id, policy_id, logged_in_user_id)
    if access.get("granted"):
        return owner_id, None
    # Not an explicit share — fall back to a workflow assignment for the caller.
    wf_owner = _resolve_workflow_owner(policy_id, logged_in_user_id)
    if wf_owner:
        return wf_owner, None
    return None, (
        jsonify({"error": "Access to this policy has not been granted"}),
        403,
    )


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


# Title extraction lives in policy_hub/titles.py (stdlib-only, unit-testable).
_extract_title = extract_title


def _safe_mint_doc_ref(user_id: str, doc_type: str, title: str) -> str | None:
    """Mint a doc_ref, swallowing any failure so document creation never breaks.

    Returns the minted ref (e.g. ``ACC-0001``) or ``None`` when the org can't
    be resolved or minting fails — such documents are picked up later by
    ``policy_hub.backfill_doc_refs``.
    """
    try:
        org_id = get_user_org_id(user_id)
        if not org_id:
            logger.info("doc_ref: skipping mint for user=%s — no resolvable org", user_id)
            return None
        return mint_doc_ref(org_id, doc_type, title)
    except Exception as exc:
        logger.warning("doc_ref: mint failed for user=%s type=%s: %s", user_id, doc_type, exc)
        return None


def _attach_display_numbers(item: dict, doc_type: str, user_id: str | None = None) -> None:
    """Annotate each parsed statement in ``item['sections']`` with a
    ``display_number`` (e.g. ``ACC-001-003``) derived from the doc_ref.
    No-op when the doc has no doc_ref or sections.
    """
    doc_ref = item.get("doc_ref")
    sections = item.get("sections")
    if not sections:
        return
    abbr_map = section_abbr_map(doc_type, user_id=user_id)
    for sec in sections:
        sec_id = sec.get("id")
        abbr = abbr_map.get(sec_id, "")
        for stmt in sec.get("statements", []) or []:
            stmt["display_number"] = statement_display_number(
                doc_ref, abbr, stmt.get("seq", 0)
            )


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
        "List ALL compliance documents (policies, procedures, and standards) that must be "
        "created for full compliance.\n"
        f"{type_filter}\n\n"
        "Return ONLY a valid JSON array — no other text — where each element has:\n"
        '  "title": document title (e.g., "Access Control Policy")\n'
        '  "type": "policy", "procedure", or "standard"\n'
        '  "description": one sentence on the document\'s purpose\n\n'
        "JSON array:"
    )


def _v2_section_requirements(doc_type: str, user_id: str | None = None) -> str:
    """Build the ordered section list injected into the V2 generation prompt."""
    try:
        template = get_template(doc_type, user_id=user_id)
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
    user_id: str | None = None,
) -> str:
    stmt_heading = doc_type_stmt_heading(doc_type)
    enforce_heading = doc_type_enforce_heading(doc_type)

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
                + _v2_section_requirements(doc_type, user_id=user_id)
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


def _fallback_sections_from_html(html: str, doc_type: str, user_id: str | None = None) -> list[dict]:
    """Heading-bucketing fallback when structured section extraction fails.

    Builds an empty sections list from the template, then walks the source HTML
    and buckets each <h1>/<h2>/<h3>-led chunk into the closest matching template
    section (by keyword overlap on the heading text). Unmatched content lands in
    the first text-kind section. Guarantees a non-empty sections[] for any doc_type.
    """
    from bs4 import BeautifulSoup

    try:
        template_defs = get_template(doc_type, user_id=user_id)
    except KeyError:
        template_defs = get_template("policy", user_id=user_id)

    sections_by_id: dict[str, dict] = {}
    for sd in template_defs:
        sec: dict = {"id": sd.id, "title": sd.title, "kind": sd.kind, "body_html": ""}
        if sd.kind in ("statements", "steps"):
            sec["statements"] = []
        sections_by_id[sd.id] = sec

    if not html or not html.strip():
        return list(sections_by_id.values())

    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup

    chunks: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_buf: list[str] = []
    for elem in body.find_all(recursive=False):
        name = (elem.name or "").lower()
        if name in ("h1", "h2", "h3"):
            if current_heading is not None or current_buf:
                chunks.append((current_heading or "", "".join(current_buf)))
            current_heading = elem.get_text(strip=True)
            current_buf = []
        else:
            current_buf.append(str(elem))
    if current_heading is not None or current_buf:
        chunks.append((current_heading or "", "".join(current_buf)))

    def _match_section(heading_text: str) -> str | None:
        ht = heading_text.lower()
        if not ht:
            return None
        heading_words = set(ht.split())
        for sd in template_defs:
            title_words = set(sd.title.lower().split())
            if title_words & heading_words:
                return sd.id
        return None

    orphans: list[str] = []
    for heading, chunk_html in chunks:
        sec_id = _match_section(heading)
        if sec_id and sec_id in sections_by_id:
            piece = f"<h3>{heading}</h3>\n{chunk_html}" if heading else chunk_html
            sections_by_id[sec_id]["body_html"] += piece
        else:
            if heading:
                orphans.append(f"<h3>{heading}</h3>\n{chunk_html}")
            elif chunk_html.strip():
                orphans.append(chunk_html)

    if orphans:
        first_text_id = next(
            (sd.id for sd in template_defs if sd.kind == "text"),
            template_defs[0].id if template_defs else None,
        )
        if first_text_id:
            sections_by_id[first_text_id]["body_html"] += "\n".join(orphans)

    return list(sections_by_id.values())


def _enrich_v2(item: dict, content: str, doc_type: str, loop: asyncio.AbstractEventLoop, user_id: str | None = None) -> dict:
    """Parse HTML into structured sections, validate, and add V2 fields to *item*.

    Mutates and returns *item*. Always guarantees a non-empty sections[] — on
    parse failure or empty result, falls back to heading-bucketing via
    _fallback_sections_from_html so every document has section-divided storage.
    """
    sections_data: list[dict] = []
    metadata: dict = {}
    validation_ok = True

    try:
        parsed = parse_document_html(content, doc_type)
        validation = validate_template(content, doc_type, user_id=user_id)
        validation_ok = validation.ok

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
        metadata = parsed.metadata

        if not validation.ok:
            logger.warning(
                "Template validation needs_review for policy=%s: missing=%s",
                item.get("policy_id"),
                validation.missing_sections,
            )
    except Exception as exc:
        logger.warning(
            "_enrich_v2 structured parse failed for policy=%s: %s — using heading-bucketing fallback",
            item.get("policy_id"),
            exc,
        )

    if not sections_data:
        logger.info(
            "_enrich_v2: falling back to heading-bucketing for policy=%s",
            item.get("policy_id"),
        )
        sections_data = _fallback_sections_from_html(content, doc_type, user_id=user_id)
        validation_ok = False

    # The "Review and Revision History" section is authoritative on the
    # structured ``revision_history`` list (populated on each publish). Render
    # its body straight from that list so the row shows regardless of whether the
    # stored content HTML carried the proper data-section-id wrapper. Only
    # override when we actually have structured entries, so a manually-authored
    # history body is preserved when no structured rows exist yet.
    try:
        from policy_hub.review_lifecycle import (
            render_history_rows_html,
            _HISTORY_SUFFIX,
        )

        history_html = render_history_rows_html(item.get("revision_history"))
        if history_html:
            for sec in sections_data:
                if str(sec.get("id", "")).endswith(_HISTORY_SUFFIX):
                    sec["body_html"] = history_html
                    break
    except Exception as exc:
        logger.warning(
            "_enrich_v2 history render failed for policy=%s: %s",
            item.get("policy_id"), exc,
        )

    item["template_version"] = 1
    item["validation_status"] = "ok" if validation_ok else "needs_review"
    item["migration_status"] = "ok"
    item["sections"] = sections_data
    item["metadata"] = metadata
    return item


# ── AI gap-fill: complete empty template sections ────────────────────────────


def _empty_required_section_ids(
    item: dict, doc_type: str, user_id: str | None = None
) -> list[str]:
    """Return ids of required template sections that are missing or empty in *item*.

    A prose ("text") section counts as empty when its body_html has no visible
    text; a "statements"/"steps" section when it carries no statements. The
    ``header_table`` and ``history`` kinds are excluded — the metadata table and
    the Review & Revision History are populated by other paths (the header at
    creation time, history by the workflow publish/milestone hooks), not by the
    content-authoring gap-fill pass, so their emptiness must not be treated as an
    authoring gap.
    """
    try:
        template = get_template(doc_type, user_id=user_id)
    except KeyError:
        return []
    by_id = {s.get("id"): s for s in item.get("sections", []) or []}
    missing: list[str] = []
    for sd in template:
        if not sd.required or sd.kind in ("header_table", "history"):
            continue
        sec = by_id.get(sd.id)
        if sec is None:
            missing.append(sd.id)
            continue
        if sd.kind in ("statements", "steps"):
            if not (sec.get("statements") or []):
                missing.append(sd.id)
        elif not _strip_html_to_text(sec.get("body_html", "") or "").strip():
            missing.append(sd.id)
    return missing


def _recompute_validation_status(
    item: dict, doc_type: str, user_id: str | None = None
) -> None:
    """Set ``validation_status`` from authored-section completeness.

    Once every required prose/statement section is filled the "sections may be
    missing or incomplete" banner should clear — even if the header/history
    sections are still being populated by their own (non-authoring) code paths.
    """
    remaining = _empty_required_section_ids(item, doc_type, user_id=user_id)
    item["validation_status"] = "ok" if not remaining else "needs_review"


def _document_context_text(item: dict, max_chars: int = 6000) -> str:
    """Build grounding context from the document's already-present sections.

    Concatenates the title and each non-empty section's title + stripped prose +
    statement text so the gap-fill model stays consistent with the existing
    document instead of inventing unrelated content.
    """
    parts: list[str] = []
    title = item.get("title") or (item.get("metadata") or {}).get("title")
    if title:
        parts.append(f"DOCUMENT TITLE: {title}")
    for sec in item.get("sections", []) or []:
        body = _strip_html_to_text(sec.get("body_html", "") or "", max_chars=1500)
        stmts = [s.get("text", "") for s in (sec.get("statements") or []) if s.get("text")]
        chunk = " ".join(p for p in (body, " ".join(stmts)) if p).strip()
        if chunk:
            parts.append(f"## {sec.get('title', '')}\n{chunk}")
    return "\n\n".join(parts)[:max_chars]


def _gap_fill_prompt(
    item: dict,
    doc_type: str,
    missing_ids: list[str],
    fw_list: str,
    user_id: str | None = None,
) -> str:
    """Prompt the model to author ONLY the empty sections, grounded in the doc."""
    template = {s.id: s for s in get_template(doc_type, user_id=user_id)}
    targets: list[str] = []
    for sid in missing_ids:
        sd = template.get(sid)
        if not sd:
            continue
        shape = (
            '{"statements": [{"text": "..."}]}'
            if sd.kind in ("statements", "steps")
            else '{"body_html": "<p>...</p>"}'
        )
        targets.append(
            f"  - id={sid}  title={sd.title!r}  kind={sd.kind}  shape={shape}  hint={sd.prompt_help!r}"
        )
    context = _document_context_text(item)
    return (
        f"You are a senior compliance writer completing an existing {doc_type} document "
        f"for an organization that must comply with: {fw_list}.\n\n"
        "Several template sections are missing or empty. Write ONLY those sections, "
        "drawing strictly on the substance of the document below — stay consistent with "
        "its scope, terminology, named roles, and frameworks. Do NOT contradict or "
        "duplicate existing sections, and do NOT invent facts that conflict with the "
        "document. For statements/steps sections, write specific, normative items "
        "('The organization shall …', 'All employees must …').\n\n"
        "EXISTING DOCUMENT CONTENT:\n"
        f"{context or '(no other content available — infer from the title and frameworks)'}\n\n"
        "SECTIONS TO WRITE (output each by id, using the given shape):\n"
        f"{chr(10).join(targets)}\n\n"
        "Return ONLY valid JSON mapping each section id to its content object, e.g.:\n"
        '{\n  "<section_id>": {"body_html": "<p>…</p>"},\n'
        '  "<section_id>": {"statements": [{"text": "The organization shall …"}]}\n}\n'
        "No markdown, no code fences, no commentary."
    )


def _fill_missing_sections(
    item: dict,
    doc_type: str,
    missing_ids: list[str],
    fw_list: str,
    loop: asyncio.AbstractEventLoop,
    user_id: str | None = None,
    mark_ai: bool = False,
) -> bool:
    """Fill empty required sections with one grounded LLM pass.

    Merges the generated content into ``item['sections']`` (creating section dicts
    for any that were absent, re-sorted into template order), mints UUIDs for new
    statements, optionally marks filled sections ``ai_generated`` so the frontend
    can highlight AI-authored content, and recomputes ``validation_status``.
    Best-effort: any failure is logged and leaves *item* unchanged. Returns True
    if at least one section was filled.
    """
    if not missing_ids:
        return False
    try:
        template = get_template(doc_type, user_id=user_id)
    except KeyError:
        return False
    template_index = {s.id: s for s in template}

    try:
        raw = loop.run_until_complete(
            get_fireworks_response2(
                user_id=user_id or "gap-fill",
                user_message=_gap_fill_prompt(
                    item, doc_type, missing_ids, fw_list, user_id=user_id
                ),
                role="user",
                credits=None,
                temp=0.1,
            )
        )
    except Exception as exc:
        logger.warning(
            "gap-fill LLM call failed for policy=%s: %s", item.get("policy_id"), exc
        )
        return False
    if not raw or raw == "INSUFFICIENT":
        return False

    data = _parse_llm_json(raw)
    if not isinstance(data, dict):
        logger.warning(
            "gap-fill returned unparseable JSON for policy=%s", item.get("policy_id")
        )
        return False

    sections = item.get("sections") or []
    by_id = {s.get("id"): s for s in sections}
    filled = False

    for sid in missing_ids:
        payload = data.get(sid)
        sd = template_index.get(sid)
        if not isinstance(payload, dict) or sd is None:
            continue

        sec = by_id.get(sid)
        if sec is None:
            sec = {"id": sid, "title": sd.title, "kind": sd.kind, "body_html": ""}
            sections.append(sec)
            by_id[sid] = sec

        if sd.kind in ("statements", "steps"):
            new_stmts = []
            for seq, st in enumerate(payload.get("statements") or [], start=1):
                text = (st.get("text") if isinstance(st, dict) else str(st)) or ""
                text = text.strip()
                if text:
                    new_stmts.append(
                        {
                            "id": str(uuid.uuid4()),
                            "text": text,
                            "seq": seq,
                            "section_id": sid,
                            "status": "active",
                        }
                    )
            if not new_stmts:
                continue
            sec["statements"] = new_stmts
            sec["body_html"] = ""
        else:
            body = (payload.get("body_html") or "").strip()
            if not body:
                continue
            sec["body_html"] = body

        if mark_ai:
            sec["ai_generated"] = True
        filled = True

    if not filled:
        return False

    # Keep template order so merged-in sections render in the right place.
    order = {s.id: i for i, s in enumerate(template)}
    sections.sort(key=lambda s: order.get(s.get("id"), 999))
    item["sections"] = sections
    _recompute_validation_status(item, doc_type, user_id=user_id)
    return True


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
                user_id=user_id,
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
                            v2=v2, user_id=user_id,
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
                resolved_title = _extract_title(content, fallback=title, doc_type=d_type)
                item = {
                    "policy_id": policy_id,
                    "title": resolved_title,
                    "doc_ref": _safe_mint_doc_ref(user_id, d_type, resolved_title),
                    "type": d_type,
                    "frameworks": frameworks,
                    "content": content,
                    "s3_key": key,
                    "created_at": created_at,
                    "etag": str(uuid.uuid4()),
                }

                # Always enrich sections so every doc has section-divided
                # storage matching the section-divided UI. _enrich_v2 has an
                # internal heading-bucketing fallback if structured parsing fails.
                item = _enrich_v2(item, content, d_type, loop, user_id=user_id)

                # Completeness pass: if the model left any required prose/statement
                # section empty, fill it from the rest of the document so every
                # generated doc covers the full template. mark_ai stays False — the
                # whole document is AI-authored, so per-section provenance is moot.
                missing = _empty_required_section_ids(item, d_type, user_id=user_id)
                if missing:
                    _fill_missing_sections(
                        item, d_type, missing, fw_list, loop, user_id=user_id, mark_ai=False
                    )
                # Recompute regardless: clears the validation banner once every
                # authored section is present, even when no gap-fill was needed.
                _recompute_validation_status(item, d_type, user_id=user_id)
                _attach_display_numbers(item, d_type, user_id=user_id)

                _write_policy_yaml(user_id, key, item)

                try:
                    auto_submit_policy(policy_id, d_type, user_id)
                except Exception as wf_exc:
                    logger.warning(
                        "auto_submit_policy failed for generated policy=%s: %s",
                        policy_id, wf_exc,
                    )

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


def _upload_extraction_prompt(html: str, doc_type: str, filename: str, user_id: str | None = None) -> str:
    """Build the Fireworks prompt that maps uploaded HTML to V2 structured sections."""
    try:
        template = get_template(doc_type, user_id=user_id)
        sections_desc = "\n".join(
            f"  - id={s.id}  title={s.title!r}  kind={s.kind}  required={s.required}  hint={s.prompt_help!r}"
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
    fill_missing: bool = True,
):
    """Background worker. For each uploaded file: extract → classify → write YAML → LLM-map → index.

    When ``fill_missing`` is set, any required template section the uploaded file
    did not cover is authored by a grounded gap-fill LLM pass and marked
    ``ai_generated`` so the frontend can distinguish it from the user's content.
    """
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
                    _extract_title(html, fallback=fallback_title, doc_type=doc_type)
                    if not extraction_failed
                    else fallback_title
                )
                item = {
                    "policy_id": policy_id,
                    "title": title,
                    "doc_ref": _safe_mint_doc_ref(user_id, doc_type, title),
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
                    _write_policy_yaml(user_id, key, item)
                except Exception as exc:
                    logger.error(
                        "Baseline YAML write failed for %s: %s", filename, exc
                    )

                if not extraction_failed:
                    # 4. LLM-map to V2 structured sections.
                    # The v2 flag gates the *LLM* path (it costs credits). When
                    # the flag is off, we still produce sections via _enrich_v2's
                    # heading-bucketing fallback so storage always matches UI.
                    if v2:
                        prompt = _upload_extraction_prompt(html, doc_type, filename, user_id=user_id)
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
                                vr = validate_template(sections_html, doc_type, user_id=user_id)
                                item["validation_status"] = "ok" if vr.ok else "needs_review"
                                item["migration_status"] = "ok"
                            else:
                                # Fall back to legacy enrichment so the doc still has some structure
                                logger.warning(
                                    "LLM mapping returned unparseable JSON for %s — falling back to _enrich_v2",
                                    filename,
                                )
                                _enrich_v2(item, html, doc_type, loop, user_id=user_id)
                                item.setdefault("migration_status", "needs_review")
                    else:
                        # v2 disabled — still produce sections via heading-bucketing
                        # so every uploaded doc has structured sections in storage.
                        _enrich_v2(item, html, doc_type, loop, user_id=user_id)

                    # Gap-fill: author any template section the upload didn't cover,
                    # grounded in the document's own content, and mark it ai_generated
                    # so the frontend highlights AI-authored vs uploaded sections.
                    # Gated on v2 (the fill is itself a credit-costing LLM call).
                    if v2 and fill_missing:
                        missing = _empty_required_section_ids(item, doc_type, user_id=user_id)
                        if missing:
                            fw_text = ", ".join(frameworks) if frameworks else "general compliance"
                            _fill_missing_sections(
                                item, doc_type, missing, fw_text, loop,
                                user_id=user_id, mark_ai=True,
                            )

                    _attach_display_numbers(item, doc_type, user_id=user_id)

                    # 4a. Re-write the enriched YAML so /list returns the
                    # final structured version.
                    try:
                        _write_policy_yaml(user_id, key, item)
                    except Exception as exc:
                        logger.error(
                            "Enriched YAML write failed for %s: %s", filename, exc
                        )

                    try:
                        auto_submit_policy(policy_id, doc_type, user_id)
                    except Exception as wf_exc:
                        logger.warning(
                            "auto_submit_policy failed for uploaded policy=%s: %s",
                            policy_id, wf_exc,
                        )

                    # 5. Sync statements to LanceDB (only when V2 is enabled, since
                    # indexing costs an embedding call per statement)
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
            meta = _read_framework_yaml(_fw_key(fw_id))
            if meta and meta.get("name"):
                uploaded_fw_names.append(meta["name"])
        except Exception:
            pass

    all_frameworks = (
        frameworks + uploaded_fw_names
    )  # static/custom + S3-resolved uploaded
    fw_list = ", ".join(all_frameworks) if all_frameworks else "general compliance"
    # Enumerate per the requested tab; unspecified / "all" covers the full
    # triad (policies, procedures, standards).
    type_filter = enumeration_type_filter(doc_type)

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
      fill_missing (optional) — "false" to disable AI gap-fill of empty template
                                sections (default on)
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

    # Default on: uploaded docs come out template-complete unless the caller
    # explicitly opts out (frontend checkbox sends "false").
    fill_missing = (request.form.get("fill_missing") or "true").strip().lower() != "false"

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
        args=(user_id, job_id, files_payload, frameworks, remote_addr, actor_email, fill_missing),
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

    data = _read_policy_yaml(user_id, _s3_key(user_id, policy_id))
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
        existing = _read_policy_yaml(user_id, key)
        if existing:
            existing["content"] = updated_content
            existing["updated_at"] = datetime.now(timezone.utc).isoformat()
            existing["etag"] = str(uuid.uuid4())

            if v2:
                # Reconcile statement IDs and rebuild structured sections
                doc_type = existing.get("type", "policy")
                threshold = statement_reid_threshold(user_id)
                existing = _reconcile_and_enrich_edit(
                    existing, updated_content, doc_type, threshold, loop, user_id=user_id
                )

            try:
                _write_policy_yaml(user_id, key, existing)
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
    user_id: str | None = None,
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
        validation = validate_template(updated_content, doc_type, user_id=user_id)
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
        # doc_ref is immutable across edits; recompute statement numbers since
        # the seq/section layout may have changed during reconciliation.
        _attach_display_numbers(existing, doc_type, user_id=user_id)

        # Sync to LanceDB
        try:
            loop.run_until_complete(
                sync_statements_to_lance(
                    policy_id=policy_id,
                    doc_type=doc_type,
                    version=version,
                    statements=all_active,
                    superseded=all_superseded,
                    user_id=user_id,
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

# Fast-path flag: serve /list from the RDS metadata index (policy_hub_documents)
# instead of one S3 GET per document. Flip to False to fall back to the legacy
# S3 scan everywhere if the index ever misbehaves.
POLICY_INDEX_ENABLED = True

# Human labels for the workflow/approval states, aligned with
# workflow_route.state_machine.DEFAULT_STATES_JSON["states"]. Used to give each
# list card a ready-to-render approval status.
_WORKFLOW_STATE_LABELS = {
    "draft": "Draft",
    "quality_review": "Quality Review",
    "governance_review": "Governance Review",
    "approval": "Approval",
    "published": "Published",
}


@policy_hub_bp.route("/list", methods=["GET"])
@permission_required_body("policyhub.view")
def list_policies():
    raw_user_id = request.args.get("user_id")
    if not raw_user_id:
        return jsonify({"error": "user_id is required"}), 400
    logged_in_user_id, user_id = parse_composite_user_id(raw_user_id)

    # ── Owner's documents ──────────────────────────────────────────────────
    # Fast path: the RDS metadata index answers the list in one indexed query.
    # Fall back to the S3 scan (and lazily populate) when the index is empty
    # for this user.
    items: list[dict] = []
    used_index = False
    if POLICY_INDEX_ENABLED:
        try:
            from policy_hub.doc_index import list_documents
            owned = list_documents(user_id)
            if owned:
                items.extend(owned)
                used_index = True
        except Exception as idx_exc:
            logger.warning("doc_index list_documents failed, S3 fallback: %s", idx_exc)
    if not used_index:
        from policy_hub.doc_index import scan_policies_from_s3
        # Pass _read_policy_yaml so the helper decrypts using the local
        # routes-module helpers without re-entering an import cycle.
        items.extend(scan_policies_from_s3(user_id, read_fn=_read_policy_yaml))

    seen_policy_ids = {it.get("policy_id") for it in items if it.get("policy_id")}

    # ── Collect foreign-owned documents to union in ────────────────────────
    # Shared TO this user (resolved owner — handles composite/impersonation).
    try:
        shared_index = get_user_shared_resources(user_id, "policy") or {}
    except Exception:
        shared_index = {}
    shared_targets: list[tuple[str, str]] = []  # (policy_id, owner_id)
    for policy_id, entry in shared_index.items():
        owner_id = entry.get("mainuser_id")
        if not owner_id or owner_id == user_id or policy_id in seen_policy_ids:
            continue
        shared_targets.append((policy_id, owner_id))

    # Workflow-assigned (QR / GR / Approver) but neither owner nor shared —
    # otherwise an assigned reviewer would only see it via /workflow/inbox.
    assigned_targets: list[tuple[str, str, str]] = []  # (policy_id, owner_id, role)
    shared_ids = {t[0] for t in shared_targets}
    try:
        from policy_hub.workflow_autosubmit import WORKFLOW_SUPPORTED_DOC_TYPES
        from workflow_route.state_machine import get_docs_assigned_to_user

        for wf_doc_type in WORKFLOW_SUPPORTED_DOC_TYPES:
            for assignment in get_docs_assigned_to_user(wf_doc_type, user_id):
                pid = assignment.get("doc_id")
                owner_id = assignment.get("owner_user_id")
                if not pid or not owner_id or owner_id == user_id:
                    continue
                if pid in seen_policy_ids or pid in shared_ids:
                    continue
                assigned_targets.append((pid, owner_id, assignment.get("role")))
    except Exception as wf_assign_exc:
        logger.warning("policy assigned-for-review union failed: %s", wf_assign_exc)

    # ── Resolve foreign documents: index first (one batched query), S3 only
    # for ids the index hasn't caught up on yet (then lazily populate). ─────
    foreign_ids = [t[0] for t in shared_targets] + [t[0] for t in assigned_targets]
    index_hits: dict[str, dict] = {}
    if foreign_ids:
        try:
            from policy_hub.doc_index import get_documents
            index_hits = get_documents(foreign_ids)
        except Exception as gd_exc:
            logger.warning("doc_index get_documents failed: %s", gd_exc)

    def _resolve_foreign(pid: str, owner_id: str) -> dict | None:
        hit = index_hits.get(pid)
        if hit:
            return dict(hit)
        full = _read_policy_yaml(owner_id, _s3_key(owner_id, pid))
        if not full:
            return None
        try:
            from policy_hub.doc_index import upsert_document
            upsert_document(owner_id, full)
        except Exception:
            pass
        return full

    for pid, owner_id in shared_targets:
        it = _resolve_foreign(pid, owner_id)
        if not it:
            continue
        items.append({**it, "owner_user_id": owner_id, "shared": True})
        seen_policy_ids.add(pid)

    for pid, owner_id, role in assigned_targets:
        if pid in seen_policy_ids:
            continue
        it = _resolve_foreign(pid, owner_id)
        if not it:
            continue
        items.append({
            **it,
            "owner_user_id": owner_id,
            "assigned_for_review": True,
            "assigned_role": role,
        })
        seen_policy_ids.add(pid)

    try:
        from policy_hub.workflow_autosubmit import WORKFLOW_SUPPORTED_DOC_TYPES
        from workflow_route.state_machine import get_workflow_states_for_docs

        # /list returns mixed types (policy / procedure / standard). The workflow
        # state machine keys by (doc_type, doc_id), so group ids by type and
        # query each supported doc_type once. All three Policy Hub doc types are
        # workflow-supported (see WORKFLOW_SUPPORTED_DOC_TYPES).
        ids_by_type: dict[str, list[str]] = {}
        for it in items:
            t = it.get("type")
            pid = it.get("policy_id")
            if t in WORKFLOW_SUPPORTED_DOC_TYPES and pid:
                ids_by_type.setdefault(t, []).append(pid)

        states_by_id: dict[str, str] = {}
        for t, ids in ids_by_type.items():
            states_by_id.update(get_workflow_states_for_docs(t, ids))

        for it in items:
            it["workflow_state"] = states_by_id.get(it.get("policy_id"))
    except Exception as wf_exc:
        logger.warning("policy workflow_state lookup failed: %s", wf_exc)

    # Surface a card-ready approval status on every item (policy / procedure /
    # standard). A document with no workflow row yet is effectively still a
    # draft, so the status defaults to "draft" rather than null — the second-pane
    # cards always have something to render. This pass always runs, even if the
    # workflow lookup above failed.
    for it in items:
        state = it.get("workflow_state") or "draft"
        it["approval_status"] = state
        it["approval_status_label"] = _WORKFLOW_STATE_LABELS.get(
            state, state.replace("_", " ").title()
        )
        it["is_published"] = state == "published"

    # Ensure every item's statements carry a display_number so the detail view
    # renders numbering without a second round-trip. Cheap: abbr maps for the
    # default templates are in-memory; only custom templates touch S3.
    try:
        abbr_cache: dict[str, dict[str, str]] = {}
        for it in items:
            if not it.get("sections"):
                continue
            dt = it.get("type", "policy")
            if dt not in abbr_cache:
                abbr_cache[dt] = section_abbr_map(dt)
            doc_ref = it.get("doc_ref")
            for sec in it["sections"]:
                abbr = abbr_cache[dt].get(sec.get("id"), "")
                for stmt in sec.get("statements", []) or []:
                    stmt["display_number"] = statement_display_number(
                        doc_ref, abbr, stmt.get("seq", 0)
                    )
    except Exception as dn_exc:
        logger.warning("display_number attach failed in /list: %s", dn_exc)

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
    existing = _read_policy_yaml(user_id, key)
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
                existing, existing["content"], doc_type, threshold, loop, user_id=user_id
            )
        finally:
            loop.close()

    try:
        _write_policy_yaml(user_id, key, existing)
    except Exception as e:
        logger.error("Failed to update policy in S3: %s", e)
        return jsonify({"error": "Failed to update policy"}), 500

    return jsonify({"status": "ok"}), 200


def _sync_history_section_body(item: dict) -> None:
    """Re-render the history section's body_html from the structured history.

    ``/list`` (and the frontend) read the stored ``sections`` directly — they do
    not re-render the Review & Revision History from ``revision_history`` — so the
    history section's body_html must be kept in step whenever the structured list
    changes. ``render_history_rows_html`` rebuilds the whole table from the rows,
    so this stays correct for per-stage milestones, not just the publish row.
    Best-effort: no-ops when there are no rows or no history section.
    """
    try:
        from policy_hub.review_lifecycle import render_history_rows_html, _HISTORY_SUFFIX

        html = render_history_rows_html(item.get("revision_history"))
        if not html:
            return
        for sec in item.get("sections", []) or []:
            if str(sec.get("id", "")).endswith(_HISTORY_SUFFIX):
                sec["body_html"] = html
                return
    except Exception as exc:
        logger.debug("_sync_history_section_body skipped for %s: %s", item.get("policy_id"), exc)


def append_revision_entries_to_policy(owner_id, policy_id, doc_type, entries, only_if_empty=False):
    """Append one or more Review & Revision History rows to a Policy Hub doc.

    Used by the workflow milestone recorder to log each review-stage transition
    into the document's ``revision_history`` (and keep the rendered HTML table in
    sync) as it happens — not just on publish. ``only_if_empty=True`` (used by the
    backfill) makes it idempotent and non-destructive: it writes only when no
    history exists yet. Best-effort: never raises so a history write can't roll
    back the (committed) workflow transition. Returns True if the document was
    updated.
    """
    if not entries:
        return False
    try:
        from policy_hub.review_lifecycle import (
            append_revision_entry,
            render_history_into_content,
        )

        key = _s3_key(owner_id, policy_id)
        item = _read_policy_yaml(owner_id, key)
        if not item:
            logger.warning(
                "append_revision_entries_to_policy: policy %s not found for owner %s",
                policy_id, owner_id,
            )
            return False

        if only_if_empty and (item.get("revision_history") or []):
            return False

        for entry in entries:
            append_revision_entry(item, entry)
            # Keep the rendered HTML history table in step (best-effort): the
            # structured revision_history above is the source of truth, so a
            # render hiccup must not drop the entry or abort the write.
            try:
                if item.get("content"):
                    item["content"] = render_history_into_content(
                        item["content"], doc_type, entry
                    )
            except Exception as render_exc:
                logger.debug(
                    "append_revision_entries_to_policy HTML sync skipped for %s: %s",
                    policy_id, render_exc,
                )

        # Rebuild the history section body from the full structured list so the
        # stored sections (read by /list and the frontend) show every row.
        _sync_history_section_body(item)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        item["etag"] = str(uuid.uuid4())
        _write_policy_yaml(owner_id, key, item)
        return True
    except Exception as exc:
        logger.error(
            "append_revision_entries_to_policy failed for %s %s: %s",
            doc_type, policy_id, exc,
        )
        return False


def apply_publication_to_policy(
    owner_id: str,
    policy_id: str,
    doc_type: str,
    doc_version: str,
    author_email: str,
    frequency: str,
    published_at=None,
    summary: str | None = None,
) -> bool:
    """Record an approval/publish on a Policy Hub document.

    Called by the workflow publish hook (and the backfill endpoint). Sets the
    review-cycle metadata (``next_review_date`` etc.) and appends a "Review and
    Revision History" entry. ``published_at`` defaults to now; pass the original
    publish timestamp when backfilling an already-published doc. Returns True if
    the document was updated. Best-effort: never raises so a publish transition
    is not rolled back by a Policy Hub I/O hiccup.
    """
    try:
        from policy_hub.review_lifecycle import record_publication

        key = _s3_key(owner_id, policy_id)
        item = _read_policy_yaml(owner_id, key)
        if not item:
            logger.warning(
                "apply_publication_to_policy: policy %s not found for owner %s",
                policy_id, owner_id,
            )
            return False

        # Idempotency guard: skip if this exact version was already recorded as
        # published (publish hooks can fire on retries / auto-advance replays,
        # and backfill must not double-write a row already present).
        history = item.get("revision_history") or []
        if history and history[-1].get("version") == str(doc_version) and \
                history[-1].get("action") == "published":
            return False

        record_publication(
            item,
            doc_type=doc_type,
            version=doc_version,
            author=author_email,
            frequency=frequency,
            published_at=published_at,
            summary=summary,
        )
        # Rebuild the history section body from the full structured list (covers
        # any per-stage rows added before this publish row).
        _sync_history_section_body(item)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        item["etag"] = str(uuid.uuid4())
        _write_policy_yaml(owner_id, key, item)
        logger.info(
            "recorded publication for %s %s (owner=%s, next_review=%s)",
            doc_type, policy_id, owner_id, item.get("next_review_date"),
        )
        return True
    except Exception as exc:
        logger.error(
            "apply_publication_to_policy failed for %s %s: %s",
            doc_type, policy_id, exc,
        )
        return False


def _replace_section_body(content: str, section_id: str, new_body_html: str, title: str) -> str | None:
    """Replace one section's body (keeping its <h2>) with ``new_body_html``.

    ``new_body_html`` is the section content *without* the heading — the same
    shape ``render_document_html`` emits as a section body. Returns the updated
    full-document HTML, or None if the section isn't present.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content or "", "lxml")
    section = soup.find(attrs={"data-section-id": section_id})
    if section is None:
        return None

    heading = section.find("h2")
    section.clear()
    if heading is not None:
        section.append(heading)
    else:
        h2 = soup.new_tag("h2")
        h2.string = title or ""
        section.append(h2)

    frag = BeautifulSoup(new_body_html or "", "lxml")
    container = frag.body if frag.body is not None else frag
    for node in list(container.children):
        section.append(node)

    root = soup.find("div", class_="policy-document")
    if root is not None:
        return str(root)
    if soup.body is not None:
        return soup.body.decode_contents()
    return str(soup)


def _section_statement_diff(item: dict, section_id: str, merged_content: str, doc_type: str) -> dict:
    """Diff a section's statements before/after a manual edit, by text equality."""
    parsed = parse_document_html(merged_content, doc_type)
    new_sec = next((s for s in parsed.sections if s.id == section_id), None)
    new_texts = [st.text for st in (new_sec.statements if new_sec else [])]
    old_sec = next(
        (s for s in item.get("sections", []) if s.get("id") == section_id), None
    )
    old_stmts = (old_sec or {}).get("statements", []) if old_sec else []
    old_by_text = {s["text"]: s["id"] for s in old_stmts}
    new_text_set = set(new_texts)
    return {
        "kept": [sid for txt, sid in old_by_text.items() if txt in new_text_set],
        "removed": [sid for txt, sid in old_by_text.items() if txt not in new_text_set],
        "added": [t for t in new_texts if t not in old_by_text],
        "old_count": len(old_stmts),
        "new_count": len(new_texts),
    }


@policy_hub_bp.route("/<policy_id>/block/preview", methods=["POST"])
@permission_required_body("policyhub.edit")
def preview_block_edit(policy_id: str):
    """Preview a manual single-section edit without persisting.

    Mirrors the report (/radar/changeblock) preview step, but the change is
    manual: the client supplies the section's new body HTML directly.

    Body: { user_id, section_id, new_html }
    Response: { policy_id, section_id, old_html, new_html, validation_status,
                statement_changes:{kept,removed,added,old_count,new_count} }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id")
    section_id = (body.get("section_id") or "").strip()
    new_html = body.get("new_html", "")

    if not baseuser or not policy_id or not section_id:
        return jsonify({"error": "user_id, policy_id, and section_id are required"}), 400
    user_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    item = _read_policy_yaml(user_id, _s3_key(user_id, policy_id))
    if not item:
        return jsonify({"error": "Policy not found"}), 404

    doc_type = item.get("type", "policy")
    old_sec = next(
        (s for s in item.get("sections", []) if s.get("id") == section_id), None
    )
    if old_sec is None:
        return jsonify({"error": f"Unknown section_id: {section_id}"}), 404

    merged = _replace_section_body(
        item.get("content", ""), section_id, new_html, old_sec.get("title", "")
    )
    if merged is None:
        return jsonify({"error": f"Section {section_id} not found in document"}), 404

    validation = validate_template(merged, doc_type, user_id=user_id)
    return jsonify({
        "policy_id": policy_id,
        "section_id": section_id,
        "old_html": old_sec.get("body_html", ""),
        "new_html": new_html,
        "validation_status": "ok" if validation.ok else "needs_review",
        "statement_changes": _section_statement_diff(item, section_id, merged, doc_type),
    }), 200


@policy_hub_bp.route("/<policy_id>/block/confirm", methods=["POST"])
@permission_required_body("policyhub.edit")
def confirm_block_edit(policy_id: str):
    """Persist a manual single-section edit.

    Replaces the section body, reconciles statement IDs across the whole
    document, re-syncs LanceDB, and writes the new YAML. Optimistic-locked via
    ``etag`` for V2 documents.

    Body: { user_id, section_id, new_html, etag? }
    Response: { status, policy_id, etag, section:{id,title,kind,body_html,statements?} }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id")
    section_id = (body.get("section_id") or "").strip()
    new_html = body.get("new_html", "")

    if not baseuser or not policy_id or not section_id:
        return jsonify({"error": "user_id, policy_id, and section_id are required"}), 400
    user_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    key = _s3_key(user_id, policy_id)
    item = _read_policy_yaml(user_id, key)
    if not item:
        return jsonify({"error": "Policy not found"}), 404

    v2 = policy_hub_v2_enabled(user_id)
    if v2 and "etag" in body and body["etag"] != item.get("etag"):
        return jsonify({
            "error": "Document was modified since you last loaded it. Please reload.",
            "current_etag": item.get("etag"),
        }), 409

    doc_type = item.get("type", "policy")
    old_sec = next(
        (s for s in item.get("sections", []) if s.get("id") == section_id), None
    )
    if old_sec is None:
        return jsonify({"error": f"Unknown section_id: {section_id}"}), 404

    merged = _replace_section_body(
        item.get("content", ""), section_id, new_html, old_sec.get("title", "")
    )
    if merged is None:
        return jsonify({"error": f"Section {section_id} not found in document"}), 404

    item["content"] = merged
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    item["etag"] = str(uuid.uuid4())

    if v2:
        threshold = statement_reid_threshold(user_id)
        loop = asyncio.new_event_loop()
        try:
            item = _reconcile_and_enrich_edit(
                item, merged, doc_type, threshold, loop, user_id=user_id
            )
        finally:
            loop.close()

    try:
        _write_policy_yaml(user_id, key, item)
    except Exception as e:
        logger.error("Failed to persist block edit for policy %s: %s", policy_id, e)
        return jsonify({"error": "Failed to update policy"}), 500

    updated_sec = next(
        (s for s in item.get("sections", []) if s.get("id") == section_id), old_sec
    )
    return jsonify({
        "status": "ok",
        "policy_id": policy_id,
        "etag": item["etag"],
        "section": updated_sec,
    }), 200


def _resolve_email(user_id: str) -> str | None:
    if not user_id:
        return None
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT email FROM users WHERE user_id=%s LIMIT 1", (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return row.get("email") if row else None


def _latest_workflow_for_doc(doc_type: str, doc_id: str) -> dict | None:
    """Return the most recent document_workflow row for a doc, any state.

    Covers in-flight reviews as well as published ones so the Review & Revision
    History can be reconstructed from the process steps before the document
    reaches 'published'.
    """
    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM document_workflow "
                "WHERE doc_type=%s AND doc_id=%s "
                "ORDER BY published_at DESC, created_at DESC LIMIT 1",
                (doc_type, doc_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _reconstruct_review_entries_from_events(workflow_id: str, doc_version: str) -> list[dict]:
    """Rebuild the per-stage Review & Revision History from workflow events.

    Reads every ``document_workflow_events`` row for the workflow in chronological
    order and maps each transition (submit / quality / governance / send-back) to a
    revision entry using the same descriptions the live recorder produces
    (``_milestone_for_hop``). The terminal ``published`` hop is intentionally
    excluded — apply_publication_to_policy appends that row with cadence metadata.
    Mirrors backfill_review_history.py::_reconstruct_entries_from_events.
    """
    if not workflow_id:
        return []
    from policy_hub.review_lifecycle import build_revision_entry
    from workflow_route.routes import _milestone_for_hop
    from workflow_route.state_machine import AUTO_ADVANCE_COMMENT

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT from_state, to_state, actor_user_id, comment, created_at "
                "FROM document_workflow_events WHERE workflow_id=%s "
                "ORDER BY created_at ASC, event_id ASC",
                (workflow_id,),
            )
            events = cur.fetchall() or []
    finally:
        conn.close()

    entries: list[dict] = []
    email_cache: dict = {}
    for ev in events:
        actor = ev.get("actor_user_id")
        if actor not in email_cache:
            email_cache[actor] = _resolve_email(actor) or ""
        is_auto = ev.get("comment") == AUTO_ADVANCE_COMMENT
        milestone = _milestone_for_hop({
            "from_state": ev.get("from_state"),
            "to_state": ev.get("to_state"),
            "comment": "" if is_auto else ev.get("comment"),
            "auto": is_auto,
        })
        if not milestone:
            continue
        action, summary = milestone
        entries.append(build_revision_entry(
            version=doc_version,
            author=email_cache[actor],
            summary=summary,
            action=action,
            published_at=ev.get("created_at"),
        ))
    return entries


@policy_hub_bp.route("/<policy_id>/backfill-history", methods=["POST"])
@permission_required_body("policyhub.edit")
def backfill_review_history(policy_id: str):
    """Reconstruct the Review & Revision History from a document's process steps.

    The live recorder only writes history for documents that flowed through the
    workflow *after* the per-stage recorder existed; legacy/auto-advanced docs show
    "No history recorded" despite completed steps. This endpoint rebuilds the full
    trail from ``document_workflow_events``:

      1. Reconstruct per-stage rows (submit / quality / governance / send-back) from
         the workflow's events — only when the document has no history yet, so it is
         non-destructive (``only_if_empty``).
      2. If the workflow has reached 'published', stamp the review-cycle metadata
         (next_review_date etc.) and append the terminal published row.

    Idempotent: re-runs are no-ops once history is present.

    Body: { user_id }
    Response: { status, updated, revision_history, next_review_date }
    """
    body = request.get_json(silent=True) or {}
    baseuser = body.get("user_id")
    if not baseuser or not policy_id:
        return jsonify({"error": "user_id and policy_id are required"}), 400
    user_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    item = _read_policy_yaml(user_id, _s3_key(user_id, policy_id))
    if not item:
        return jsonify({"error": "Policy not found"}), 404
    doc_type = item.get("type", "policy")

    wf = _latest_workflow_for_doc(doc_type, policy_id)
    if not wf:
        return jsonify({
            "error": "No review workflow found for this document; nothing to backfill."
        }), 400

    from workflow_route.state_machine import get_org_review_frequency

    doc_version = wf.get("doc_version") or item.get("metadata", {}).get("version", "1.0")

    # 1. Per-stage trail from the workflow events (only when history is empty).
    perstage = _reconstruct_review_entries_from_events(wf.get("workflow_id"), doc_version)
    appended = append_revision_entries_to_policy(
        user_id, policy_id, doc_type, perstage, only_if_empty=True
    )

    # 2. Terminal publish row + cadence metadata, only for published workflows.
    published = False
    if wf.get("state") == "published":
        published_at = wf.get("published_at") or wf.get("approved_at")
        approver_email = _resolve_email(wf.get("current_approver")) or ""
        frequency = get_org_review_frequency(get_user_org_id(user_id))
        published = apply_publication_to_policy(
            owner_id=user_id,
            policy_id=policy_id,
            doc_type=doc_type,
            doc_version=doc_version,
            author_email=approver_email,
            frequency=frequency,
            published_at=published_at,
        )

    item = _read_policy_yaml(user_id, _s3_key(user_id, policy_id)) or item
    return jsonify({
        "status": "ok",
        "updated": bool(appended or published),
        "revision_history": item.get("revision_history", []),
        "next_review_date": item.get("next_review_date"),
    }), 200


def _statements_from_item(item: dict) -> list[dict]:
    """Flatten a policy item's sections into a numbered statement list.

    Recomputes ``display_number`` on the fly so legacy docs (persisted before
    the numbering feature) and freshly-edited docs both render consistently.
    """
    doc_type = item.get("type", "policy")
    doc_ref = item.get("doc_ref")
    doc_ref_disp = display_doc_ref(doc_ref)
    abbr_map = section_abbr_map(doc_type)
    out: list[dict] = []
    for sec in item.get("sections", []) or []:
        sec_id = sec.get("id")
        abbr = abbr_map.get(sec_id, "")
        for stmt in sec.get("statements", []) or []:
            seq = stmt.get("seq", 0)
            out.append({
                "statement_id": stmt.get("id"),
                "policy_id": item.get("policy_id"),
                "doc_ref": doc_ref_disp,
                "doc_type": doc_type,
                "section_id": sec_id,
                "section_abbr": abbr,
                "seq": seq,
                "display_number": statement_display_number(doc_ref, abbr, seq),
                "text": stmt.get("text"),
                "status": stmt.get("status", "active"),
                "version": item.get("metadata", {}).get("version", "1.0"),
            })
    return out


@policy_hub_bp.route("/<policy_id>/statements", methods=["GET"])
@permission_required_body("policyhub.view")
def list_policy_statements(policy_id: str):
    """Return the numbered, individually-addressable statements of a document.

    Query: user_id. Each statement carries its ``display_number``
    (e.g. ``ACC-001-003``) so clients render a consistent scheme.
    """
    baseuser = request.args.get("user_id")
    if not baseuser or not policy_id:
        return jsonify({"error": "user_id is required"}), 400
    owner_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    # Read in strict mode so a genuinely-deleted policy (NoSuchKey -> None) is told
    # apart from a transient S3 failure (raises). A tracker can still reference a
    # policy that was later deleted; surface that clearly instead of erroring the
    # whole page, and keep transient blips honestly retryable.
    try:
        item = _read_policy_yaml(owner_id, _s3_key(owner_id, policy_id), strict=True)
    except Exception:
        logger.warning(
            "list_policy_statements: transient read failure for policy=%s",
            policy_id, exc_info=True,
        )
        return jsonify(
            {"error": "Service temporarily unavailable. Please try again later."}
        ), 503

    if not item:
        # The policy is gone (e.g. a tracker cell still references a deleted doc).
        # Return 200 with an explicit `deleted` flag so the client can tell the
        # user the policy was deleted, rather than firing a generic error toast.
        return jsonify({
            "policy_id": policy_id,
            "doc_ref": None,
            "title": None,
            "doc_type": None,
            "statements": [],
            "deleted": True,
            "message": "This policy has been deleted.",
        }), 200

    return jsonify({
        "policy_id": policy_id,
        "doc_ref": display_doc_ref(item.get("doc_ref")),
        "title": item.get("title"),
        "doc_type": item.get("type", "policy"),
        "statements": _statements_from_item(item),
    }), 200


def _user_policy_ids(user_id: str) -> dict[str, dict]:
    """Map ``policy_id -> {doc_ref, title, type}`` for the user's own documents.

    Reads YAMLs from the owner's S3 prefix. Used to scope cross-doc search to
    the caller's library and to enrich search hits with doc_ref/title.
    """
    out: dict[str, dict] = {}
    prefix = f"{user_id}/policies/"
    for obj in list_all_files(folder=prefix) or []:
        key = obj.get("Key", "")
        if not key.endswith(".yaml") or "/jobs/" in key or "/raw/" in key:
            continue
        data = load_yaml_from_s3(key)
        if data and data.get("policy_id"):
            out[data["policy_id"]] = {
                "doc_ref": data.get("doc_ref"),
                "title": data.get("title"),
                "type": data.get("type", "policy"),
            }
    return out


@policy_hub_bp.route("/search", methods=["GET"])
@permission_required_body("policyhub.view")
async def search_statements():
    """Cross-document search powering the document picker and statement search.

    Returns two kinds of hits, merged, in one shape:

    - **Title matches** (``match_type="title"``): documents whose decrypted title
      contains ``q`` as a case-insensitive substring. These are the authoritative
      result when ``q`` looks like a doc name (e.g. ``privileged``) and surface
      regardless of LanceDB indexing latency. Placed first.
    - **Statement matches** (``match_type="statement"``): LanceDB semantic
      similarity over indexed statements. Useful when ``q`` is conceptual.

    Query: q (required), types? (csv of policy,procedure,standard), top_k?,
    user_id. Scoped to the caller's own documents (no cross-org leakage).
    """
    baseuser = request.args.get("user_id")
    q = (request.args.get("q") or "").strip()
    if not baseuser:
        return jsonify({"error": "user_id is required"}), 400
    if not q:
        return jsonify({"error": "q (query) is required"}), 400
    _logged_in, user_id = parse_composite_user_id(baseuser)

    types_raw = (request.args.get("types") or "").strip()
    doc_types = [t.strip() for t in types_raw.split(",") if t.strip()] or None
    try:
        top_k = min(int(request.args.get("top_k", 10)), 50)
    except (TypeError, ValueError):
        top_k = 10

    # Use the decrypted index (titles in plaintext) so title matching works and
    # response titles aren't ciphertext blobs. Falls back to an S3 scan when the
    # index is empty (newly-bootstrapped user), matching the /list behaviour.
    from policy_hub.doc_index import list_documents, scan_policies_from_s3
    indexed = list_documents(user_id)
    if not indexed:
        indexed = scan_policies_from_s3(user_id, read_fn=_read_policy_yaml)
    doc_index: dict[str, dict] = {
        it.get("policy_id"): {
            "doc_ref": it.get("doc_ref"),
            "title": it.get("title"),
            "type": it.get("type"),
        }
        for it in indexed
        if it.get("policy_id")
    }
    if not doc_index:
        return jsonify({"results": [], "query": q, "total": 0}), 200

    needle = q.casefold()
    type_filter = set(doc_types) if doc_types else None
    title_hits: list[dict] = []
    for pid, meta in doc_index.items():
        title = meta.get("title") or ""
        if not isinstance(title, str) or needle not in title.casefold():
            continue
        if type_filter and meta.get("type") not in type_filter:
            continue
        title_hits.append({
            "policy_id": pid,
            "doc_ref": display_doc_ref(meta.get("doc_ref")),
            "title": title,
            "doc_type": meta.get("type"),
            "statement_id": None,
            "section_id": None,
            "seq": None,
            "text": title,
            "score": None,
            "match_type": "title",
        })
    # Stable order: exact > prefix > contains, then alphabetical within each tier.
    def _title_rank(hit: dict) -> tuple[int, str]:
        title = (hit.get("title") or "").casefold()
        if title == needle:
            tier = 0
        elif title.startswith(needle):
            tier = 1
        else:
            tier = 2
        return (tier, title)
    title_hits.sort(key=_title_rank)

    embeddings = await get_firework_embedding()
    vec = await asyncio.to_thread(embeddings.embed_query, q)

    lance = LanceDBServer()
    hits = await lance.query_statements_multi(
        embedding=vec,
        policy_ids=list(doc_index.keys()),
        top_k=top_k,
        doc_types=doc_types,
        user_id=user_id,
    )

    statement_hits: list[dict] = []
    for h in hits:
        pid = h.get("policy_id")
        meta = doc_index.get(pid, {})
        statement_hits.append({
            "policy_id": pid,
            "doc_ref": display_doc_ref(meta.get("doc_ref")),
            "title": meta.get("title"),
            "doc_type": h.get("doc_type"),
            "statement_id": h.get("statement_id"),
            "section_id": h.get("section_id"),
            "seq": h.get("seq"),
            "text": h.get("text"),
            "score": h.get("_distance"),
            "match_type": "statement",
        })

    # Title matches first; drop any statement match for a doc already surfaced
    # by title so the picker doesn't show the same doc twice.
    titled_pids = {r["policy_id"] for r in title_hits}
    results = title_hits + [r for r in statement_hits if r["policy_id"] not in titled_pids]
    results = results[:top_k]
    return jsonify({"results": results, "query": q, "total": len(results)}), 200


def _doc_query_text(item: dict) -> str:
    """Build a representative query string from a document's purpose + statements
    for 'related documents' similarity search."""
    parts: list[str] = []
    for sec in item.get("sections", []) or []:
        sid = sec.get("id", "")
        if sid.endswith(".purpose"):
            parts.append(sec.get("body_html", ""))
        for stmt in sec.get("statements", []) or []:
            parts.append(stmt.get("text", ""))
    text = " ".join(p for p in parts if p)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:2000]


@policy_hub_bp.route("/<policy_id>/related", methods=["GET"])
@permission_required_body("policyhub.view")
async def related_documents(policy_id: str):
    """Suggest related documents by statement similarity (excludes self).

    Query: user_id, top_k?. Returns distinct documents ranked by their best
    matching statement, plus any pinned ``linked_doc_refs``.
    """
    baseuser = request.args.get("user_id")
    if not baseuser or not policy_id:
        return jsonify({"error": "user_id is required"}), 400
    owner_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err
    try:
        top_k = min(int(request.args.get("top_k", 5)), 25)
    except (TypeError, ValueError):
        top_k = 5

    item = _read_policy_yaml(owner_id, _s3_key(owner_id, policy_id))
    if not item:
        return jsonify({"error": "Policy not found"}), 404

    doc_index = _user_policy_ids(owner_id)
    other_ids = [pid for pid in doc_index if pid != policy_id]
    query_text = _doc_query_text(item)
    related: list[dict] = []
    if other_ids and query_text:
        # Suggested-document similarity is a best-effort enrichment that depends
        # on the external embedding service + the LanceDB index. If either is
        # unavailable, degrade gracefully (empty suggestions + pinned links)
        # rather than 500-ing the whole panel with a "Service unavailable" toast.
        try:
            embeddings = await get_firework_embedding()
            vec = await asyncio.to_thread(embeddings.embed_query, query_text)
            lance = LanceDBServer()
            # over-fetch so we can collapse to distinct documents
            hits = await lance.query_statements_multi(
                embedding=vec, policy_ids=other_ids, top_k=top_k * 4, user_id=owner_id,
            )
            seen: set[str] = set()
            for h in hits:
                pid = h.get("policy_id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                meta = doc_index.get(pid, {})
                related.append({
                    "policy_id": pid,
                    "doc_ref": meta.get("doc_ref"),
                    "title": meta.get("title"),
                    "doc_type": meta.get("type"),
                    "best_match_text": h.get("text"),
                    "score": h.get("_distance"),
                })
                if len(related) >= top_k:
                    break
        except Exception as exc:
            logger.warning(
                "related_documents similarity search failed for policy %s "
                "(owner %s); returning pinned links only: %s",
                policy_id, owner_id, exc,
            )

    return jsonify({
        "policy_id": policy_id,
        "related": related,
        "linked_doc_refs": item.get("linked_doc_refs", []),
    }), 200


def _update_linked_doc_refs(policy_id: str, baseuser: str, doc_ref: str, add: bool):
    owner_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err
    if not doc_ref:
        return jsonify({"error": "doc_ref is required"}), 400
    key = _s3_key(owner_id, policy_id)
    item = _read_policy_yaml(owner_id, key)
    if not item:
        return jsonify({"error": "Policy not found"}), 404
    linked = list(item.get("linked_doc_refs", []))
    if add and doc_ref not in linked:
        linked.append(doc_ref)
    elif not add and doc_ref in linked:
        linked.remove(doc_ref)
    item["linked_doc_refs"] = linked
    item["etag"] = str(uuid.uuid4())
    _write_policy_yaml(owner_id, key, item)
    return jsonify({"policy_id": policy_id, "linked_doc_refs": linked}), 200


@policy_hub_bp.route("/<policy_id>/related/pin", methods=["POST"])
@permission_required_body("policyhub.edit")
def pin_related_document(policy_id: str):
    """Pin a related document by its doc_ref onto this document. Body: {user_id, doc_ref}."""
    body = request.get_json(silent=True) or {}
    return _update_linked_doc_refs(policy_id, body.get("user_id"), body.get("doc_ref"), add=True)


@policy_hub_bp.route("/<policy_id>/related/unpin", methods=["POST"])
@permission_required_body("policyhub.edit")
def unpin_related_document(policy_id: str):
    """Unpin a related document. Body: {user_id, doc_ref}."""
    body = request.get_json(silent=True) or {}
    return _update_linked_doc_refs(policy_id, body.get("user_id"), body.get("doc_ref"), add=False)


@policy_hub_bp.route("/statement/<statement_id>/trackers", methods=["GET"])
@permission_required_body("policyhub.view")
def statement_trackers(statement_id: str):
    """Reverse lookup: which tracker rows reference this statement.

    Query: user_id, page?, page_size?. Powers the inline "N referenced rows"
    badge next to a statement and its accordion drill-down.
    """
    baseuser = request.args.get("user_id")
    if not baseuser or not statement_id:
        return jsonify({"error": "user_id is required"}), 400
    try:
        page = max(1, int(request.args.get("page") or 1))
        page_size = min(200, max(1, int(request.args.get("page_size") or 50)))
    except (TypeError, ValueError):
        page, page_size = 1, 50

    from services.statement_tracker_refs import get_trackers_for_statement
    rows, total = get_trackers_for_statement(statement_id, page=page, page_size=page_size)
    return jsonify({
        "statement_id": statement_id,
        "trackers": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
    }), 200


@policy_hub_bp.route("/<policy_id>/trackers", methods=["GET"])
@permission_required_body("policyhub.view")
def policy_trackers(policy_id: str):
    """Distinct trackers referencing this document, with mapped-row counts.

    Powers the drag-and-drop tracker tag set shown above the statement list.
    Query: user_id.
    """
    baseuser = request.args.get("user_id")
    if not baseuser or not policy_id:
        return jsonify({"error": "user_id is required"}), 400
    owner_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    from services.statement_tracker_refs import get_trackers_for_policy
    return jsonify({"policy_id": policy_id, "trackers": get_trackers_for_policy(policy_id)}), 200


@policy_hub_bp.route("/<policy_id>/tracker-map", methods=["GET"])
@permission_required_body("policyhub.view")
def policy_tracker_map(policy_id: str):
    """Per-statement view of the rows a given tracker maps to this document.

    Query: user_id, tracker_id. Powers the accordion side panel — one entry
    per statement with the referencing rows nested underneath. Single
    round-trip to avoid N+1 from the per-statement badge endpoint.
    """
    baseuser = request.args.get("user_id")
    tracker_id = request.args.get("tracker_id")
    if not baseuser or not policy_id:
        return jsonify({"error": "user_id is required"}), 400
    if not tracker_id:
        return jsonify({"error": "tracker_id is required"}), 400
    owner_id, err = _check_policy_share_access(baseuser, policy_id)
    if err:
        return err

    item = _read_policy_yaml(owner_id, _s3_key(owner_id, policy_id))
    if not item:
        return jsonify({"error": "Policy not found"}), 404

    from services.statement_tracker_refs import get_refs_for_policy
    refs = get_refs_for_policy(policy_id, tracker_id=tracker_id)
    rows_by_statement: dict[str, list[dict]] = {}
    for r in refs:
        rows_by_statement.setdefault(r["statement_id"], []).append(
            {"row_id": r["row_id"], "column_id": r["column_id"], "status": r.get("status")}
        )

    statements = []
    for st in _statements_from_item(item):
        statements.append({
            "statement_id": st["statement_id"],
            "display_number": st["display_number"],
            "text": st["text"],
            "rows": rows_by_statement.get(st["statement_id"], []),
        })

    return jsonify({
        "policy_id": policy_id,
        "tracker_id": tracker_id,
        "statements": statements,
    }), 200


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
        existing = _read_policy_yaml(user_id, key)
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

    # Delete-through the metadata index (best-effort; reconcile heals drift).
    try:
        from policy_hub.doc_index import delete_document
        delete_document(policy_id)
    except Exception as idx_exc:
        logger.warning("doc_index delete failed for policy=%s: %s", policy_id, idx_exc)

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
        data = _read_framework_yaml(key)
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
        data = _read_framework_yaml(key)
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
        data = _read_framework_yaml(key)
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

    existing = _read_framework_yaml(key)
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
        _write_framework_yaml(key, record)
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

    data = _read_framework_yaml(_fw_key(framework_id))
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
        owner_policy = _read_policy_yaml(admin_id, _s3_key(admin_id, policy_id))
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


@policy_hub_bp.route("/admin/replicate-template", methods=["POST"])
def admin_replicate_template():
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    user_id = g.get("user_id") or request.get_json(silent=True, force=True).get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    doc_type = request.args.get("doc_type", "all")
    if doc_type not in ("all", "policy", "procedure", "standard"):
        return jsonify({"error": "doc_type must be all|policy|procedure|standard"}), 400

    dry_run = request.args.get("dry_run", "false").lower() == "true"

    # Lazy-import the Celery task so Flask app boot doesn't force a Redis
    # connection at module load time.
    from utils.celery_base import replicate_template_to_org

    task = replicate_template_to_org.delay(user_id, doc_type, dry_run)

    log_audit_event(
        action=TEMPLATE_REPLICATED,
        endpoint="/policy-hub/admin/replicate-template",
        ip=request.remote_addr,
        status="queued",
        actor_user_id=user_id,
        actor_email=get_email_by_id(user_id),
        metadata={"doc_type": doc_type, "dry_run": dry_run, "task_id": task.id},
    )

    return jsonify({"task_id": task.id, "status": "queued", "doc_type": doc_type, "dry_run": dry_run}), 202


@policy_hub_bp.route("/admin/replicate-status", methods=["GET"])
def admin_replicate_status():
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    from utils.celery_base import replicate_template_to_org

    result = replicate_template_to_org.AsyncResult(task_id)
    state = result.state

    payload = {"task_id": task_id, "state": state}
    if state == "SUCCESS":
        payload["result"] = result.result
    elif state == "FAILURE":
        payload["error"] = str(result.result)

    return jsonify(payload), 200


# ─── Per-org Template editor + AI-driven apply ──────────────────────────────

_VALID_TEMPLATE_DOC_TYPES = ("policy", "procedure", "standard")


def _resolve_template_user_id() -> str | None:
    """Resolve the acting user_id for a template endpoint."""
    return (
        getattr(g, "user_id", None)
        or getattr(g, "session_user_id", None)
        or request.form.get("user_id")
        or request.args.get("user_id")
        or (request.get_json(silent=True) or {}).get("user_id")
    )


def _apply_template_prompt(existing_yaml: dict, new_template: list) -> str:
    """Build an LLM prompt that recategorises an existing policy under a new template.

    *existing_yaml* is the parsed YAML dict (sections + content). *new_template* is
    a list of SectionDef. The LLM must preserve all content; missing source
    matches land in the closest section's body_html.
    """
    sections_desc = "\n".join(
        f"  - id={s.id}  title={s.title!r}  kind={s.kind}  required={s.required}  hint={s.prompt_help!r}"
        for s in new_template
    )

    # Build a single source HTML blob from the existing sections (preferred) or
    # the legacy `content` field.
    src_sections = existing_yaml.get("sections") or []
    if src_sections:
        source_html = _render_upload_sections_to_html(src_sections)
    else:
        source_html = existing_yaml.get("content") or ""

    return (
        "You are a compliance document restructuring assistant. The source HTML "
        "below is an existing policy that must be reorganized to match a NEW "
        "template structure. Your job: recategorize every existing statement and "
        "every block of body content under the new template's section IDs.\n\n"
        "STRICT RULES:\n"
        "- Do NOT invent content not present in the source. Preserve wording.\n"
        "- Every existing statement must end up in exactly one new section.\n"
        "- If source content does not clearly map to any new section, place it in "
        "the closest section's body_html (never drop content).\n"
        "- Preserve existing statement IDs verbatim when the text is substantially unchanged.\n"
        "- Include every section from the NEW template in the output, even when empty.\n"
        "- Return ONLY valid JSON — no markdown, no code fences, no commentary.\n\n"
        "TARGET SCHEMA:\n"
        f"{_UPLOAD_SCHEMA_DOC}\n\n"
        "NEW TEMPLATE SECTIONS (target structure):\n"
        f"{sections_desc}\n\n"
        f"SOURCE HTML:\n{(source_html or '')[:80000]}\n\n"
        "JSON:"
    )


def _template_chat_prompt(current_sections: list, instruction: str, doc_type: str) -> str:
    """Build an LLM prompt that revises the template draft based on a chat instruction."""
    current_desc = json.dumps(
        [serialize_section(s) for s in current_sections], indent=2
    )
    return (
        "You are a compliance template designer. Below is the current draft of a "
        f"{doc_type} template (list of sections). Apply the user's instruction and "
        "return the **complete revised template** as a JSON array of section "
        "objects. Each object must have keys: id (kebab-case slug), title, kind "
        "(one of: text, statements, steps, header_table, history), required (bool), "
        "prompt_help (one sentence guiding what content belongs).\n\n"
        "STRICT RULES:\n"
        "- Return ONLY a JSON array, no markdown, no code fences, no commentary.\n"
        "- Preserve existing section IDs when keeping a section; mint new kebab-case IDs only for newly added sections.\n"
        "- Do not drop sections unless the instruction explicitly asks.\n\n"
        f"CURRENT TEMPLATE:\n{current_desc}\n\n"
        f"USER INSTRUCTION:\n{instruction}\n\n"
        "JSON:"
    )


@policy_hub_bp.route("/template/<doc_type>", methods=["GET"])
def get_template_endpoint(doc_type: str):
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    if doc_type not in _VALID_TEMPLATE_DOC_TYPES:
        return jsonify({"error": f"doc_type must be one of {_VALID_TEMPLATE_DOC_TYPES}"}), 400

    user_id = _resolve_template_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    custom = load_custom_template(user_id, doc_type)
    if custom:
        sections = [serialize_section(s) for s in custom]
        meta = get_custom_template_metadata(user_id, doc_type) or {}
        return jsonify({
            "doc_type": doc_type,
            "sections": sections,
            "is_custom": True,
            "updated_at": meta.get("updated_at"),
        }), 200

    defaults = get_default_template(doc_type)
    return jsonify({
        "doc_type": doc_type,
        "sections": [serialize_section(s) for s in defaults],
        "is_custom": False,
        "updated_at": None,
    }), 200


@policy_hub_bp.route("/template/<doc_type>", methods=["PUT"])
def put_template_endpoint(doc_type: str):
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    if doc_type not in _VALID_TEMPLATE_DOC_TYPES:
        return jsonify({"error": f"doc_type must be one of {_VALID_TEMPLATE_DOC_TYPES}"}), 400

    user_id = _resolve_template_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    body = request.get_json(silent=True) or {}
    sections = body.get("sections")
    if not isinstance(sections, list):
        return jsonify({"error": "sections must be a list"}), 400

    try:
        save_custom_template(user_id, doc_type, sections)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        logger.error("put_template_endpoint: save failed: %s", exc)
        return jsonify({"error": "Failed to save template"}), 500

    log_audit_event(
        action=TEMPLATE_EDITED,
        endpoint=f"/policy-hub/template/{doc_type}",
        ip=request.remote_addr,
        status="ok",
        actor_user_id=user_id,
        actor_email=get_email_by_id(user_id),
        metadata={"doc_type": doc_type, "section_count": len(sections)},
    )
    return jsonify({"ok": True}), 200


@policy_hub_bp.route("/template/<doc_type>/reset", methods=["POST"])
def reset_template_endpoint(doc_type: str):
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    if doc_type not in _VALID_TEMPLATE_DOC_TYPES:
        return jsonify({"error": f"doc_type must be one of {_VALID_TEMPLATE_DOC_TYPES}"}), 400

    user_id = _resolve_template_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        delete_custom_template(user_id, doc_type)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    log_audit_event(
        action=TEMPLATE_RESET,
        endpoint=f"/policy-hub/template/{doc_type}/reset",
        ip=request.remote_addr,
        status="ok",
        actor_user_id=user_id,
        actor_email=get_email_by_id(user_id),
        metadata={"doc_type": doc_type},
    )
    return jsonify({"ok": True}), 200


@policy_hub_bp.route("/template/<doc_type>/chat", methods=["POST"])
def template_chat_endpoint(doc_type: str):
    """Revise the template draft based on a chat instruction. Returns revised sections."""
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    if doc_type not in _VALID_TEMPLATE_DOC_TYPES:
        return jsonify({"error": f"doc_type must be one of {_VALID_TEMPLATE_DOC_TYPES}"}), 400

    user_id = _resolve_template_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    body = request.get_json(silent=True) or {}
    instruction = (body.get("instruction") or "").strip()
    current = body.get("current_sections")
    if not instruction:
        return jsonify({"error": "instruction is required"}), 400
    if not isinstance(current, list) or not current:
        return jsonify({"error": "current_sections must be a non-empty list"}), 400

    try:
        current_defs = [deserialize_section(s) for s in current]
    except Exception as exc:
        return jsonify({"error": f"invalid current_sections: {exc}"}), 400

    prompt = _template_chat_prompt(current_defs, instruction, doc_type)
    loop = asyncio.new_event_loop()
    try:
        raw = loop.run_until_complete(
            get_fireworks_response2(
                user_id=user_id,
                user_message=prompt,
                role="user",
                credits=None,
                temp=0.1,
            )
        )
    except Exception as exc:
        logger.error("template_chat_endpoint LLM call failed: %s", exc)
        return jsonify({"error": "LLM call failed"}), 502
    finally:
        loop.close()

    if raw == "INSUFFICIENT":
        return jsonify({"error": "Insufficient credits"}), 402

    parsed = _parse_llm_json(raw) if raw else None
    # Accept either a bare array or { sections: [...] } in case the LLM wraps it
    revised: list | None = None
    if isinstance(parsed, list):
        revised = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("sections"), list):
        revised = parsed["sections"]

    if not revised:
        return jsonify({"error": "LLM returned unparseable JSON", "raw": (raw or "")[:500]}), 502

    # Validate the LLM output via save_custom_template's checks (without saving)
    seen = set()
    for idx, s in enumerate(revised):
        if not isinstance(s, dict):
            return jsonify({"error": f"section[{idx}] is not an object"}), 502
        sid = (s.get("id") or "").strip()
        if not sid or sid in seen:
            return jsonify({"error": f"section[{idx}] missing or duplicate id"}), 502
        seen.add(sid)
        if s.get("kind") not in ("text", "statements", "steps", "header_table", "history"):
            return jsonify({"error": f"section[{idx}] invalid kind {s.get('kind')!r}"}), 502

    return jsonify({"sections": revised}), 200


@policy_hub_bp.route("/template/<doc_type>/apply", methods=["POST"])
def apply_template_endpoint(doc_type: str):
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    if doc_type not in _VALID_TEMPLATE_DOC_TYPES:
        return jsonify({"error": f"doc_type must be one of {_VALID_TEMPLATE_DOC_TYPES}"}), 400

    user_id = _resolve_template_user_id()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    # Custom template must exist — applying defaults wouldn't change anything
    if load_custom_template(user_id, doc_type) is None:
        return jsonify({"error": "No custom template saved. Edit and save the template before applying."}), 400

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))

    from utils.celery_base import apply_template_to_org
    task = apply_template_to_org.delay(user_id, doc_type, dry_run)

    log_audit_event(
        action=TEMPLATE_APPLIED,
        endpoint=f"/policy-hub/template/{doc_type}/apply",
        ip=request.remote_addr,
        status="queued",
        actor_user_id=user_id,
        actor_email=get_email_by_id(user_id),
        metadata={"doc_type": doc_type, "dry_run": dry_run, "task_id": task.id},
    )
    return jsonify({"task_id": task.id, "status": "queued", "doc_type": doc_type, "dry_run": dry_run}), 202


@policy_hub_bp.route("/template/apply-status", methods=["GET"])
def apply_template_status_endpoint():
    auth_error = _require_framework_owner()
    if auth_error:
        return auth_error

    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"error": "task_id is required"}), 400

    from utils.celery_base import apply_template_to_org
    result = apply_template_to_org.AsyncResult(task_id)
    state = result.state
    payload = {"task_id": task_id, "state": state}
    if state == "SUCCESS":
        payload["result"] = result.result
    elif state == "FAILURE":
        payload["error"] = str(result.result)
    return jsonify(payload), 200
