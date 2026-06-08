"""Map OSINT findings to runbook 'Responses & Evidence' records.

Pure transformation (no I/O), so it is trivially testable and reused by both the
dashboard drill-down and the report's Evidence Summary. Each evidence record
carries exactly the fields the requirements mandate:

    Source · Collection date · Evidence type · Finding summary ·
    Supporting details · Risk indicators

Evidence is *derived* from the stored snapshot on demand — it is never persisted
separately (the snapshot in S3 is the single source of truth).
"""

from __future__ import annotations

from vra.schema import CATEGORY_LABELS


def finding_to_evidence(finding: dict) -> dict:
    """One finding -> one evidence record in the mandated shape."""
    return {
        "source": finding.get("source", ""),
        "source_url": finding.get("source_url", ""),
        "collection_date": finding.get("collected_at", ""),
        "evidence_type": finding.get("evidence_type", ""),
        "category": finding.get("category", ""),
        "category_label": CATEGORY_LABELS.get(finding.get("category", ""), ""),
        "finding_summary": finding.get("finding_summary", ""),
        "supporting_details": finding.get("supporting_details", {}),
        "risk_indicators": finding.get("risk_indicators", []),
        "severity": finding.get("severity", "info"),
    }


def snapshot_to_evidence(snapshot: dict) -> dict:
    """Build the evidence bundle for a snapshot: records + artifact counts.

    Returns ``{records, total_findings, evidence_artifacts, collected_window}``
    where ``evidence_artifacts`` counts records that carry a source URL (the
    citable artifacts) — what the report's Evidence Summary reports.
    """
    findings = snapshot.get("findings") or []
    records = [finding_to_evidence(f) for f in findings]
    artifacts = sum(1 for r in records if r["source_url"])
    dates = sorted(r["collection_date"] for r in records if r["collection_date"])
    return {
        "records": records,
        "total_findings": len(records),
        "evidence_artifacts": artifacts,
        "collected_window": {
            "first": dates[0] if dates else snapshot.get("scanned_at", ""),
            "last": dates[-1] if dates else snapshot.get("scanned_at", ""),
        },
        "scan_id": snapshot.get("scan_id", ""),
        "scanned_at": snapshot.get("scanned_at", ""),
    }


def evidence_by_category(snapshot: dict) -> dict:
    """Group evidence records by intelligence category (for drill-down views)."""
    grouped: dict[str, list] = {}
    for record in snapshot_to_evidence(snapshot)["records"]:
        grouped.setdefault(record["category"], []).append(record)
    return grouped
