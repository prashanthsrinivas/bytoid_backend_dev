"""Pure helpers for normalizing evidence_overview blobs.

Kept dependency-free (no pandas / DB / S3 imports) so it can be imported and
unit-tested in isolation, and reused by runbook.helper and the workflow service.
"""

from __future__ import annotations

import re
from difflib import get_close_matches


def _norm(text):
    return " ".join(str(text or "").lower().split())


def _canon_key(text):
    """Aggressive normalization for matching: lowercase, drop punctuation,
    collapse whitespace. ``"SOP / procedure"`` and ``"sop  procedure"`` both
    become ``"sop procedure"``."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def canonicalize_artifact_name(name, known_artifacts, cutoff=0.6):
    """Map a free-form artifact name (e.g. an LLM classifier's output) back to a
    name that actually exists in the evidence config.

    Every downstream step in the auto-fill pipeline compares artifact names with
    exact string equality (``artifact in allowed_artifacts``,
    ``evidence_required`` intersection, ``expectations`` lookup). An LLM that
    returns ``"Access Control Policy"`` or ``"system screenshot"`` instead of the
    config's ``"Policies"`` / ``"Screenshot"`` would otherwise be silently
    dropped. This collapses that drift.

    ``known_artifacts`` may be a list of config dicts (each with an ``artifact``
    key) or an iterable of artifact-name strings. Returns the canonical config
    name, or ``None`` when nothing matches confidently.
    """
    if not name or not str(name).strip():
        return None

    # Accept either config dicts or bare strings.
    names = []
    for a in known_artifacts or []:
        if isinstance(a, dict):
            a = a.get("artifact")
        if a and isinstance(a, str):
            names.append(a)
    if not names:
        return None

    # 1) Exact match, ignoring case/whitespace.
    target_simple = _norm(name)
    for cand in names:
        if _norm(cand) == target_simple:
            return cand

    # 2) Exact match, ignoring punctuation too.
    by_key = {}
    for cand in names:
        by_key.setdefault(_canon_key(cand), cand)
    target_key = _canon_key(name)
    if not target_key:
        return None
    if target_key in by_key:
        return by_key[target_key]

    # 3) Whole-substring containment (e.g. "screenshot of dashboard" ->
    #    "Screenshot", "system log export" -> "System log file"). Prefer the
    #    longest (most specific) config key so a generic short key can't win
    #    over a precise one.
    contained = [
        k
        for k in by_key
        if len(k) >= 4
        and len(target_key) >= 4
        and (k in target_key or target_key in k)
    ]
    if contained:
        return by_key[max(contained, key=len)]

    # 4) Fuzzy match on the punctuation-stripped keys (handles pluralization and
    #    minor wording, e.g. "system screenshot" -> "Screenshot").
    close = get_close_matches(target_key, list(by_key.keys()), n=1, cutoff=cutoff)
    if close:
        return by_key[close[0]]

    return None


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
