"""Shared guard for mutation endpoints in policy_hub, runbook, and ai_reporting."""

from flask import jsonify

from utils.base_logger import get_logger
from workflow_route.state_machine import get_workflow_for_doc

logger = get_logger(__name__)

# Stages that lock the underlying document for content edits.
# Quality reviewers can still edit (they're expected to tweak before approving);
# governance reviewers and approvers are strictly read-only per product spec.
_READ_ONLY_STATES = frozenset({"governance_review", "approval"})


def guard_mutation(doc_type: str, doc_id: str, doc_version: str):
    """Return a (jsonify_response, status_code) error tuple if the document is published.

    Usage in a route:
        err = guard_mutation("policy", policy_id, version)
        if err:
            return err

    Returns None when mutation is allowed.
    """
    try:
        workflow = get_workflow_for_doc(doc_type, doc_id, doc_version)
    except Exception:
        return None  # If workflow state can't be read, allow mutation (fail open)

    if workflow and workflow.get("state") == "published":
        return (
            jsonify(
                {
                    "error": "Cannot modify a published document. "
                    "Create a new version by submitting for review again.",
                    "workflow_id": workflow.get("workflow_id"),
                }
            ),
            409,
        )
    return None


def assert_doc_editable(
    doc_type: str,
    doc_id: str,
    actor_user_id: str,
    *,
    doc_version: str = "1.0",
) -> tuple[bool, str | None, dict | None]:
    """Returns ``(ok, reason, workflow_row)``.

    ``ok=True`` when no workflow exists, the workflow is in ``draft``,
    ``quality_review``, or ``published`` (latter is gated separately by
    ``guard_mutation``). ``ok=False`` with a human-readable reason when in
    ``governance_review`` or ``approval``.

    Failures looking up workflow state fail-open (return True) so a DB blip
    doesn't lock every report.
    """
    try:
        wf = get_workflow_for_doc(doc_type, doc_id, doc_version)
    except Exception as exc:
        logger.warning(
            "assert_doc_editable: get_workflow_for_doc(%s, %s) failed: %s — "
            "allowing edit by default",
            doc_type, doc_id, exc,
        )
        return True, None, None

    if not wf:
        return True, None, None

    state = wf.get("state") or "draft"
    if state in _READ_ONLY_STATES:
        return (
            False,
            (
                f"Report is read-only while in {state.replace('_', ' ')} stage. "
                "Send it back to draft to make further changes."
            ),
            wf,
        )
    return True, None, wf
