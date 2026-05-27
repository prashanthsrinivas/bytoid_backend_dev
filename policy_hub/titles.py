"""Title extraction for policy/procedure/standard documents.

Kept dependency-free (stdlib only) so it is unit-testable without importing
the heavy ``policy_hub.routes`` module.
"""

import re

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def looks_like_uuid(value: str) -> bool:
    """True when *value* is a bare UUID — never a valid human title."""
    return bool(_UUID_RE.match((value or "").strip()))


def extract_title(content: str, fallback: str, doc_type: str = "policy") -> str:
    """Best human-readable title for a document.

    Order: HTML ``<h1>`` → markdown ``# `` heading → caller fallback →
    ``"Untitled <doc_type>"``. A candidate that is empty or shaped like a bare
    UUID is rejected so the detail page never shows the policy_id as the name.
    """
    def _ok(candidate: str) -> str | None:
        candidate = (candidate or "").strip()
        if candidate and not looks_like_uuid(candidate):
            return candidate
        return None

    m = re.search(r"<h1[^>]*>(.*?)</h1>", content or "", re.IGNORECASE | re.DOTALL)
    if m:
        title = _ok(re.sub(r"<[^>]+>", "", m.group(1)))
        if title:
            return title
    for line in (content or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = _ok(line[2:])
            if title:
                return title
    return _ok(fallback) or f"Untitled {doc_type}"
