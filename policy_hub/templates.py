"""
Section template definitions for Policy Hub V2 documents.

Each SectionDef describes one required section in a generated document.
  id          — stable identifier used in data-section-id HTML attributes and
                statement-mapping storage; never changes after initial definition.
  title       — human-readable heading rendered as <h2>.
  kind        — how the section content is structured:
                  "text"         plain prose paragraphs
                  "statements"   list of individually addressable policy statements
                  "steps"        numbered procedure steps
                  "header_table" document-metadata table (title, version, etc.)
                  "history"      revision-history table
  required    — if True the template validator treats absence as an error.
  prompt_help — one sentence injected into the LLM generation prompt to
                guide content for this section.
"""

from dataclasses import dataclass, field
from typing import Literal


SectionKind = Literal["text", "statements", "steps", "header_table", "history"]


@dataclass(frozen=True)
class SectionDef:
    id: str
    title: str
    kind: SectionKind
    required: bool = True
    prompt_help: str = ""


POLICY_TEMPLATE: list[SectionDef] = [
    SectionDef(
        id="policy.header",
        title="Document Header",
        kind="header_table",
        prompt_help=(
            "Render as a <table> with rows for: Policy Name, Document ID, "
            "Version, Effective Date, Classification."
        ),
    ),
    SectionDef(
        id="policy.purpose",
        title="Purpose",
        kind="text",
        prompt_help="State the rationale for this policy: its intent and objectives.",
    ),
    SectionDef(
        id="policy.scope",
        title="Scope",
        kind="text",
        prompt_help=(
            "Describe applicability: roles, systems, entities, and geographic coverage."
        ),
    ),
    SectionDef(
        id="policy.statements",
        title="Policy Statements",
        kind="statements",
        prompt_help=(
            "Write normative rules using 'must', 'shall', or 'is prohibited'. "
            "Each distinct directive belongs in its own <li data-statement-id> element."
        ),
    ),
    SectionDef(
        id="policy.roles",
        title="Roles and Responsibilities",
        kind="text",
        prompt_help="Identify owners, enforcement parties, and accountability lines.",
    ),
    SectionDef(
        id="policy.compliance",
        title="Compliance and Enforcement",
        kind="text",
        prompt_help=(
            "Describe consequences of non-compliance and how violations are handled."
        ),
    ),
    SectionDef(
        id="policy.exceptions",
        title="Exceptions",
        kind="text",
        prompt_help="Explain the process for requesting and approving deviations.",
    ),
    SectionDef(
        id="policy.related_documents",
        title="Related Documents",
        kind="text",
        prompt_help=(
            "List linked standards, procedures, and framework references "
            "(e.g. ISO 27001 Annex A controls, NIST control IDs)."
        ),
    ),
    SectionDef(
        id="policy.definitions",
        title="Definitions / Glossary",
        kind="text",
        required=False,
        prompt_help="Define key terminology used in this document.",
    ),
    SectionDef(
        id="policy.revision_history",
        title="Review and Revision History",
        kind="history",
        prompt_help=(
            "Render as a <table> with columns: Version, Date, Author, Summary of Changes."
        ),
    ),
]

PROCEDURE_TEMPLATE: list[SectionDef] = [
    SectionDef(
        id="procedure.header",
        title="Document Header",
        kind="header_table",
        prompt_help=(
            "Render as a <table> with rows for: Procedure Name, Document ID, "
            "Version, Effective Date, Classification."
        ),
    ),
    SectionDef(
        id="procedure.purpose",
        title="Purpose",
        kind="text",
        prompt_help="State the operational objective of this procedure.",
    ),
    SectionDef(
        id="procedure.scope",
        title="Scope",
        kind="text",
        prompt_help="Describe the conditions and contexts in which this procedure applies.",
    ),
    SectionDef(
        id="procedure.prerequisites",
        title="Prerequisites",
        kind="text",
        prompt_help=(
            "List required tools, access rights, permissions, or prior knowledge "
            "needed before starting."
        ),
    ),
    SectionDef(
        id="procedure.roles",
        title="Roles",
        kind="text",
        prompt_help=(
            "Assign responsibility per step using a RACI matrix where complexity warrants."
        ),
    ),
    SectionDef(
        id="procedure.steps",
        title="Procedure Steps",
        kind="steps",
        prompt_help=(
            "Write numbered, sequential, actionable instructions. "
            "Each step belongs in its own <li data-statement-id> element."
        ),
    ),
    SectionDef(
        id="procedure.io",
        title="Inputs and Outputs",
        kind="text",
        prompt_help="Identify triggers (inputs) and resulting artifacts or records (outputs).",
    ),
    SectionDef(
        id="procedure.exceptions",
        title="Exception Handling",
        kind="text",
        prompt_help="Describe responses to step failures and edge cases.",
    ),
    SectionDef(
        id="procedure.evidence",
        title="Evidence / Records",
        kind="text",
        prompt_help="List items logged or retained for audit purposes.",
    ),
    SectionDef(
        id="procedure.related_documents",
        title="Related Documents",
        kind="text",
        prompt_help=(
            "Reference the parent policy, related procedures, and templates."
        ),
    ),
    SectionDef(
        id="procedure.revision_history",
        title="Revision History",
        kind="history",
        prompt_help=(
            "Render as a <table> with columns: Version, Date, Author, Summary of Changes."
        ),
    ),
]

