"""Weave VRA questions into a workflow's question set (the "Vendor Intelligence"
section), additively and idempotently.

The questionnaire's questions live in ``workflow_json["assigned_questions"]``
(the same list the Playground renders). This module:
  * keeps a distinct **Vendor Intelligence** block at the FRONT of that list —
    the two locked vendor questions first, then auto-generated OSINT-derived
    follow-ups — without disturbing the rest of the questionnaire (e.g. an
    uploaded SIG set);
  * tags VRA questions with ``vra_question`` / ``osint_derived`` / ``source_url``
    so the frontend can badge them and the assessor can trace each back to
    evidence.

Persistence mirrors the existing ``edit_assigned_question`` flow exactly: read
the workflow JSON, decrypt the content fields, mutate ``assigned_questions``
(which is NOT an encrypted content field), then ``save_playbook_to_s3`` (which
re-encrypts the content fields). Pure list logic is split out for testing.
"""

from __future__ import annotations

import uuid

from vra.schema import DEFAULT_VRA_QUESTIONS, SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM

VENDOR_INTEL_SECTION = "Vendor Intelligence"


def make_question_item(
    question: str,
    *,
    osint_derived: bool = False,
    vra_role: str | None = None,
    locked: bool = False,
    source_url: str = "",
    options: dict | None = None,
) -> dict:
    """Build one ``assigned_questions`` item in the canonical shape.

    ``options == {}`` denotes a free-text question (vs an MCQ). The extra
    ``vra_*``/``osint_derived``/``source_url`` keys are additive and ignored by
    the existing question code.
    """
    return {
        "id": uuid.uuid4().hex,
        "question": question,
        "options": options or {},
        "answer": None,
        "comment": None,
        "section": VENDOR_INTEL_SECTION,
        "evidence_required": [],
        "vra_question": True,
        "osint_derived": osint_derived,
        "vra_role": vra_role,
        "locked": locked,
        "source_url": source_url,
    }


def vendor_question_items() -> list[dict]:
    """The two mandatory, locked vendor questions as assigned-question items."""
    return [
        make_question_item(spec["question"], vra_role=spec["vra_role"], locked=True)
        for spec in DEFAULT_VRA_QUESTIONS
    ]


# --- OSINT-derived question generation (deterministic, grounded, free) --------
# Template by evidence_type / risk indicator. Deterministic (no LLM) so every
# question is traceable to its finding and there is no hallucination or cost.
def _question_for_finding(f: dict) -> str | None:
    et = f.get("evidence_type")
    sd = f.get("supporting_details") or {}
    summary = f.get("finding_summary", "")
    if et == "known_exploited_vulnerability":
        cve = sd.get("cve", "a known-exploited CVE")
        return (
            f"OSINT identified {cve} (CISA Known Exploited Vulnerability) on your "
            "internet-facing infrastructure. Confirm remediation status, date, and "
            "compensating controls."
        )
    if et in ("public_cve", "infrastructure_exposure") and f.get("risk_indicators"):
        return (
            f"OSINT flagged public vulnerabilities/exposure: {summary}. Describe "
            "your patching cadence and how these are remediated or mitigated."
        )
    if et == "breach_disclosure":
        name = sd.get("name") or "a public breach"
        return (
            f"A public breach disclosure was found ({name}). Describe the incident "
            "response taken and the controls now in place to prevent recurrence."
        )
    if et == "dmarc" and "dmarc_missing" in (f.get("risk_indicators") or []):
        return (
            "OSINT found no enforced DMARC policy on your primary domain. Describe "
            "your email authentication / anti-spoofing controls."
        )
    if et == "spf" and "spf_missing" in (f.get("risk_indicators") or []):
        return (
            "OSINT found no SPF record on your primary domain. Describe your email "
            "sender-authentication controls."
        )
    if et == "security_txt" and "no_security_txt" in (f.get("risk_indicators") or []):
        return (
            "OSINT found no published security.txt / vulnerability-disclosure "
            "contact. How can researchers report vulnerabilities to you?"
        )
    if et == "security_headers" and f.get("risk_indicators"):
        return (
            f"OSINT found missing HTTP security headers ({summary}). Describe your "
            "web application hardening standards."
        )
    if et == "reputation_summary" and "adverse_media" in (f.get("risk_indicators") or []):
        return (
            "OSINT surfaced security-negative news/adverse media about your "
            "organization. Please provide context and any remediation."
        )
    return None


