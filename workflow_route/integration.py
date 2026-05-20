"""Shared guard for mutation endpoints in policy_hub, runbook, and ai_reporting."""

from flask import jsonify

from workflow_route.state_machine import get_workflow_for_doc


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
