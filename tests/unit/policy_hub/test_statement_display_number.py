"""Unit tests for statement display-number composition and section abbr map."""

import pytest

from policy_hub.doc_types import statement_display_number
from policy_hub.templates import section_abbr_map


@pytest.mark.unit
class TestStatementDisplayNumber:
    def test_basic(self):
        assert statement_display_number("ACC-0001", "STM", 3) == "ACC-0001.STM.3"

    def test_standard_requirements(self):
        assert statement_display_number("ENC-S0001", "REQ", 1) == "ENC-S0001.REQ.1"

    def test_procedure_steps(self):
        assert statement_display_number("CHG-P0002", "STP", 7) == "CHG-P0002.STP.7"

    def test_missing_doc_ref_returns_none(self):
        assert statement_display_number(None, "STM", 3) is None
        assert statement_display_number("", "STM", 3) is None

    def test_blank_abbr_defaults_to_stm(self):
        assert statement_display_number("ACC-0001", "", 2) == "ACC-0001.STM.2"
        assert statement_display_number("ACC-0001", None, 2) == "ACC-0001.STM.2"

    def test_seq_zero(self):
        assert statement_display_number("ACC-0001", "STM", 0) == "ACC-0001.STM.0"

    def test_does_not_crash_on_large_seq(self):
        assert statement_display_number("ACC-0001", "STM", 9999) == "ACC-0001.STM.9999"


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
