"""One-time AI migration of legacy Policy Hub HTML policies into V2 structured schema.

Operator use:
  POST /policy-hub/admin/migrate              — queue all legacy policies for an org
  POST /policy-hub/admin/migrate?dry_run=true — dry-run (count only, no writes)
  POST /policy-hub/admin/migrate?policy_id=X  — target a single policy

Celery tasks:
  tasks.migrate_legacy_policies_org    — orchestrator; chunks → per-policy tasks
  tasks.migrate_legacy_policy          — single-policy worker
"""

import asyncio
import uuid

from utils.app_configs import MIGRATION_FIREWORKS_CONCURRENCY
from utils.base_logger import get_logger
from utils.s3_utils import list_all_files, load_yaml_from_s3

logger = get_logger(__name__)

_SCHEMA_DOC = """
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
    {
      "id": "policy.purpose",
      "title": "Purpose",
      "kind": "text",
      "body_html": "<p>…</p>"
    },
    {
      "id": "policy.statements",
      "title": "Policy Statements",
      "kind": "statements",
      "statements": [
        {"id": "<uuid>", "text": "Statement text.", "seq": 1}
      ]
    }
  ]
}
"""


def _migration_prompt(html: str, doc_type: str) -> str:
    """Build the Fireworks extraction prompt for one legacy policy."""
    from policy_hub.templates import get_template
    try:
        template = get_template(doc_type)
        sections_desc = "\n".join(
            f"  - id={s.id}  title={s.title!r}  kind={s.kind}  required={s.required}"
            for s in template
        )
    except KeyError:
        sections_desc = "(unknown template)"

    return (
        "You are a compliance document structuring assistant. "
        "Given the legacy HTML document below, extract its content into the JSON schema shown.\n\n"
        "TARGET SCHEMA:\n"
        f"{_SCHEMA_DOC}\n\n"
        "TEMPLATE SECTIONS (map each heading to the closest section id):\n"
        f"{sections_desc}\n\n"
        "RULES:\n"
        "- Assign every <li> in the policy statements / procedure steps section a unique UUID v4.\n"
        "- For text sections, preserve the HTML in body_html verbatim.\n"
        "- Include all sections from the template; use empty body_html for missing sections.\n"
        "- Return ONLY valid JSON — no markdown, no code fences, no explanation.\n\n"
        f"LEGACY DOCUMENT HTML:\n{html[:80000]}\n\n"
        "JSON:"
    )


async def _migrate_one_policy(
    key: str,
    data: dict,
    dry_run: bool = False,
) -> dict:
    """Migrate one policy YAML from legacy HTML to V2 structured format.

    Returns a result dict with keys: policy_id, status, error (if any).
    """
    from utils.fireworkzz import get_fireworks_response2
    from policy_hub.structured import sync_statements_to_lance
    from policy_hub.templates import validate

    import os
    S3_BUCKET = os.getenv("S3_BUCKET")

    policy_id = data.get("policy_id", key)
    doc_type = data.get("type", "policy")
    # key format: "{user_id}/policies/{policy_id}.yaml"
    _key_parts = key.split("/")
    _migration_user_id = _key_parts[0] if len(_key_parts) >= 3 else "migration"

    if dry_run:
        return {"policy_id": policy_id, "status": "dry_run"}

    # Back up raw HTML
    existing_content = data.get("content", "")
    data["raw_html"] = existing_content
    data["migration_status"] = "in_progress"

    prompt = _migration_prompt(existing_content, doc_type)

    sem = asyncio.Semaphore(MIGRATION_FIREWORKS_CONCURRENCY)

    async def call_ai():
        async with sem:
            return await get_fireworks_response2(
                user_id="migration",
                user_message=prompt,
                role="user",
                credits=None,
                temp=0.0,
            )

    raw = await call_ai()

    import json as _json
    try:
        structured = _json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
    except Exception as exc:
        logger.warning("Migration JSON parse failed for %s: %s", policy_id, exc)
        # Retry once with a slightly less strict parse
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                structured = _json.loads(m.group(0))
            except Exception:
                data["migration_status"] = "migration_failed"
                _save_yaml(key, data, S3_BUCKET)
                return {"policy_id": policy_id, "status": "migration_failed", "error": str(exc)}
        else:
            data["migration_status"] = "migration_failed"
            _save_yaml(key, data, S3_BUCKET)
            return {"policy_id": policy_id, "status": "migration_failed", "error": "No JSON in response"}

    # Validate
    sections_html = _render_sections_to_html(structured.get("sections", []))
    vr = validate(sections_html, doc_type)

    data["template_version"] = 1
    data["migration_status"] = "ok"
    data["validation_status"] = "ok" if vr.ok else "needs_review"
    data["metadata"] = structured.get("metadata", data.get("metadata", {}))
    data["sections"] = structured.get("sections", [])

    # Sync statements to LanceDB
    all_stmts = []
    from policy_hub.structured import Statement
    for sec in data["sections"]:
        for raw_s in sec.get("statements", []):
            all_stmts.append(
                Statement(
                    id=raw_s.get("id", str(uuid.uuid4())),
                    text=raw_s.get("text", ""),
                    seq=raw_s.get("seq", 1),
                    section_id=sec.get("id", ""),
                )
            )

    version = data.get("metadata", {}).get("version", "1.0") if isinstance(data.get("metadata"), dict) else "1.0"
    try:
        await sync_statements_to_lance(
            policy_id=policy_id,
            doc_type=doc_type,
            version=version,
            statements=all_stmts,
            user_id=_migration_user_id,
        )
    except Exception as exc:
        logger.error("sync_statements_to_lance failed during migration for %s: %s", policy_id, exc)

    _save_yaml(key, data, S3_BUCKET)
    logger.info("Migration complete for policy=%s", policy_id)
    return {"policy_id": policy_id, "status": "ok"}


def _render_sections_to_html(sections: list) -> str:
    """Produce minimal data-section-id HTML for validation purposes."""
    parts = []
    for sec in sections:
        parts.append(f'<div data-section-id="{sec.get("id", "")}">')
        parts.append(f'<h2>{sec.get("title", "")}</h2>')
        for stmt in sec.get("statements", []):
            stmt_id = stmt.get("id", "")
            attr = f' data-statement-id="{stmt_id}"' if stmt_id else ""
            parts.append(f"<ul><li{attr}>{stmt.get('text','')}</li></ul>")
        if sec.get("body_html"):
            parts.append(sec["body_html"])
        parts.append("</div>")
    return "\n".join(parts)


def _save_yaml(key: str, data: dict, bucket: str):
    import io
    import yaml
    from utils.s3_utils import s3bucket as _s3bucket

    s3 = _s3bucket()
    raw = yaml.safe_dump(data, sort_keys=False).encode("utf-8")
    s3.upload_fileobj(io.BytesIO(raw), bucket, key)


def list_legacy_policy_keys(user_id: str) -> list[str]:
    """Return S3 keys for all legacy (non-V2) policies for a user."""
    prefix = f"{user_id}/policies/"
    objects = list_all_files(folder=prefix)
    keys = []
    for obj in objects:
        key = obj.get("Key", "")
        if not key.endswith(".yaml") or "/jobs/" in key:
            continue
        data = load_yaml_from_s3(key)
        if data and data.get("template_version") != 1:
            keys.append(key)
    return keys
