"""Tests for the pure helpers added to policy_hub/routes.py for the upload feature.

We stub the heavy external deps (db, credits, fireworks, S3, lance) before
importing so the tests run in any environment that has pandas, pymysql, and
the standard pure-Python deps installed.
"""

import json
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def routes_module():
    """Import policy_hub.routes with external deps stubbed; restore on teardown."""
    keys = [
        "pymysql", "pymysql.cursors",
        "db", "db.rds_db", "db.db_checkers", "db.lance_db_service",
        "credits_route", "credits_route.route",
        "utils.fireworkzz", "utils.s3_utils", "utils.normal",
        "utils.permission_required", "utils.app_configs",
        "services.audit_log_service",
        "shared_configuration",
        "fitz", "pptx", "pptx.util",
        "policy_hub.routes",
    ]
    saved = {k: sys.modules.get(k) for k in keys}

    for k in keys:
        if k == "policy_hub.routes":
            sys.modules.pop(k, None)
            continue
        sys.modules[k] = types.ModuleType(k)

    sys.modules["pymysql.cursors"].DictCursor = MagicMock()
    sys.modules["db.rds_db"].connect_to_rds = MagicMock(return_value=None)
    sys.modules["db.db_checkers"].get_email_by_id = MagicMock(return_value=None)
    for sym in ("LanceDBServer", "VectorData", "QueryData"):
        setattr(sys.modules["db.lance_db_service"], sym, MagicMock())
    sys.modules["credits_route.route"].Credits = MagicMock()
    sys.modules["utils.fireworkzz"].get_fireworks_response2 = MagicMock()
    sys.modules["utils.fireworkzz"].get_firework_embedding = MagicMock()
    for sym in ("s3bucket", "load_yaml_from_s3", "read_json_from_s3",
                "delete_file_from_s3", "list_all_files"):
        setattr(sys.modules["utils.s3_utils"], sym, MagicMock())
    for sym in ("check_role_has_permission", "core_assign_resource",
                "core_list_resource_shares", "core_revoke_resource",
                "get_round_robin_user_for_resource", "get_user_resource_access",
                "get_user_shared_resources"):
        setattr(sys.modules["shared_configuration"], sym, MagicMock())
    sys.modules["utils.normal"].parse_composite_user_id = (
        lambda u: (None, u) if u else (None, None)
    )

    def _passthrough(perm):
        def deco(fn):
            return fn
        return deco
    sys.modules["utils.permission_required"].permission_required_body = _passthrough
    sys.modules["utils.permission_required"].permission_required = _passthrough

    sys.modules["utils.app_configs"].FRAMEWORK_OWNER = "service@bytoid.ca"
    sys.modules["utils.app_configs"].policy_hub_v2_enabled = lambda u: True
    sys.modules["utils.app_configs"].statement_reid_threshold = lambda u: 0.5
    sys.modules["utils.app_configs"].MIGRATION_FIREWORKS_CONCURRENCY = 2

    audit = sys.modules["services.audit_log_service"]
    audit.log_audit_event = MagicMock()
    audit.build_audit_actor = MagicMock()
    audit.POLICY_SHARED = "POLICY_SHARED"
    audit.POLICY_SHARE_REVOKED = "POLICY_SHARE_REVOKED"
    audit.POLICY_UPLOADED = "POLICY_UPLOADED"

    import policy_hub.routes as routes
    yield routes

    # Restore
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    sys.modules.pop("policy_hub.routes", None)


class TestRawFileKey:
    def test_with_leading_dot(self, routes_module):
        assert routes_module._raw_file_key("u1", "p1", ".pdf") == "u1/policies/raw/p1.pdf"

    def test_without_leading_dot(self, routes_module):
        assert routes_module._raw_file_key("u1", "p1", "docx") == "u1/policies/raw/p1.docx"

    def test_empty_ext(self, routes_module):
        assert routes_module._raw_file_key("u1", "p1", "") == "u1/policies/raw/p1"


class TestConstants:
    def test_allowed_extensions_include_target_formats(self, routes_module):
        assert routes_module.UPLOAD_ALLOWED_EXTENSIONS == {".pdf", ".docx", ".html", ".htm"}

    def test_max_bytes_is_25mb(self, routes_module):
        assert routes_module.UPLOAD_MAX_BYTES == 25 * 1024 * 1024

    def test_max_files_per_request(self, routes_module):
        assert routes_module.UPLOAD_MAX_FILES_PER_REQUEST == 20

    def test_valid_doc_types(self, routes_module):
        assert routes_module.VALID_DOC_TYPES == {"policy", "procedure", "standard"}

    def test_mime_type_mapping(self, routes_module):
        m = routes_module.UPLOAD_MIME_TYPES
        assert m[".pdf"] == "application/pdf"
        assert ".docx" in m
        assert m[".html"] == "text/html"
        assert m[".htm"] == "text/html"


