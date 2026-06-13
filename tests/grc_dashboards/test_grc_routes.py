"""Route / authorization tests for the GRC dashboard blueprint.

Heavy deps stubbed before importing the SUT (same pattern as tests/strategy).
The helper's metric functions are patched to canned dicts so we test routing,
auth gating and validation without DB/S3.
"""

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

_HEAVY = ["pymysql", "db", "db.rds_db", "boto3", "dotenv", "dbutils", "dbutils.pooled_db",
          "pptx", "bs4", "pytz", "yaml", "docx", "utils.s3_utils", "utils.celery_base",
          "utils.base_logger"]
for _m in _HEAVY:
    sys.modules.setdefault(_m, MagicMock(name=f"{_m}_stub"))
_cur = types.ModuleType("pymysql.cursors")
_cur.DictCursor = MagicMock(name="DictCursor")
sys.modules["pymysql.cursors"] = _cur
sys.modules["pymysql"].cursors = _cur
sys.modules.setdefault("utils.permission_resolver", MagicMock(name="permission_resolver_stub"))
sys.modules.setdefault("utils.permission_metadata", MagicMock(name="permission_metadata_stub"))

from flask import Flask  # noqa: E402

import grc_dashboards.helper as helper_mod  # noqa: E402
import utils.permission_required as perm_mod  # noqa: E402
from grc_dashboards.routes import grc_bp  # noqa: E402


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        # Decorator's user lookup → admin self-access.
        self._one = {"user_id": "admin1", "user_type": "admin", "launch_id_fk": "org1"}

    def fetchone(self):
        return getattr(self, "_one", None)

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConn:
    def cursor(self, *a, **k):
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@contextmanager
def wired():
    with patch.object(perm_mod, "connect_to_rds", return_value=FakeConn()):
        yield


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="t")
    app.register_blueprint(grc_bp)
    return app


def _client(app, user_id="admin1"):
    c = app.test_client()
    if user_id is not None:
        with c.session_transaction() as sess:
            sess["user_id"] = user_id
    return c


@pytest.mark.integration
def test_routes_registered():
    rules = {r.rule for r in make_app().url_map.iter_rules()}
    assert {"/governance/summary", "/risk/summary", "/compliance/summary"} <= rules


@pytest.mark.integration
@pytest.mark.authz
def test_unauthenticated_is_401():
    app = make_app()
    with wired():
        r = app.test_client().get("/governance/summary?user_id=admin1")
    assert r.status_code == 401


@pytest.mark.integration
def test_governance_summary_happy_path():
    app = make_app()
    with wired(), patch.object(
        helper_mod, "governance_summary", return_value={"generated_at": "now", "policies": {"total": 3}}
    ):
        r = _client(app).get("/governance/summary?user_id=admin1")
    assert r.status_code == 200
    assert r.get_json()["policies"]["total"] == 3


@pytest.mark.integration
def test_risk_and_compliance_summaries_respond():
    app = make_app()
    with wired(), \
        patch.object(helper_mod, "risk_summary", return_value={"generated_at": "now", "tracker_count": 5}), \
        patch.object(helper_mod, "compliance_summary", return_value={"generated_at": "now", "evidence_count": 9}):
        c = _client(app)
        assert c.get("/risk/summary?user_id=admin1").get_json()["tracker_count"] == 5
        assert c.get("/compliance/summary?user_id=admin1").get_json()["evidence_count"] == 9


@pytest.mark.integration
def test_missing_user_id_is_400():
    app = make_app()
    with wired():
        # session present (self-access), but no user_id in query/body
        r = _client(app).get("/governance/summary")
    assert r.status_code == 400
