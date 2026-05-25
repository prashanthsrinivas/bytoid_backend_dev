"""Tests for the STANDARD_TEMPLATE added for the upload feature."""

from policy_hub.templates import (
    STANDARD_TEMPLATE,
    TEMPLATES,
    get_template,
    validate,
)


class TestStandardTemplateDefinition:
    def test_standard_template_registered(self):
        assert "standard" in TEMPLATES
        assert TEMPLATES["standard"] is STANDARD_TEMPLATE

    def test_get_template_returns_standard(self):
        t = get_template("standard")
        assert t is STANDARD_TEMPLATE

    def test_standard_template_has_required_sections(self):
        ids = {s.id for s in STANDARD_TEMPLATE}
        required = {
            "standard.header",
            "standard.purpose",
            "standard.scope",
            "standard.requirements",
            "standard.compliance",
            "standard.exceptions",
            "standard.references",
            "standard.revision_history",
        }
        assert required.issubset(ids)

    def test_standard_requirements_is_statements_kind(self):
        sec = next(s for s in STANDARD_TEMPLATE if s.id == "standard.requirements")
        assert sec.kind == "statements"

    def test_standard_header_is_header_table(self):
        sec = next(s for s in STANDARD_TEMPLATE if s.id == "standard.header")
        assert sec.kind == "header_table"

    def test_standard_revision_history_is_history(self):
        sec = next(s for s in STANDARD_TEMPLATE if s.id == "standard.revision_history")
        assert sec.kind == "history"

    def test_standard_definitions_is_optional(self):
        sec = next(s for s in STANDARD_TEMPLATE if s.id == "standard.definitions")
        assert sec.required is False

    def test_all_other_standard_sections_required(self):
        for sec in STANDARD_TEMPLATE:
            if sec.id == "standard.definitions":
                continue
            assert sec.required is True, f"{sec.id} should be required"


STANDARD_V2_HTML = """
<div data-section-id="standard.header">
  <h2>Document Header</h2>
  <table><tr><td>Standard Name</td><td>Cryptography Standard</td></tr></table>
</div>
<div data-section-id="standard.purpose">
  <h2>Purpose</h2>
  <p>Define cryptographic requirements.</p>
</div>
<div data-section-id="standard.scope">
  <h2>Scope</h2>
  <p>Applies to all data at rest and in transit.</p>
</div>
<div data-section-id="standard.requirements">
  <h2>Requirements</h2>
  <ul>
    <li data-statement-id="req-001">AES-256 must be used for data at rest.</li>
    <li data-statement-id="req-002">TLS 1.3 must be used for data in transit.</li>
  </ul>
</div>
<div data-section-id="standard.compliance">
  <h2>Compliance and Enforcement</h2>
  <p>Quarterly review by the Security team.</p>
</div>
<div data-section-id="standard.exceptions">
  <h2>Exceptions</h2>
  <p>Approved by CISO only.</p>
</div>
<div data-section-id="standard.references">
  <h2>Related Documents</h2>
  <p>NIST SP 800-175B.</p>
</div>
<div data-section-id="standard.revision_history">
  <h2>Review and Revision History</h2>
  <table><tr><td>1.0</td></tr></table>
</div>
"""


class TestStandardValidation:
    def test_well_formed_standard_validates(self):
        result = validate(STANDARD_V2_HTML, "standard")
        assert result.ok is True
        assert result.missing_sections == []
        assert result.empty_required == []
        assert result.statements_missing_ids == 0

    def test_missing_required_section_fails(self):
        html = STANDARD_V2_HTML.replace(
            '<div data-section-id="standard.requirements">',
            '<div data-section-id="standard.something_else">',
        )
        result = validate(html, "standard")
        assert result.ok is False
        assert "standard.requirements" in result.missing_sections

    def test_missing_optional_definitions_does_not_fail(self):
        # standard.definitions is not even present in the fixture — should still pass.
        result = validate(STANDARD_V2_HTML, "standard")
        assert result.ok is True

    def test_statement_without_id_counted(self):
        html = STANDARD_V2_HTML.replace(
            '<li data-statement-id="req-001">AES-256 must be used for data at rest.</li>',
            "<li>AES-256 must be used for data at rest.</li>",
        )
        result = validate(html, "standard")
        assert result.statements_missing_ids == 1
