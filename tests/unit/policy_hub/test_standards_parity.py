"""Standards-parity tests: doc-type knowledge, workflow allowlist, title key."""

import sys
from unittest.mock import MagicMock

import pytest

from policy_hub.doc_types import (
    DOC_TYPES,
    enforce_heading,
    enumeration_type_filter,
    stmt_heading,
)


@pytest.mark.unit
class TestDocTypeHeadings:
    def test_standard_is_a_doc_type(self):
        assert "standard" in DOC_TYPES
        assert set(DOC_TYPES) == {"policy", "procedure", "standard"}

    def test_stmt_heading_per_type(self):
        assert stmt_heading("policy") == "Policy Statement"
        assert stmt_heading("procedure") == "Procedure Steps"
        assert stmt_heading("standard") == "Requirements"

    def test_enforce_heading_per_type(self):
        assert enforce_heading("policy") == "Enforcement"
        assert enforce_heading("standard") == "Compliance and Enforcement"

    def test_unknown_type_defaults(self):
        assert stmt_heading("mystery") == "Procedure Steps"
        assert enforce_heading("mystery") == "Compliance Monitoring"


@pytest.mark.unit
class TestEnumerationTypeFilter:
    def test_all_covers_triad(self):
        assert enumeration_type_filter(None) == "Include policies, procedures, and standards."
        assert enumeration_type_filter("all") == "Include policies, procedures, and standards."

    def test_specific_types_narrow(self):
        assert enumeration_type_filter("policy") == "Include ONLY policies."
        assert enumeration_type_filter("procedure") == "Include ONLY procedures."
        assert enumeration_type_filter("standard") == "Include ONLY standards."

    def test_case_insensitive(self):
        assert enumeration_type_filter("STANDARD") == "Include ONLY standards."


@pytest.mark.unit
class TestWorkflowAllowlist:
    def test_standard_is_workflow_supported(self):
        # workflow_autosubmit imports utils.app_configs (IS_DEV) and
        # utils.base_logger. Other heavy tests in the tree stub these and may
        # not restore them cleanly when their own imports fail, so install
        # minimal real-shaped stubs here and force a clean reimport, then
        # restore — keeps this test order-independent.
        import logging
        import types

        keys = ("utils.app_configs", "utils.base_logger", "policy_hub.workflow_autosubmit")
        saved = {k: sys.modules.get(k) for k in keys}
        try:
            for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db"):
                sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

            ac = types.ModuleType("utils.app_configs")
            ac.IS_DEV = False
            sys.modules["utils.app_configs"] = ac

            bl = types.ModuleType("utils.base_logger")
            bl.get_logger = lambda *a, **k: logging.getLogger("test")
            sys.modules["utils.base_logger"] = bl

            sys.modules.pop("policy_hub.workflow_autosubmit", None)
            import policy_hub.workflow_autosubmit as wa

            assert "standard" in wa.WORKFLOW_SUPPORTED_DOC_TYPES
            assert set(wa.WORKFLOW_SUPPORTED_DOC_TYPES) == {"policy", "procedure", "standard"}
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


@pytest.mark.unit
class TestStandardTitleExtraction:
    def test_standard_name_maps_to_title(self):
        from policy_hub.structured import parse_document_html

        html = """
        <div data-section-id="standard.header">
          <h2>Document Header</h2>
          <table>
            <tr><td>Standard Name</td><td>Password Standard</td></tr>
            <tr><td>Document ID</td><td>STD-001</td></tr>
            <tr><td>Version</td><td>1.0</td></tr>
          </table>
        </div>
        <div data-section-id="standard.requirements">
          <h2>Requirements</h2>
          <ul>
            <li data-statement-id="r1">Passwords must be at least 12 characters.</li>
          </ul>
        </div>
        """
        parsed = parse_document_html(html, "standard")
        assert parsed.metadata.get("title") == "Password Standard"
        # requirements section parsed its <li> as a statement
        req = next(s for s in parsed.sections if s.id == "standard.requirements")
        assert len(req.statements) == 1
