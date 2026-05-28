"""Pure doc-type knowledge shared across policy hub generation paths.

Kept stdlib-only so it is unit-testable without importing the heavy
``policy_hub.routes`` module.
"""

import re

# The three first-class governance document types.
DOC_TYPES = ("policy", "procedure", "standard")

# Heading used for the normative-statement section in the generation prompt.
STMT_HEADINGS = {
    "policy": "Policy Statement",
    "procedure": "Procedure Steps",
    "standard": "Requirements",
}

# Heading used for the enforcement/compliance section in the generation prompt.
ENFORCE_HEADINGS = {
    "policy": "Enforcement",
    "procedure": "Compliance Monitoring",
    "standard": "Compliance and Enforcement",
}


def stmt_heading(doc_type: str) -> str:
    return STMT_HEADINGS.get(doc_type, "Procedure Steps")


def enforce_heading(doc_type: str) -> str:
    return ENFORCE_HEADINGS.get(doc_type, "Compliance Monitoring")


# Matches a stored doc_ref like ``ACC-0001`` / ``ACC-P0001`` / ``ACC2-0042``:
#   group 1 — everything up to and including the separating ``-``
#   group 2 — optional single-letter type marker (``P`` procedure, ``S`` standard)
#   group 3 — the numeric sequence (leading zeros consumed)
_DOC_REF_RE = re.compile(r"^(.+-)([PS]?)0*(\d+)$")


def display_doc_ref(doc_ref: str | None) -> str | None:
    """Render a stored ``doc_ref`` (``ACC-0001``) in the 3-digit display form (``ACC-001``).

    Stored refs are minted 4-digit wide for sortability; this is a pure
    presentation shim. Refs that don't match the canonical pattern pass
    through unchanged so unknown/legacy values aren't silently mangled.
    Seq values >= 1000 are emitted at their natural width.
    """
    if not doc_ref:
        return doc_ref
    m = _DOC_REF_RE.match(doc_ref)
    if not m:
        return doc_ref
    prefix, type_marker, seq = m.groups()
    return f"{prefix}{type_marker}{int(seq):03d}"


def statement_display_number(
    doc_ref: str | None,
    section_abbr: str | None,
    seq: int,
) -> str | None:
    """Compose a statement's human display number, e.g. ``ACC-001-003``.

    Returns ``None`` when the parent document has no ``doc_ref`` yet (legacy
    docs awaiting backfill) so callers can omit the number rather than render
    a malformed one. ``section_abbr`` is accepted for call-site compatibility
    but no longer rendered.
    """
    del section_abbr
    if not doc_ref:
        return None
    return f"{display_doc_ref(doc_ref)}-{int(seq):03d}"


def enumeration_type_filter(doc_type: str | None) -> str:
    """Map the requested tab to an enumeration instruction.

    Unspecified / ``"all"`` enumerates the full triad; a specific type
    narrows to just that type.
    """
    dt = (doc_type or "all").strip().lower()
    if dt == "policy":
        return "Include ONLY policies."
    if dt == "procedure":
        return "Include ONLY procedures."
    if dt == "standard":
        return "Include ONLY standards."
    return "Include policies, procedures, and standards."
