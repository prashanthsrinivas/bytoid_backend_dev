import logging
import uuid
from datetime import datetime, timezone

from policy_hub.templates import get_template, validate
from policy_hub.migrate_legacy_policies import _render_sections_to_html
from utils.s3_utils import s3bucket, load_yaml_from_s3
from utils.celery_base import celery
from utils.app_configs import S3_BUCKET

logger = logging.getLogger(__name__)


def _replicate_sections(existing_sections: list, doc_type: str) -> list:
    """
    Sync existing sections list to the current template for doc_type.
    - Template sections: added (empty) if missing, reordered to match template order
    - Extra sections not in template: appended at end (preserved, not deleted)
    - Existing section content: kept exactly as-is
    """
    template_defs = get_template(doc_type)
    existing_by_id = {s["id"]: s for s in (existing_sections or [])}
    template_ids = {sd.id for sd in template_defs}

    result = []
    for sd in template_defs:
        if sd.id in existing_by_id:
            result.append(existing_by_id[sd.id])
        else:
            new_sec = {"id": sd.id, "title": sd.title, "kind": sd.kind, "body_html": ""}
            if sd.kind in ("statements", "steps"):
                new_sec["statements"] = []
            result.append(new_sec)

    # Append user-added sections not in the template (don't delete them)
    for sec in (existing_sections or []):
        if sec["id"] not in template_ids:
            result.append(sec)

    return result


@celery.task(bind=True)
def replicate_template_to_org(self, user_id: str, doc_type: str = "all", dry_run: bool = False):
    """
    Sync all V2 policies for user_id to the current template definition.
    doc_type: "all" applies each type's template to that type's documents.
    Returns: { processed, updated, skipped, errors, dry_run, doc_type }
    """
    from policy_hub.routes import _s3_key, _write_yaml_to_s3, _sync_statements

    types_to_process = ["policy", "procedure", "standard"] if doc_type == "all" else [doc_type]

    # List all policy YAMLs for this user
    prefix = f"{user_id}/policies/"
    try:
        response = s3bucket().list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    except Exception as e:
        logger.error("replicate_template: S3 list failed for %s: %s", user_id, e)
        return {"error": str(e), "dry_run": dry_run, "doc_type": doc_type}

    keys = [
        obj["Key"]
        for obj in response.get("Contents", [])
        if obj["Key"].endswith(".yaml") and "/raw/" not in obj["Key"] and "/jobs/" not in obj["Key"]
    ]

    processed, updated, skipped, errors = 0, 0, 0, []

    for key in keys:
        try:
            data = load_yaml_from_s3(S3_BUCKET, key)
            if not data:
                skipped += 1
                continue

            policy_type = data.get("type")
            if policy_type not in types_to_process:
                skipped += 1
                continue

            if data.get("template_version") != 1:
                skipped += 1  # legacy-only, no sections to sync
                continue

            existing_sections = data.get("sections", [])
            new_sections = _replicate_sections(existing_sections, policy_type)
            processed += 1

            # Check if anything actually changed
            existing_ids = [s["id"] for s in existing_sections]
            new_ids = [s["id"] for s in new_sections]
            if existing_ids == new_ids:
                skipped += 1
                continue

            # Re-validate
            rendered_html = _render_sections_to_html(new_sections)
            vr = validate(rendered_html, policy_type)
            new_validation_status = "ok" if vr.ok else "needs_review"

            if not dry_run:
                data["sections"] = new_sections
                data["validation_status"] = new_validation_status
                data["etag"] = str(uuid.uuid4())
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                _write_yaml_to_s3(key, data)

                # Re-sync statements to LanceDB (best-effort, don't fail job)
                try:
                    import asyncio

                    loop = asyncio.new_event_loop()
                    _sync_statements(data, user_id, policy_type, loop)
                    loop.close()
                except Exception as se:
                    logger.warning("replicate_template: LanceDB sync failed for %s: %s", key, se)

            updated += 1

        except Exception as e:
            logger.error("replicate_template: failed for key %s: %s", key, e)
            errors.append({"key": key, "error": str(e)})

    result = {
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
        "doc_type": doc_type,
    }
    logger.info("replicate_template completed: %s", result)
    return result
