"""Shared fixtures and import stubs for ai_governance unit tests.

All heavy AI libraries (and the DB / AWS clients they pull in) are stubbed at
module level BEFORE any test imports ai_governance code.  This matches the
pattern used in tests/unit/ and tests/security/ conftest files throughout
this repo, and ensures the test suite runs without the packages from
requirements-ai-governance.txt installed.
"""

import sys
from unittest.mock import MagicMock

import pytest

# ── Stub all AI governance packages ──────────────────────────────────────────

_AI_STUBS = [
    "nemoguardrails",
    "nemoguardrails.rails",
    "langfuse",
    "mlflow",
    "mlflow.pyfunc",
    "giskard",
    "trulens",
    "trulens.core",
    "trulens.core.schema",
    "trulens.core.schema.feedback",
    "deepeval",
    "deepeval.metrics",
    "deepeval.test_case",
    "aif360",
    "aif360.datasets",
    "aif360.metrics",
    "fairlearn",
    "fairlearn.reductions",
    "aequitas",
    "aequitas.group",
    "aequitas.bias",
    "aequitas.fairness",
    "shap",
    "sklearn",
    "sklearn.linear_model",
]

for _mod in _AI_STUBS:
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# ── Stub DB and AWS dependencies ──────────────────────────────────────────────

_mock_conn = MagicMock()
_mock_cursor = MagicMock()
_mock_cursor.__enter__ = lambda s: s
_mock_cursor.__exit__ = MagicMock(return_value=False)
_mock_conn.cursor.return_value = _mock_cursor
_mock_cursor.fetchone.return_value = None

_rds_stub = MagicMock()
_rds_stub.connect_to_rds = MagicMock(return_value=_mock_conn)
sys.modules.setdefault("db", MagicMock())
sys.modules["db.rds_db"] = _rds_stub
sys.modules.setdefault("db.db_checkers", MagicMock())
sys.modules.setdefault("pymysql", MagicMock())
sys.modules.setdefault("pymysql.cursors", MagicMock())

# DictCursor stub — rbac.py uses it as a constructor argument
import pymysql  # noqa: E402 (already stubbed above)
pymysql.cursors = MagicMock()
pymysql.cursors.DictCursor = dict

# ── Stub s3_utils (pulled in by audit_log_service) ────────────────────────────

_s3_stub = MagicMock()
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules["utils.s3_utils"] = _s3_stub

# ── Stub utils.normal (pulled in by audit_log_service via parse_composite_user_id) ──
# utils/normal.py imports pptx, docx, pytz at module level — stub them first,
# then provide a real parse_composite_user_id so RBAC can split ##SU## IDs.
for _mod in ("pptx", "pptx.util", "docx", "pytz"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

_normal_stub = MagicMock()


def _parse_composite(user_id: str):
    parts = str(user_id).split("##SU##")
    logged_in = parts[0]
    acting_as = parts[1] if len(parts) > 1 else parts[0]
    return logged_in, acting_as


_normal_stub.parse_composite_user_id = _parse_composite
sys.modules["utils.normal"] = _normal_stub

# ── Stub utils.celery_base (avoids the fireworks → apiConnector import chain) ──
# celery.task() must work as a decorator factory so ai_governance/tasks.py loads.
# Each decorated function gets a .delay() attribute (MagicMock by default).
class _FakeCeleryTask:
    """Minimal stand-in for a Celery task so .delay() exists on the function."""
    def __init__(self, fn):
        self._fn = fn
        self.delay = MagicMock(return_value=MagicMock(id="stub-task-id"))

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _FakeCelery:
    def task(self, *_a, **_kw):
        def decorator(fn):
            return _FakeCeleryTask(fn)
        return decorator


_celery_base_stub = MagicMock()
_celery_base_stub.celery = _FakeCelery()
sys.modules["utils.celery_base"] = _celery_base_stub

# Also stub the services/audit_log_service action constants and chains pulled
# in transitively via celery_base when tasks.py imports them lazily at call
# time — they are already handled by the real audit_log_service import path
# above, but stub the transitive deps it may still need at import time.
for _mod in ("fireworks", "fireworks.client", "utils.fireworkzz"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

# ── Stub dotenv (pulled in by utils.app_configs) ──────────────────────────────
sys.modules.setdefault("dotenv", MagicMock())

# ── Flask test app ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def ai_gov_app():
    """Minimal Flask app with the ai_governance blueprint registered."""
    from flask import Flask
    from ai_governance.routes import ai_governance_bp

    app = Flask(__name__)
    app.secret_key = "test-secret-key"  # noqa: S105
    app.register_blueprint(ai_governance_bp)
    return app


@pytest.fixture
def client(ai_gov_app):
    with ai_gov_app.test_client() as c:
        yield c


# ── RBAC user fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_service_user(monkeypatch):
    """Patch _fetch_user_row to return the service@bytoid.ca super account."""
    from ai_governance.middleware import rbac

    monkeypatch.setattr(
        rbac,
        "_fetch_user_row",
        lambda uid: {"user_type": "admin", "email": "service@bytoid.ca"},
    )


@pytest.fixture
def mock_admin_user(monkeypatch):
    """Patch _fetch_user_row to return a regular admin."""
    from ai_governance.middleware import rbac

    monkeypatch.setattr(
        rbac,
        "_fetch_user_row",
        lambda uid: {"user_type": "admin", "email": "admin@tenant.com"},
    )


@pytest.fixture
def mock_regular_user(monkeypatch):
    """Patch _fetch_user_row to return a non-admin user."""
    from ai_governance.middleware import rbac

    monkeypatch.setattr(
        rbac,
        "_fetch_user_row",
        lambda uid: {"user_type": "user", "email": "user@tenant.com"},
    )


@pytest.fixture
def mock_db_failure(monkeypatch):
    """Patch _fetch_user_row to simulate a DB failure (returns None)."""
    from ai_governance.middleware import rbac

    monkeypatch.setattr(rbac, "_fetch_user_row", lambda uid: None)
