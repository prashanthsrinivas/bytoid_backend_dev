"""In-process collection engine — enumerate scopes, run the provider's collectors.

No external Lambda (unlike AWS): Azure/GCP collection runs in-process (the AWS
in-process fallback, proven). Per-scope failures land in ``collector_status`` and
degrade to a partial snapshot; the run never aborts.
"""

from __future__ import annotations

from cspm_core.normalize import build_snapshot


def run_collection(provider, *, scan_id, audit_id, scope, creds) -> dict:
    """Run one audit's collection and return the snapshot. Never raises."""
    findings: list = []
    collector_status: dict = {}

    try:
        scopes = provider.enumerate_scopes(creds, scope) or []
    except Exception as exc:
        collector_status["_discovery"] = f"error: {type(exc).__name__}"
        scopes = []

    if not scopes:
        collector_status.setdefault("_discovery", "error: no_scopes")
        snap = build_snapshot(scan_id=scan_id, audit_id=audit_id, findings=[],
                              scopes_scanned=[], collector_status=collector_status, scope=scope)
        snap["fatal"] = True
        return snap

    domains = scope.get("domains") or list(provider.domains)
    scopes_scanned = []
    for sc in scopes:
        sid = sc.get("id", "")
        try:
            f, status = provider.collect(creds, sc, domains)
            findings += f or []
            collector_status.update(status or {})
            collector_status[sid] = "ok"
            scopes_scanned.append(sid)
        except Exception as exc:
            collector_status[sid] = f"error: {type(exc).__name__}"

    return build_snapshot(scan_id=scan_id, audit_id=audit_id, findings=findings,
                          scopes_scanned=scopes_scanned, collector_status=collector_status, scope=scope)
