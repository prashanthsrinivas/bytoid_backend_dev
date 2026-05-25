"""S3-backed per-org template storage.

Templates live at ``<user_id>/templates/<doc_type>.yaml`` and override the
hardcoded defaults in ``policy_hub.templates`` for that user. Reads fall
through to the default when no custom YAML exists; writes always go to S3.
"""

import io

import yaml
from botocore.exceptions import ClientError

from utils.base_logger import get_logger
from utils.s3_utils import S3_BUCKET, s3bucket

logger = get_logger(__name__)

VALID_DOC_TYPES = ("policy", "procedure", "standard")
VALID_KINDS = ("text", "statements", "steps", "header_table", "history")


def template_s3_key(user_id: str, doc_type: str) -> str:
    return f"{user_id}/templates/{doc_type}.yaml"


def load_custom_template(user_id: str, doc_type: str):
    """Return the user's saved template sections (list of dicts), or None if not set."""
    from policy_hub.templates import deserialize_section

    if doc_type not in VALID_DOC_TYPES:
        return None

    key = template_s3_key(user_id, doc_type)
    s3 = s3bucket()
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return None
        logger.warning("load_custom_template: S3 error %s for %s", code, key)
        return None
    except Exception as e:
        logger.warning("load_custom_template: unexpected error for %s: %s", key, e)
        return None

    try:
        data = yaml.safe_load(resp["Body"].read().decode("utf-8")) or {}
        raw_sections = data.get("sections") or []
        return [deserialize_section(d) for d in raw_sections]
    except Exception as e:
        logger.error("load_custom_template: parse failed for %s: %s", key, e)
        return None


def save_custom_template(user_id: str, doc_type: str, sections: list[dict]) -> None:
    """Validate then persist a custom template to S3.

    Raises ValueError on validation failure.
    """
    if doc_type not in VALID_DOC_TYPES:
        raise ValueError(f"doc_type must be one of {VALID_DOC_TYPES}")
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty list")

    seen_ids = set()
    cleaned: list[dict] = []
    for idx, raw in enumerate(sections):
        if not isinstance(raw, dict):
            raise ValueError(f"section[{idx}] is not an object")
        sid = (raw.get("id") or "").strip()
        title = (raw.get("title") or "").strip()
        kind = raw.get("kind")
        if not sid:
            raise ValueError(f"section[{idx}] missing id")
        if not title:
            raise ValueError(f"section[{idx}] missing title")
        if kind not in VALID_KINDS:
            raise ValueError(f"section[{idx}] kind must be one of {VALID_KINDS}")
        if sid in seen_ids:
            raise ValueError(f"duplicate section id: {sid}")
        seen_ids.add(sid)
        cleaned.append({
            "id": sid,
            "title": title,
            "kind": kind,
            "required": bool(raw.get("required", True)),
            "prompt_help": (raw.get("prompt_help") or "").strip(),
        })

    from datetime import datetime, timezone
    payload = {
        "doc_type": doc_type,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sections": cleaned,
    }

    key = template_s3_key(user_id, doc_type)
    yaml_bytes = yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
    s3bucket().upload_fileobj(io.BytesIO(yaml_bytes), S3_BUCKET, key)
    logger.info("save_custom_template: wrote %s (%d sections)", key, len(cleaned))


def delete_custom_template(user_id: str, doc_type: str) -> None:
    """Reset to default by deleting the S3 override (no-op if absent)."""
    if doc_type not in VALID_DOC_TYPES:
        raise ValueError(f"doc_type must be one of {VALID_DOC_TYPES}")
    key = template_s3_key(user_id, doc_type)
    try:
        s3bucket().delete_object(Bucket=S3_BUCKET, Key=key)
        logger.info("delete_custom_template: removed %s", key)
    except Exception as e:
        logger.warning("delete_custom_template: failed for %s: %s", key, e)


def get_custom_template_metadata(user_id: str, doc_type: str) -> dict | None:
    """Return updated_at metadata for a custom template, or None if not set."""
    if doc_type not in VALID_DOC_TYPES:
        return None
    key = template_s3_key(user_id, doc_type)
    s3 = s3bucket()
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        data = yaml.safe_load(resp["Body"].read().decode("utf-8")) or {}
        return {"updated_at": data.get("updated_at")}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return None
        return None
    except Exception:
        return None