class TestStripHtmlToText:
    def test_removes_tags(self, routes_module):
        out = routes_module._strip_html_to_text("<p>Hello <b>world</b></p>")
        assert "<" not in out
        assert "Hello world" in out

    def test_truncates_to_max_chars(self, routes_module):
        long_text = "<p>" + "x" * 5000 + "</p>"
        out = routes_module._strip_html_to_text(long_text, max_chars=200)
        assert len(out) == 200

    def test_collapses_whitespace(self, routes_module):
        out = routes_module._strip_html_to_text("<p>Hello\n\n\n  world</p>")
        assert out == "Hello world"

    def test_empty_input(self, routes_module):
        assert routes_module._strip_html_to_text("") == ""


class TestParseLlmJson:
    def test_plain_json(self, routes_module):
        out = routes_module._parse_llm_json('{"a": 1, "b": "hi"}')
        assert out == {"a": 1, "b": "hi"}

    def test_strips_code_fences(self, routes_module):
        raw = '```json\n{"a": 1}\n```'
        out = routes_module._parse_llm_json(raw)
        assert out == {"a": 1}

    def test_strips_generic_fences(self, routes_module):
        raw = '```\n{"a": 1}\n```'
        out = routes_module._parse_llm_json(raw)
        assert out == {"a": 1}

    def test_extracts_from_preamble(self, routes_module):
        raw = 'Here is your JSON:\n{"a": 1, "nested": {"b": 2}}\nThanks!'
        out = routes_module._parse_llm_json(raw)
        assert out == {"a": 1, "nested": {"b": 2}}

    def test_invalid_json_returns_none(self, routes_module):
        assert routes_module._parse_llm_json("not json at all") is None

    def test_empty_returns_none(self, routes_module):
        assert routes_module._parse_llm_json("") is None

    def test_non_string_returns_none(self, routes_module):
        assert routes_module._parse_llm_json(None) is None


class TestClassificationPrompt:
    def test_includes_document_content(self, routes_module):
        p = routes_module._classification_prompt("Sample policy content")
        assert "Sample policy content" in p

    def test_mentions_all_three_types(self, routes_module):
        p = routes_module._classification_prompt("x")
        for t in ("policy", "procedure", "standard"):
            assert t in p


class TestUploadExtractionPrompt:
    def test_for_policy(self, routes_module):
        p = routes_module._upload_extraction_prompt(
            "<p>content</p>", "policy", "test.pdf"
        )
        assert "policy.purpose" in p  # template section ids listed
        assert "policy.statements" in p
        assert "test.pdf" in p
        assert "pdf file" in p

    def test_for_procedure(self, routes_module):
        p = routes_module._upload_extraction_prompt(
            "<p>content</p>", "procedure", "x.docx"
        )
        assert "procedure.steps" in p
        assert "docx file" in p

    def test_for_standard(self, routes_module):
        p = routes_module._upload_extraction_prompt(
            "<p>content</p>", "standard", "y.html"
        )
        assert "standard.requirements" in p
        assert "html file" in p

    def test_includes_source_html(self, routes_module):
        p = routes_module._upload_extraction_prompt(
            "<p>my-source-marker</p>", "policy", "f.pdf"
        )
        assert "my-source-marker" in p

    def test_caps_source_at_80k(self, routes_module):
        huge = "<p>" + "a" * 100000 + "</p>"
        p = routes_module._upload_extraction_prompt(huge, "policy", "f.pdf")
        # The injected source is at most 80000 chars; whole prompt should be near that + overhead
        # Look at the SOURCE HTML section explicitly
        src = p.split("SOURCE HTML:\n", 1)[1].split("\n\nJSON:", 1)[0]
        assert len(src) <= 80000

    def test_unknown_doc_type_does_not_crash(self, routes_module):
        # Falls into the KeyError branch, returns prompt with "(unknown template)"
        p = routes_module._upload_extraction_prompt("<p>x</p>", "nonsense", "a.pdf")
        assert "(unknown template)" in p


