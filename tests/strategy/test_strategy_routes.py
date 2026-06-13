"""Route / authorization integration tests for the Strategy blueprint.

Heavy transitive deps (``db.rds_db`` hits AWS at import; ``pptx``/``bs4``/
``yaml``/``docx`` may be unavailable) are stubbed BEFORE importing the SUT —
the same pattern used by ``tests/security/authz/test_privilege_escalation.py``.
Per test we point every ``connect_to_rds`` alias at a fake connection whose
cursor answers by SQL shape.
"""

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# ── stub heavy modules before importing the SUT ───────────────────────────────
_HEAVY = [
    "pymysql", "db", "db.rds_db", "boto3", "dotenv", "dbutils",
    "dbutils.pooled_db", "pptx", "pptx.util", "bs4", "pytz", "yaml", "docx",
    "utils.s3_utils", "utils.celery_base", "utils.base_logger",
]
for _m in _HEAVY:
    sys.modules.setdefault(_m, MagicMock(name=f"{_m}_stub"))

_cursors = types.ModuleType("pymysql.cursors")
_cursors.DictCursor = MagicMock(name="DictCursor")
sys.modules["pymysql.cursors"] = _cursors
sys.modules["pymysql"].cursors = _cursors

sys.modules.setdefault("utils.permission_resolver", MagicMock(name="permission_resolver_stub"))
sys.modules.setdefault("utils.permission_metadata", MagicMock(name="permission_metadata_stub"))

from flask import Flask  # noqa: E402

import strategy.helper as helper_mod  # noqa: E402
import utils.permission_required as perm_mod  # noqa: E402
from strategy.routes import strategy_bp  # noqa: E402


# ── fake DB ───────────────────────────────────────────────────────────────────

OBJ_ROW = {
    "id": "obj1", "owner_user_id": "admin1", "org_id": "org1", "created_by": "admin1",
    "title": "My Objective", "description": None, "status": "draft",
    "start_date": None, "target_date": None, "created_at": None, "updated_at": None,
}


def default_router(sql, params):
    """Return (fetchone, fetchall) for a SQL string. params is a tuple/None."""
    s = sql.lower()
    p = params or ()
    if "company_name from users" in s:
        uid = p[0] if p else None
        # 'outsider' lives in a different org → owner-scope rejection.
        if uid == "outsider":
            return {"launch_id_fk": "org2", "company_name": None}, []
        return {"launch_id_fk": "org1", "company_name": None}, []
    if "from users" in s and "user_type" in s:
        return {"user_id": "admin1", "user_type": "admin", "launch_id_fk": "org1"}, []
    if "from strategic_objectives where id" in s:
        return OBJ_ROW, []
    if "from projects where id" in s:
        return None, []  # not found by default
    return None, []


class FakeCursor:
    def __init__(self, router):
        self._router = router
        self._one = None
        self._all = []
        self.rowcount = 1
        self.lastrowid = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._one, self._all = self._router(sql, params)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def close(self):
        pass


class FakeConn:
    def __init__(self, router):
        self._router = router

    def cursor(self, *args, **kwargs):
        return FakeCursor(self._router)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@contextmanager
def wired(router=default_router):
    conn = FakeConn(router)
    with patch.object(helper_mod, "connect_to_rds", return_value=conn), \
         patch.object(perm_mod, "connect_to_rds", return_value=conn):
        yield


def make_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app.register_blueprint(strategy_bp)
    return app


def _client(app, user_id="admin1"):
    c = app.test_client()
    if user_id is not None:
        with c.session_transaction() as sess:
            sess["user_id"] = user_id
    return c


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_all_routes_registered():
    app = make_app()
    rules = {r.rule for r in app.url_map.iter_rules()}
    for expected in (
        "/strategy/objectives",
        "/strategy/objective/<oid>",
        "/strategy/programs",
        "/strategy/projects",
        "/strategy/project/<pid>/link-doc",
        "/strategy/project/<pid>/drilldown",
        "/strategy/project/<pid>/health",
        "/strategy/roadmap",
        "/strategy/milestones",
    ):
        assert expected in rules


@pytest.mark.integration
@pytest.mark.authz
def test_unauthenticated_create_is_401():
    app = make_app()
    with wired():
        c = app.test_client()  # no session
        r = c.post("/strategy/objectives", json={"user_id": "admin1", "title": "x"})
    assert r.status_code == 401


@pytest.mark.integration
def test_create_objective_happy_path_201():
    app = make_app()
    with wired():
        c = _client(app)
        r = c.post("/strategy/objectives", json={"user_id": "admin1", "title": "My Objective"})
    assert r.status_code == 201
    assert r.get_json()["title"] == "My Objective"


@pytest.mark.integration
def test_create_objective_missing_title_400():
    app = make_app()
    with wired():
        c = _client(app)
        r = c.post("/strategy/objectives", json={"user_id": "admin1"})
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.authz
@pytest.mark.regression
def test_create_objective_cross_org_owner_rejected_403():
    app = make_app()
    with wired():
        c = _client(app)
        r = c.post(
            "/strategy/objectives",
            json={"user_id": "admin1", "title": "x", "owner_user_id": "outsider"},
        )
    assert r.status_code == 403


@pytest.mark.integration
def test_list_objectives_200_empty():
    app = make_app()
    with wired():
        c = _client(app)
        r = c.get("/strategy/objectives?user_id=admin1")
    assert r.status_code == 200
    assert r.get_json() == {"objectives": []}


@pytest.mark.integration
def test_get_unknown_project_404():
    app = make_app()
    with wired():
        c = _client(app)
        r = c.get("/strategy/project/ghost?user_id=admin1")
    assert r.status_code == 404


@pytest.mark.integration
def test_missing_user_id_400():
    app = make_app()
    with wired():
        c = _client(app)
        # session present (passes auth as self), but no user_id in body/query
        r = c.post("/strategy/objectives", json={"title": "x"})
    # decorator sees no requested owner → owner defaults to actor (admin1) → self
    # access allowed; handler then rejects the missing user_id with 400.
    assert r.status_code == 400