def derive_osint_questions(snapshot: dict, *, limit: int = 15) -> list[dict]:
    """Generate grounded OSINT-derived question items from a scan snapshot.

    Only medium+ findings produce questions; each carries its ``source_url`` so
    the assessor can trace it. Deduplicated by question text.
    """
    sev_ok = {SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM}
    items: list[dict] = []
    seen: set[str] = set()
    for f in snapshot.get("findings") or []:
        if f.get("severity") not in sev_ok:
            continue
        q = _question_for_finding(f)
        if not q or q in seen:
            continue
        seen.add(q)
        items.append(make_question_item(q, osint_derived=True, source_url=f.get("source_url", "")))
        if len(items) >= limit:
            break
    return items


def set_vra_block(
    assigned: list[dict],
    *,
    vendor_items: list[dict] | None = None,
    osint_items: list[dict] | None = None,
    replace_osint: bool = False,
) -> list[dict]:
    """Pure: rebuild ``assigned_questions`` with the VRA block kept at the front.

    Order: [vendor questions] + [osint-derived] + [everything else]. Vendor
    questions are idempotent (deduped by ``vra_role``). When ``replace_osint`` is
    set, the prior osint-derived questions are replaced by ``osint_items`` (so a
    re-scan refreshes them rather than piling up).
    """
    non_vra = [q for q in assigned if not q.get("vra_question")]
    existing_vra = [q for q in assigned if q.get("vra_question")]
    existing_vendor = [q for q in existing_vra if q.get("vra_role")]
    existing_osint = [q for q in existing_vra if q.get("osint_derived")]

    vendor = list(existing_vendor)
    have_roles = {q.get("vra_role") for q in vendor}
    for item in vendor_items or []:
        if item.get("vra_role") not in have_roles:
            vendor.append(item)
            have_roles.add(item.get("vra_role"))

    if osint_items is None:
        osint = existing_osint
    elif replace_osint:
        osint = list(osint_items)
    else:
        osint = existing_osint + list(osint_items)

    return vendor + osint + non_vra


# --- S3 load/save wrappers (thin; mirror edit_assigned_question) --------------
def _load_workflow(user_id: str, filename: str):
    from playbook.helperzz import _PLAYBOOK_CONTENT_FIELDS, _dec_pb, base_name
    from utils.s3_utils import read_json_from_s3

    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    loc = f"{user_id}/workflow/{base_name(filename=filename)}/{filename}"
    wf = read_json_from_s3(loc)
    if not wf:
        return None, filename
    for field in _PLAYBOOK_CONTENT_FIELDS:
        if field in wf:
            wf[field] = _dec_pb(user_id, wf[field])
    return wf, filename


def inject_into_workflow(
    user_id: str,
    filename: str,
    *,
    vendor_items: list[dict] | None = None,
    osint_items: list[dict] | None = None,
    replace_osint: bool = False,
) -> dict:
    """Load the workflow, rebuild its VRA question block, and save. Best-effort."""
    from playbook.helperzz import save_playbook_to_s3

    wf, filename = _load_workflow(user_id, filename)
    if wf is None:
        return {"status": "error", "message": "workflow not found"}
    before = wf.get("assigned_questions") or []
    wf["assigned_questions"] = set_vra_block(
        before, vendor_items=vendor_items, osint_items=osint_items, replace_osint=replace_osint
    )
    save_playbook_to_s3(wf, user_id, "vra questions injected", filename)
    return {
        "status": "success",
        "total_questions": len(wf["assigned_questions"]),
        "added": len(wf["assigned_questions"]) - len(before),
    }