class TestRenderUploadSectionsToHtml:
    def test_section_with_body_html(self, routes_module):
        sections = [
            {"id": "policy.purpose", "title": "Purpose", "kind": "text",
             "body_html": "<p>To define rules.</p>"}
        ]
        out = routes_module._render_upload_sections_to_html(sections)
        assert 'data-section-id="policy.purpose"' in out
        assert "<h2" in out
        assert "<p>To define rules.</p>" in out

    def test_section_with_statements(self, routes_module):
        sections = [{
            "id": "policy.statements",
            "title": "Policy Statements",
            "kind": "statements",
            "statements": [
                {"id": "s1", "text": "First."},
                {"id": "s2", "text": "Second."},
            ],
        }]
        out = routes_module._render_upload_sections_to_html(sections)
        assert 'data-statement-id="s1"' in out
        assert 'data-statement-id="s2"' in out
        assert "First." in out and "Second." in out

    def test_empty_sections_returns_empty(self, routes_module):
        assert routes_module._render_upload_sections_to_html([]) == ""

    def test_none_sections_handled(self, routes_module):
        assert routes_module._render_upload_sections_to_html(None) == ""

    def test_validates_against_template(self, routes_module):
        """Rendered HTML should pass the existing template validator."""
        from policy_hub.templates import validate
        sections = []
        for sec_id in [
            "standard.header", "standard.purpose", "standard.scope",
            "standard.compliance", "standard.exceptions",
            "standard.references", "standard.revision_history",
        ]:
            sections.append({
                "id": sec_id, "title": sec_id.split(".")[1], "kind": "text",
                "body_html": "<p>content</p>",
            })
        sections.append({
            "id": "standard.requirements", "title": "Requirements",
            "kind": "statements",
            "statements": [{"id": "r1", "text": "AES-256 required."}],
        })
        html = routes_module._render_upload_sections_to_html(sections)
        result = validate(html, "standard")
        assert result.ok is True


class TestClassifyDocTypeViaLlm:
    """The classifier is async; use a fresh event loop to drive it with a stubbed Fireworks call."""

    def _run(self, coro):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_policy_classification(self, routes_module, monkeypatch):
        async def fake_fw(**kwargs):
            return "policy"
        monkeypatch.setattr(routes_module, "get_fireworks_response2", fake_fw)
        out = self._run(routes_module._classify_doc_type_via_llm("<p>some content</p>"))
        assert out == "policy"

    def test_standard_classification(self, routes_module, monkeypatch):
        async def fake_fw(**kwargs):
            return "Standard"
        monkeypatch.setattr(routes_module, "get_fireworks_response2", fake_fw)
        out = self._run(routes_module._classify_doc_type_via_llm("<p>x</p>"))
        assert out == "standard"

    def test_unparseable_response_defaults_to_policy(self, routes_module, monkeypatch):
        async def fake_fw(**kwargs):
            return "I cannot classify this!"
        monkeypatch.setattr(routes_module, "get_fireworks_response2", fake_fw)
        out = self._run(routes_module._classify_doc_type_via_llm("<p>x</p>"))
        assert out == "policy"

    def test_llm_exception_defaults_to_policy(self, routes_module, monkeypatch):
        async def fake_fw(**kwargs):
            raise RuntimeError("API down")
        monkeypatch.setattr(routes_module, "get_fireworks_response2", fake_fw)
        out = self._run(routes_module._classify_doc_type_via_llm("<p>x</p>"))
        assert out == "policy"

    def test_empty_html_skips_call_and_defaults(self, routes_module, monkeypatch):
        called = {"n": 0}

        async def fake_fw(**kwargs):
            called["n"] += 1
            return "procedure"
        monkeypatch.setattr(routes_module, "get_fireworks_response2", fake_fw)
        out = self._run(routes_module._classify_doc_type_via_llm(""))
        assert out == "policy"
        assert called["n"] == 0  # no API call when input is empty


class TestEndpointsRegistered:
    def test_upload_endpoint_on_blueprint(self, routes_module):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(routes_module.policy_hub_bp)
        rules = {str(r.rule): sorted(r.methods - {"HEAD", "OPTIONS"}) for r in app.url_map.iter_rules()}
        assert "/policy-hub/upload" in rules
        assert "POST" in rules["/policy-hub/upload"]
        assert "/policy-hub/upload-status" in rules
        assert "GET" in rules["/policy-hub/upload-status"]
        assert "/policy-hub/download-raw" in rules
        assert "GET" in rules["/policy-hub/download-raw"]
