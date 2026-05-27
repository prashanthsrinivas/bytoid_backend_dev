"""Pure doc-type knowledge shared across policy hub generation paths.

Kept stdlib-only so it is unit-testable without importing the heavy
``policy_hub.routes`` module.
"""

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


def statement_display_number(
    doc_ref: str | None,
    section_abbr: str | None,
    seq: int,
) -> str | None:
    """Compose a statement's human display number, e.g. ``ACC-0001.STM.3``.

    Returns ``None`` when the parent document has no ``doc_ref`` yet (legacy
    docs awaiting backfill) so callers can omit the number rather than render
    a malformed one. ``section_abbr`` defaults to ``STM`` when blank.
    """
    if not doc_ref:
        return None
    abbr = (section_abbr or "STM").strip() or "STM"
    return f"{doc_ref}.{abbr}.{seq}"


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
