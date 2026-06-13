"""Unit tests for the AI framework generation feature in policy_hub/routes.py.

Covers:
  * `_build_framework_prompt` / `_normalize_ai_framework` pure helpers
  * the async `generate_framework` route (Kimi 2.5 draft → unsaved preview)
  * `save_framework` provenance (source / ai_generated) + explicit columns
  * `_async_index_framework` delete-then-insert (idempotent re-index)

External deps (db, credits, fireworks, S3, lance, …) are stubbed before import
so the suite runs without AWS / network — same approach as
tests/policy_hub/test_upload_helpers.py. Flask async views are exercised by
calling the coroutine directly inside a request context (the test env has no
asgiref, so the test client can't dispatch async views).
"""

import asyncio
import json
import re
import sys
import types
from unittest.mock import MagicMock

import pytest
from flask import Flask

FRAMEWORK_OWNER = "service@bytoid.ca"


def _fake_extract_json_safe(text):
    """Faithful-enough stand-in for fireworkzz.extract_json_safe: parse JSON,
    tolerate ```json fences, return None on failure (never raises)."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```json", "", text)
        text = re.sub(r"^```", "", text)
        text = re.sub(r"```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        return None


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

    def _autostub(mod):
        # PEP 562 module __getattr__: any symbol we didn't set explicitly
        # resolves to a fresh MagicMock, so transitively-imported names
        # (pulled in by other policy_hub submodules) never break the import,
        # independent of test ordering.
        mod.__getattr__ = lambda name: MagicMock(name=f"{mod.__name__}.{name}")
        return mod

    for k in keys:
        if k == "policy_hub.routes":
            sys.modules.pop(k, None)
            continue
        sys.modules[k] = _autostub(types.ModuleType(k))

    sys.modules["pymysql.cursors"].DictCursor = MagicMock()
    sys.modules["db.rds_db"].connect_to_rds = MagicMock(return_value=None)
    sys.modules["db.db_checkers"].get_email_by_id = MagicMock(return_value=FRAMEWORK_OWNER)
    for sym in ("LanceDBServer", "VectorData", "QueryData"):
        setattr(sys.modules["db.lance_db_service"], sym, MagicMock())
    sys.modules["credits_route.route"].Credits = MagicMock()

    fw = sys.modules["utils.fireworkzz"]
    fw.get_fireworks_response2 = MagicMock()
    fw.get_firework_embedding = MagicMock()
    fw.get_think_fire_response2_og = MagicMock()
    fw.extract_json_safe = _fake_extract_json_safe
    fw.GUARDRAIL_BLOCKED = "BLOCKED_BY_GUARDRAIL"

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

    sys.modules["utils.app_configs"].FRAMEWORK_OWNER = FRAMEWORK_OWNER
    sys.modules["utils.app_configs"].policy_hub_v2_enabled = lambda u: True
    sys.modules["utils.app_configs"].statement_reid_threshold = lambda u: 0.5
    sys.modules["utils.app_configs"].MIGRATION_FIREWORKS_CONCURRENCY = 2

    audit = sys.modules["services.audit_log_service"]
    audit.log_audit_event = MagicMock()
    audit.build_audit_actor = MagicMock(return_value=(None, None, None, None))
    audit.POLICY_SHARED = "POLICY_SHARED"
    audit.POLICY_SHARE_REVOKED = "POLICY_SHARE_REVOKED"
    audit.POLICY_UPLOADED = "POLICY_UPLOADED"
    audit.TEMPLATE_REPLICATED = "TEMPLATE_REPLICATED"
    audit.TEMPLATE_EDITED = "TEMPLATE_EDITED"
    audit.TEMPLATE_RESET = "TEMPLATE_RESET"
    audit.TEMPLATE_APPLIED = "TEMPLATE_APPLIED"

    import policy_hub.routes as routes
    yield routes

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    sys.modules.pop("policy_hub.routes", None)


@pytest.fixture
def app():
    return Flask(__name__)


def _ok_async(value):
    async def _fn(prompt, user_id, credits):
        return value
    return _fn


# ───────────────────────── pure helpers ─────────────────────────

@pytest.mark.unit
class TestPromptHelper:
    def test_prompt_includes_name_and_json_contract(self, routes_module):
        p = routes_module._build_framework_prompt("ISO/IEC 27001:2022")
        assert "ISO/IEC 27001:2022" in p
        assert "JSON" in p
        assert '"columns"' in p and '"rows"' in p
        # Anti-hallucination instruction must be present.
        assert "fabricate" in p.lower() or "invent" in p.lower()


@pytest.mark.unit
class TestNormalize:
    def test_object_with_columns_and_rows(self, routes_module):
        parsed = {
            "columns": ["Reference", "Control"],
            "rows": [
                {"Reference": "A.5.1", "Control": "Policies"},
                {"Reference": "A.6.1", "Control": "Roles"},
            ],
        }
        cols, rows = routes_module._normalize_ai_framework(parsed)
        assert cols == ["Reference", "Control"]
        assert len(rows) == 2
        assert rows[0] == {"Reference": "A.5.1", "Control": "Policies"}

    def test_bare_list_of_dicts(self, routes_module):
        parsed = [{"Reference": "1.1", "Control": "x"}]
        cols, rows = routes_module._normalize_ai_framework(parsed)
        assert "Reference" in cols and "Control" in cols
        assert rows == [{"Reference": "1.1", "Control": "x"}]

    def test_extra_keys_become_columns(self, routes_module):
        parsed = {"columns": ["Reference"], "rows": [{"Reference": "1", "Extra": "y"}]}
        cols, rows = routes_module._normalize_ai_framework(parsed)
        assert "Reference" in cols and "Extra" in cols
        assert rows[0]["Extra"] == "y"

    def test_empty_rows_dropped_and_values_stringified(self, routes_module):
        parsed = {
            "columns": ["A", "B"],
            "rows": [
                {"A": "  ", "B": None},          # all-empty → dropped
                {"A": 12, "B": {"k": "v"}},      # coerced to str / json
            ],
        }
        _cols, rows = routes_module._normalize_ai_framework(parsed)
        assert len(rows) == 1
        assert rows[0]["A"] == "12"
        assert json.loads(rows[0]["B"]) == {"k": "v"}

    def test_garbage_returns_empty(self, routes_module):
        assert routes_module._normalize_ai_framework(None) == ([], [])
        assert routes_module._normalize_ai_framework("nope") == ([], [])
        assert routes_module._normalize_ai_framework({"rows": "x"}) == ([], [])


# ───────────────────────── generate route ─────────────────────────

@pytest.mark.unit
class TestGenerateRoute:
    def _call(self, app, routes, body):
        with app.test_request_context(json=body, method="POST"):
            return asyncio.run(routes.generate_framework())

    def test_success_returns_unsaved_preview(self, app, routes_module, monkeypatch):
        payload = json.dumps({
            "columns": ["Reference", "Domain", "Control", "Description"],
            "rows": [{"Reference": "A.5.1", "Domain": "Org", "Control": "Policy",
                      "Description": "Define security policies."}],
        })
        monkeypatch.setattr(routes_module, "get_think_fire_response2_og", _ok_async(payload))
        # Ensure nothing is persisted.
        write = MagicMock()
        monkeypatch.setattr(routes_module, "_write_framework_yaml", write)

        resp, status = self._call(app, routes_module, {"name": "ISO 27001", "user_id": "u1"})
        assert status == 200
        data = resp.get_json()
        assert data["ai_generated"] is True
        assert data["row_count"] == 1
        assert data["columns"][0] == "Reference"
        assert data["source_filename"].startswith("AI")
        write.assert_not_called()

    def test_missing_name_400(self, app, routes_module, monkeypatch):
        called = {"n": 0}

        async def _spy(*a, **k):
            called["n"] += 1
            return "{}"
        monkeypatch.setattr(routes_module, "get_think_fire_response2_og", _spy)
        _resp, status = self._call(app, routes_module, {"name": "  ", "user_id": "u1"})
        assert status == 400
        assert called["n"] == 0

    def test_insufficient_credits_402(self, app, routes_module, monkeypatch):
        monkeypatch.setattr(routes_module, "get_think_fire_response2_og", _ok_async("INSUFFICIENT"))
        _resp, status = self._call(app, routes_module, {"name": "PCI DSS", "user_id": "u1"})
        assert status == 402

    def test_guardrail_block_403(self, app, routes_module, monkeypatch):
        monkeypatch.setattr(
            routes_module, "get_think_fire_response2_og",
            _ok_async("BLOCKED_BY_GUARDRAIL: nope"),
        )
        _resp, status = self._call(app, routes_module, {"name": "X", "user_id": "u1"})
        assert status == 403

    def test_non_json_response_502(self, app, routes_module, monkeypatch):
        monkeypatch.setattr(
            routes_module, "get_think_fire_response2_og",
            _ok_async("I'm sorry, I cannot help with that."),
        )
        _resp, status = self._call(app, routes_module, {"name": "X", "user_id": "u1"})
        assert status == 502

    def test_valid_json_but_no_rows_502(self, app, routes_module, monkeypatch):
        monkeypatch.setattr(
            routes_module, "get_think_fire_response2_og",
            _ok_async('{"columns": ["A"], "rows": []}'),
        )
        _resp, status = self._call(app, routes_module, {"name": "X", "user_id": "u1"})
        assert status == 502

    def test_non_owner_denied_no_model_call(self, app, routes_module, monkeypatch):
        called = {"n": 0}

        async def _spy(*a, **k):
            called["n"] += 1
            return "{}"
        monkeypatch.setattr(routes_module, "get_think_fire_response2_og", _spy)
        monkeypatch.setattr(routes_module, "get_email_by_id", lambda _u: "intruder@evil.com")
        _resp, status = self._call(app, routes_module, {"name": "X", "user_id": "u1"})
        assert status == 403
        assert called["n"] == 0


# ───────────────────────── save_framework provenance ─────────────────────────

@pytest.mark.unit
class TestSaveProvenance:
    def _save(self, app, routes, body, monkeypatch):
        captured = {}
        monkeypatch.setattr(routes, "_read_framework_yaml", lambda key: None)
        monkeypatch.setattr(
            routes, "_write_framework_yaml",
            lambda key, rec: captured.update(record=rec, key=key),
        )
        monkeypatch.setattr(routes, "_lance_index_worker", lambda *a, **k: None)
        with app.test_request_context(json=body, method="POST"):
            _resp, status = routes.save_framework()
        return captured.get("record"), status

    def test_ai_provenance_persisted(self, app, routes_module, monkeypatch):
        rec, status = self._save(
            app, routes_module,
            {"name": "ISO 27001", "rows": [{"Reference": "A.5"}],
             "source": "ai", "ai_generated": True, "user_id": "u1"},
            monkeypatch,
        )
        assert status == 200
        assert rec["source"] == "ai"
        assert rec["ai_generated"] is True

    def test_defaults_when_omitted_regression(self, app, routes_module, monkeypatch):
        rec, status = self._save(
            app, routes_module,
            {"name": "Legacy FW", "rows": [{"Reference": "1.1"}], "user_id": "u1"},
            monkeypatch,
        )
        assert status == 200
        assert rec["source"] == "upload"
        assert rec["ai_generated"] is False

    def test_explicit_columns_preserved(self, app, routes_module, monkeypatch):
        rec, status = self._save(
            app, routes_module,
            {"name": "FW", "columns": ["Reference", "Domain", "Control", "Description"],
             "rows": [{"Reference": "1.1"}], "user_id": "u1"},
            monkeypatch,
        )
        assert status == 200
        assert rec["columns"] == ["Reference", "Domain", "Control", "Description"]


# ───────────────────────── idempotent re-index ─────────────────────────

@pytest.mark.unit
class TestReindexIdempotent:
    def _run_index(self, routes, monkeypatch, rows):
        calls = []

        class _Lance:
            def __init__(self):
                calls.append("init")

            async def delete_folder_async(self, user, fid):
                calls.append("delete")

            async def insert_batch(self, vectors):
                calls.append(("insert", len(vectors)))

        class _Emb:
            def embed_documents(self, texts):
                return [[0.1, 0.2, 0.3] for _ in texts]

        async def _get_emb():
            return _Emb()

        monkeypatch.setattr(routes, "LanceDBServer", _Lance)
        monkeypatch.setattr(routes, "get_firework_embedding", _get_emb)
        monkeypatch.setattr(routes, "VectorData", lambda **kw: kw)
        asyncio.run(routes._async_index_framework("fw-1", rows))
        return calls

    def test_delete_before_insert(self, routes_module, monkeypatch):
        calls = self._run_index(
            routes_module, monkeypatch,
            [{"Reference": "A.5.1", "Control": "Policy"}],
        )
        names = [c if isinstance(c, str) else c[0] for c in calls]
        assert "delete" in names
        assert "insert" in names
        assert names.index("delete") < names.index("insert")

    def test_delete_even_when_no_rows(self, routes_module, monkeypatch):
        calls = self._run_index(routes_module, monkeypatch, [])
        names = [c if isinstance(c, str) else c[0] for c in calls]
        assert "delete" in names
        assert "insert" not in names
