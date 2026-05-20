"""Tests for policy_hub/templates.py — template definitions and validation."""

import pytest
from policy_hub.templates import (
    POLICY_TEMPLATE,
    PROCEDURE_TEMPLATE,
    get_template,
    validate,
    ValidationResult,
)
from tests.conftest import POLICY_V2_HTML


class TestTemplateDefinitions:
    def test_policy_template_has_required_sections(self):
        ids = {s.id for s in POLICY_TEMPLATE}
        required = {
            "policy.header",
            "policy.purpose",
            "policy.scope",
            "policy.statements",
            "policy.roles",
            "policy.compliance",
            "policy.exceptions",
            "policy.related_documents",
            "policy.revision_history",
        }
        assert required.issubset(ids)

    def test_procedure_template_has_required_sections(self):
        ids = {s.id for s in PROCEDURE_TEMPLATE}
        required = {
            "procedure.header",
            "procedure.purpose",
            "procedure.scope",
            "procedure.prerequisites",
            "procedure.roles",
            "procedure.steps",
            "procedure.io",
            "procedure.exceptions",
            "procedure.evidence",
            "procedure.related_documents",
            "procedure.revision_history",
        }
        assert required.issubset(ids)

    def test_policy_statements_section_is_statements_kind(self):
        sec = next(s for s in POLICY_TEMPLATE if s.id == "policy.statements")
        assert sec.kind == "statements"

    def test_procedure_steps_section_is_steps_kind(self):
        sec = next(s for s in PROCEDURE_TEMPLATE if s.id == "procedure.steps")
        assert sec.kind == "steps"

    def test_header_sections_are_header_table_kind(self):
        policy_header = next(s for s in POLICY_TEMPLATE if s.id == "policy.header")
        proc_header = next(s for s in PROCEDURE_TEMPLATE if s.id == "procedure.header")
        assert policy_header.kind == "header_table"
        assert proc_header.kind == "header_table"

    def test_revision_history_sections_are_history_kind(self):
        policy_hist = next(
            s for s in POLICY_TEMPLATE if s.id == "policy.revision_history"
        )
        proc_hist = next(
            s for s in PROCEDURE_TEMPLATE if s.id == "procedure.revision_history"
        )
        assert policy_hist.kind == "history"
        assert proc_hist.kind == "history"

    def test_section_ids_are_unique_within_template(self):
        policy_ids = [s.id for s in POLICY_TEMPLATE]
        assert len(policy_ids) == len(set(policy_ids))
        proc_ids = [s.id for s in PROCEDURE_TEMPLATE]
        assert len(proc_ids) == len(set(proc_ids))

    def test_get_template_returns_policy_template(self):
        assert get_template("policy") is POLICY_TEMPLATE

    def test_get_template_returns_procedure_template(self):
        assert get_template("procedure") is PROCEDURE_TEMPLATE

    def test_get_template_raises_on_unknown(self):
        with pytest.raises(KeyError):
            get_template("report")

    def test_all_required_sections_are_required_true(self):
        # Spot-check: purpose, scope, statements must all be required
        required_ids = {
            "policy.purpose",
            "policy.scope",
            "policy.statements",
            "policy.roles",
        }
        for sec in POLICY_TEMPLATE:
            if sec.id in required_ids:
                assert sec.required is True, f"{sec.id} should be required"


class TestValidate:
    def test_fully_populated_policy_is_ok(self, policy_v2_html):
        result = validate(policy_v2_html, "policy")
        assert isinstance(result, ValidationResult)
        assert result.ok is True
        assert result.missing_sections == []
        assert result.empty_required == []

    def test_fully_populated_procedure_is_ok(self, procedure_v2_html):
        result = validate(procedure_v2_html, "procedure")
        assert result.ok is True

    def test_missing_required_section_sets_ok_false(self):
        # Remove the policy.statements section
        html = POLICY_V2_HTML.replace(
            '<div data-section-id="policy.statements">',
            '<div data-section-id="policy.REMOVED">',
        )
        result = validate(html, "policy")
        assert result.ok is False
        assert "policy.statements" in result.missing_sections

    def test_multiple_missing_sections_all_reported(self):
        html = "<div><p>no sections here</p></div>"
        result = validate(html, "policy")
        assert result.ok is False
        # All required sections should be in the missing list
        required_ids = {s.id for s in POLICY_TEMPLATE if s.required}
        assert set(result.missing_sections) == required_ids

    def test_statements_missing_ids_counted(self):
        # Replace data-statement-id with nothing on some li elements
        html = POLICY_V2_HTML.replace(
            '<li data-statement-id="stmt-001">',
            "<li>",
        ).replace(
            '<li data-statement-id="stmt-002">',
            "<li>",
        )
        result = validate(html, "policy")
        assert result.statements_missing_ids == 2

    def test_optional_section_not_flagged_as_missing(self):
        # policy.definitions is optional — removing it should not affect ok
        html = POLICY_V2_HTML.replace(
            '<div data-section-id="policy.definitions">',
            '<div data-section-id="policy.GONE">',
        )
        result = validate(html, "policy")
        assert "policy.definitions" not in result.missing_sections
