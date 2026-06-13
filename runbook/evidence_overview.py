"""Pure helpers for normalizing evidence_overview blobs.

Kept dependency-free (no pandas / DB / S3 imports) so it can be imported and
unit-tested in isolation, and reused by runbook.helper and the workflow service.
"""

from __future__ import annotations


def _norm(text):
    return " ".join(str(text or "").lower().split())


def build_expectations_checklist(expectations_str, llm_items):
    """Reconcile an LLM's per-expectation verdicts against the authoritative
    semicolon-delimited expectations string.

    Guarantees exactly one entry per declared expectation, in order:
      [{"expectation": <verbatim>, "met": <bool>, "reason": <str>}]
    LLM items that don't match a declared expectation are dropped; declared
    expectations the LLM never addressed are recorded as unmet / "Not evaluated".
    Tolerant of malformed ``llm_items`` (non-list, missing keys).
    """
    points = [p.strip() for p in str(expectations_str or "").split(";") if p.strip()]
    if not points:
        return []

    # Index the LLM verdicts by normalized expectation text (and prefix match).
    matched = {}
    if isinstance(llm_items, list):
        for item in llm_items:
            if not isinstance(item, dict):
                continue
            key = _norm(item.get("expectation"))
            if key:
                matched[key] = item

    checklist = []
    for point in points:
        nkey = _norm(point)
        verdict = matched.get(nkey)
        if verdict is None:
            # Fall back to a forgiving prefix/substring match.
            for k, v in matched.items():
                if k and (k.startswith(nkey) or nkey.startswith(k)):
                    verdict = v
                    break
        if verdict is None:
            checklist.append(
                {"expectation": point, "met": False, "reason": "Not evaluated"}
            )
        else:
            checklist.append(
                {
                    "expectation": point,
                    "met": bool(verdict.get("met", False)),
                    "reason": str(verdict.get("reason", "") or ""),
                }
            )
    return checklist


def dedupe_evidence_overview(overview):
    """Normalize a (possibly legacy) evidence_overview so each file is
    attributed to exactly one artifact. Newer results already satisfy this; old
    blobs may list the same file under several artifacts (first-wins here, which
    matches the deterministic ranking used at classification time).

    Returns a new overview dict; tolerant of missing/odd shapes.
    """
    if not isinstance(overview, dict):
        return overview
    seen_files = set()
    result = {}
    for bucket in ("admissible", "inadmissible", "discarded"):
        entries = overview.get(bucket)
        if not isinstance(entries, list):
            result[bucket] = entries if entries is not None else []
            continue
        deduped = []
        for entry in entries:
            if not isinstance(entry, dict):
                deduped.append(entry)
                continue
            files = entry.get("files")
            if not isinstance(files, list):
                deduped.append(entry)
                continue
            kept = [f for f in files if f not in seen_files]
            seen_files.update(kept)
            # Drop an entry only if it HAD files and all were claimed elsewhere.
            if files and not kept:
                continue
            new_entry = dict(entry)
            new_entry["files"] = kept
            deduped.append(new_entry)
        result[bucket] = deduped
    # Preserve any other keys verbatim.
    for k, v in overview.items():
        if k not in result:
            result[k] = v
    return result