STANDARD_TEMPLATE: list[SectionDef] = [
    SectionDef(
        id="standard.header",
        title="Document Header",
        kind="header_table",
        prompt_help=(
            "Render as a <table> with rows for: Standard Name, Document ID, "
            "Version, Effective Date, Classification."
        ),
    ),
    SectionDef(
        id="standard.purpose",
        title="Purpose",
        kind="text",
        prompt_help="State the rationale for this standard: its intent and the technical objective it codifies.",
    ),
    SectionDef(
        id="standard.scope",
        title="Scope",
        kind="text",
        prompt_help=(
            "Describe applicability: systems, technologies, roles, and contexts in which this standard applies."
        ),
    ),
    SectionDef(
        id="standard.requirements",
        title="Requirements",
        kind="statements",
        prompt_help=(
            "Write normative, measurable requirements using 'must', 'shall', or 'is required'. "
            "Each distinct requirement belongs in its own <li data-statement-id> element."
        ),
    ),
    SectionDef(
        id="standard.compliance",
        title="Compliance and Enforcement",
        kind="text",
        prompt_help=(
            "Describe how compliance is measured, audited, and enforced; "
            "include consequences for non-compliance."
        ),
    ),
    SectionDef(
        id="standard.exceptions",
        title="Exceptions",
        kind="text",
        prompt_help="Explain the process for requesting and approving deviations from this standard.",
    ),
    SectionDef(
        id="standard.definitions",
        title="Definitions / Glossary",
        kind="text",
        required=False,
        prompt_help="Define key terminology used in this standard.",
    ),
    SectionDef(
        id="standard.references",
        title="Related Documents",
        kind="text",
        prompt_help=(
            "Reference the parent policy, related procedures, and external framework controls "
            "(e.g. ISO 27001 Annex A, NIST SP 800-53)."
        ),
    ),
    SectionDef(
        id="standard.revision_history",
        title="Review and Revision History",
        kind="history",
        prompt_help=(
            "Render as a <table> with columns: Version, Date, Author, Summary of Changes."
        ),
    ),
]

TEMPLATES: dict[str, list[SectionDef]] = {
    "policy": POLICY_TEMPLATE,
    "procedure": PROCEDURE_TEMPLATE,
    "standard": STANDARD_TEMPLATE,
}


def get_template(doc_type: str) -> list[SectionDef]:
    """Return the template for *doc_type*, raising KeyError if unknown."""
    return TEMPLATES[doc_type]


@dataclass
class ValidationResult:
    ok: bool
    missing_sections: list[str] = field(default_factory=list)
    empty_required: list[str] = field(default_factory=list)
    statements_missing_ids: int = 0


def validate(content_html: str, doc_type: str) -> ValidationResult:
    """Validate *content_html* against the template for *doc_type*.

    Returns a ValidationResult. Does not raise on failure — callers decide
    whether to block or surface a warning.
    """
    from bs4 import BeautifulSoup

    template = get_template(doc_type)
    soup = BeautifulSoup(content_html, "lxml")

    # Build index of present section ids from data-section-id attributes
    present: set[str] = set()
    for tag in soup.find_all(attrs={"data-section-id": True}):
        present.add(tag["data-section-id"])

    missing: list[str] = []
    empty_required: list[str] = []
    statements_missing_ids = 0

    for sec in template:
        if sec.id not in present:
            if sec.required:
                missing.append(sec.id)
            continue

        # Check for empty required sections
        section_tag = soup.find(attrs={"data-section-id": sec.id})
        if sec.required and section_tag:
            text = section_tag.get_text(strip=True)
            if not text:
                empty_required.append(sec.id)

        # Count <li> elements missing data-statement-id in statements/steps sections
        if sec.kind in ("statements", "steps") and section_tag:
            for li in section_tag.find_all("li"):
                if not li.get("data-statement-id"):
                    statements_missing_ids += 1

    ok = not missing and not empty_required
    return ValidationResult(
        ok=ok,
        missing_sections=missing,
        empty_required=empty_required,
        statements_missing_ids=statements_missing_ids,
    )
