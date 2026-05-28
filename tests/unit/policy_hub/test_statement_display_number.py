"""Unit tests for statement display-number composition and section abbr map."""

import pytest

from policy_hub.doc_types import display_doc_ref, statement_display_number
from policy_hub.templates import section_abbr_map


@pytest.mark.unit
class TestStatementDisplayNumber:
    def test_basic(self):
        assert statement_display_number("ACC-0001", "STM", 3) == "ACC-001-003"

    def test_standard_requirements(self):
        assert statement_display_number("ENC-S0001", "REQ", 1) == "ENC-S001-001"

    def test_procedure_steps(self):
        assert statement_display_number("CHG-P0002", "STP", 7) == "CHG-P002-007"

    def test_missing_doc_ref_returns_none(self):
        assert statement_display_number(None, "STM", 3) is None
        assert statement_display_number("", "STM", 3) is None

    def test_abbr_is_ignored(self):
        # section_abbr is accepted for backward compat but no longer rendered
        assert statement_display_number("ACC-0001", "", 2) == "ACC-001-002"
        assert statement_display_number("ACC-0001", None, 2) == "ACC-001-002"
        assert statement_display_number("ACC-0001", "STM", 2) == "ACC-001-002"
        assert statement_display_number("ACC-0001", "REQ", 2) == "ACC-001-002"

    def test_seq_zero(self):
        assert statement_display_number("ACC-0001", "STM", 0) == "ACC-001-000"

    def test_does_not_crash_on_large_seq(self):
        # seq values past 3 digits keep their natural width
        assert statement_display_number("ACC-0001", "STM", 9999) == "ACC-001-9999"


@pytest.mark.unit
class TestDisplayDocRef:
    def test_policy(self):
        assert display_doc_ref("ACC-0001") == "ACC-001"

    def test_procedure(self):
        assert display_doc_ref("ACC-P0001") == "ACC-P001"

    def test_standard(self):
        assert display_doc_ref("ENC-S0042") == "ENC-S042"

    def test_salted_prefix(self):
        assert display_doc_ref("ACC2-0001") == "ACC2-001"

    def test_seq_past_three_digits_preserved(self):
        assert display_doc_ref("ACC-1234") == "ACC-1234"
        assert display_doc_ref("ACC-P1000") == "ACC-P1000"

    def test_none_and_blank_pass_through(self):
        assert display_doc_ref(None) is None
        assert display_doc_ref("") == ""

    def test_unrecognised_pattern_passes_through(self):
        # legacy / malformed refs are returned unchanged rather than mangled
        assert display_doc_ref("no-trailing-digits") == "no-trailing-digits"
        assert display_doc_ref("ACC") == "ACC"


@pytest.mark.unit
class TestSectionAbbrMap:
    def test_policy_statements_abbr(self):
        m = section_abbr_map("policy")
        assert m["policy.statements"] == "STM"

    def test_procedure_steps_abbr(self):
        m = section_abbr_map("procedure")
        assert m["procedure.steps"] == "STP"

    def test_standard_requirements_abbr(self):
        m = section_abbr_map("standard")
        assert m["standard.requirements"] == "REQ"

    def test_only_statement_bearing_sections_included(self):
        m = section_abbr_map("policy")
        # prose sections like policy.purpose must not appear
        assert "policy.purpose" not in m
        assert "policy.header" not in m
        assert set(m) == {"policy.statements"}
