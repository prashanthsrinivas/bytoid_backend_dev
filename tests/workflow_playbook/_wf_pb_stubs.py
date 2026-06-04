"""Shared test infrastructure for the workflow-builder / playbook module.

Importing the workflow/playbook code pulls a transitive chain that touches
third-party packages NOT installed in the test env (``fireworks``, ``celery``,
``google``, ``pptx``, ``PIL``, ``langchain_*``, ``apscheduler``, ``pytz``) and
``db.rds_db`` (which hits AWS Secrets Manager at *import* time).

Design goal — **zero contamination of other test suites**:

* We never mock-replace a real *internal* module (``utils.normal``,
  ``utils.s3_utils``, ``services.*`` …). With the missing third-party packages
  stubbed, those import for real, so any test that needs the genuine article is
  unaffected.
* The only globally-faked names are (a) the 12 third-party packages that are
  *never importable* in this env anyway, and (b) ``db.rds_db`` (un-importable
  without AWS creds). Faking these is consistent with what every DB-touching
  test already needs.

Public API
----------
``install_stubs()``           idempotent; call once before importing the SUT.
``make_conn(...)``            build a fake PyMySQL connection.
``FakeCursor``                the cursor it returns (inspect ``.executed``).
``make_app(*blueprints)``     minimal Flask app with the blueprint(s) registered.
``allow_auth()`` / ``deny_auth(...)``   context managers toggling the
                              ``permission_required_body`` gate.
``mock_rds(conn)``            context manager pointing every ``connect_to_rds``
                              alias at a fake connection.
"""

from __future__ import annotations

import contextlib
import importlib.abc
import importlib.util
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

# ── 1. Permissive stub module + meta-path finder ──────────────────────────────

# Top-level third-party packages that are not installed in the test env. Any
# submodule of these (e.g. ``google.oauth2.credentials``) is fabricated on
# demand by the finder below, so we never have to enumerate sub-paths.
_STUB_ROOTS = {
    "pytz",
    "pptx",
    "PIL",
    "fireworks",
    "celery",
    "google",
    "googleapiclient",
    "langchain_openai",
    "langchain_fireworks",
    "langchain_core",
    "langchain_community",
    "apscheduler",
}


class _StubModule(types.ModuleType):
    """A module that behaves like a package and yields a MagicMock for any
    attribute — so ``from pkg.sub import Thing`` always resolves."""

    __path__: list[str] = []  # marks it importable as a package

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        val = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, val)
        return val


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Claims imports whose top-level package is a known-missing third party."""

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".", 1)[0]
        if top in _STUB_ROOTS:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return _StubModule(spec.name)

    def exec_module(self, module):
        return None


_INSTALLED = False


def install_stubs() -> types.ModuleType:
    """Install the meta-path finder + the ``db.rds_db`` fake. Idempotent.

    Returns the ``db.rds_db`` stub module so a caller can tweak its callables.
    """
    global _INSTALLED
    if not any(isinstance(f, _StubFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _StubFinder())

    # db.rds_db hits AWS Secrets Manager at import — fake it explicitly so the
    # *real* db package and db.db_checkers (which import from it) still load.
    if not isinstance(sys.modules.get("db.rds_db"), _StubModule):
        rds = _StubModule("db.rds_db")
        rds.connect_to_rds = MagicMock(name="connect_to_rds", return_value=MagicMock())
        rds.get_secret = MagicMock(name="get_secret", return_value={})
        rds.safe_execute = MagicMock(name="safe_execute", return_value=None)
        rds.get_cursor = MagicMock(name="get_cursor", return_value=MagicMock())
        sys.modules["db.rds_db"] = rds

    _INSTALLED = True
    return sys.modules["db.rds_db"]


# ── 1b. SUT-import bootstrap (order-independent against stub poisoning) ────────

def bootstrap_sut() -> None:
    """Prepare ``sys.modules`` so importing workflow/playbook code is robust to
    collection-order stub poisoning. Idempotent; call before importing the SUT.

    Handles the two module-load-time hazards beyond the base third-party stubs:
      * ``db.db_checkers`` — the SUT does ``from db.db_checkers import <many>`` at
        module load. Another suite may have replaced it with a bare stub missing
        some of those names, so our import fails purely on collection order. We pin
        a *permissive* stub (yields a mock for any name) so any name resolves,
        order-independently. Marked ``_wf_pb_stub`` so the pin is idempotent.
      * ``services.redis_service`` — ``get_redis`` stubbed (the real one needs a
        live Redis at the moment the SUT calls ``get_redis()`` at import).
    """
    install_stubs()

    import importlib

    # Some suites do ``sys.modules.setdefault("db", MagicMock())``, leaving ``db``
    # a non-package so ``from db.lance_db_service import ...`` fails with
    # "'db' is not a package". Restore the real ``db`` package (rds_db stays
    # stubbed by install_stubs). Idempotent.
    db_mod = sys.modules.get("db")
    if db_mod is not None and not hasattr(db_mod, "__path__"):
        sys.modules.pop("db", None)
    if not hasattr(sys.modules.get("db"), "__path__"):
        with contextlib.suppress(Exception):
            importlib.import_module("db")

    # Real third-party libs the SUT binds at import (``yaml``, ``bs4``) get
    # stubbed by other suites (a known stub-collision). A genuine module is a
    # ``ModuleType`` with a real ``__file__``; a MagicMock/stub is neither.
    # Restore the real ones before the warm-up binds them into helperzz.
    for _lib in ("yaml", "bs4"):
        _m = sys.modules.get(_lib)
        if _m is not None and (not isinstance(_m, types.ModuleType)
                               or not getattr(_m, "__file__", None)):
            sys.modules.pop(_lib, None)
            with contextlib.suppress(Exception):
                importlib.import_module(_lib)

    # Load the real ``services`` package first so that stubbing a submodule below
    # does not strand its siblings (audit_log_service, etc.).
    try:
        import services as _services
    except Exception:
        _services = None

    dbc = sys.modules.get("db.db_checkers")
    if not getattr(dbc, "_wf_pb_stub", False):
        mod = _StubModule("db.db_checkers")  # permissive: any attr → MagicMock
        mod._wf_pb_stub = True
        sys.modules["db.db_checkers"] = mod
        db_pkg = sys.modules.get("db")
        if db_pkg is not None:
            db_pkg.db_checkers = mod

    rs = sys.modules.get("services.redis_service")
    if not getattr(rs, "_wf_pb_stub", False):
        rs_mod = types.ModuleType("services.redis_service")
        rs_mod._wf_pb_stub = True
        # The redis service is async — its methods are awaited by callers (e.g.
        # the job-status route). Hand back a redis whose common ops are AsyncMocks
        # so `await redis.get(...)` works; ``get`` returns None (job not found).
        _redis = MagicMock(name="redis")
        for _m in ("get", "set", "delete", "exists", "expire", "hget", "hset"):
            setattr(_redis, _m, AsyncMock(name=f"redis.{_m}", return_value=None))
        rs_mod.get_redis = MagicMock(name="get_redis", return_value=_redis)
        sys.modules["services.redis_service"] = rs_mod
        if _services is not None:
            _services.redis_service = rs_mod

    # services.scheduler_service imports utils.celery_base → apiConnector →
    # runbook.utils → pandas, which is broken in this env. playbook.routes needs
    # SchedulerService at import; stub the module so the route blueprint loads.
    ss = sys.modules.get("services.scheduler_service")
    if not getattr(ss, "_wf_pb_stub", False):
        ss_mod = types.ModuleType("services.scheduler_service")
        ss_mod._wf_pb_stub = True
        ss_mod.SchedulerService = MagicMock(name="SchedulerService")
        sys.modules["services.scheduler_service"] = ss_mod
        if _services is not None:
            _services.scheduler_service = ss_mod

    # Eagerly import the heavy SUT chain in a clean, leaf-first order so each
    # module fully initializes and caches *now*, before any individual test
    # triggers a partial/circular import mid-chain (e.g. helperzz →
    # agent_route.doc_clarity → utils.chatopenzz → utils.s3_utils). Best-effort:
    # a genuine import error is left to surface at the test's own import line.
    import importlib

    for _name in (
        "utils.s3_utils",
        "utils.chatopenzz",
        "agent_route.doc_clarity",
        "playbook.helperzz",
        "services.workflow_service",
        "workflow_route.state_machine",
        "workflow_route.routes",
        "playbook.routes",
    ):
        # Best-effort warm-up; a genuine import error surfaces at the test's
        # own import line, so suppress here rather than masking it twice.
        with contextlib.suppress(Exception):
            importlib.import_module(_name)


# ── 2. Fake DB connection / cursor ────────────────────────────────────────────

_UNSET = object()


class FakeCursor:
    """A PyMySQL-cursor stand-in usable as a context manager or bare object.

    fetchone:
        * a ``list``  -> consumed one row per ``fetchone()`` call (then ``None``)
        * a ``dict``/``None``/scalar -> returned every call
    fetchall:
        * ``fetchall``      -> a fixed result set (list of rows) every call
        * ``fetchall_seq``  -> a list of result sets, one popped per call
    """

    def __init__(
        self,
        fetchone=_UNSET,
        fetchall=_UNSET,
        fetchall_seq=None,
        rowcount: int = 1,
        lastrowid: int = 1,
    ):
        self._one = fetchone
        self._all = fetchall
        self._all_seq = list(fetchall_seq) if fetchall_seq is not None else None
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.executed: list[tuple] = []  # (sql, params)

    # context-manager protocol
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # write side
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self.rowcount

    def executemany(self, sql, seq=None):
        self.executed.append((sql, list(seq or [])))
        return self.rowcount

    # read side
    def fetchone(self):
        if isinstance(self._one, list):
            return self._one.pop(0) if self._one else None
        return None if self._one is _UNSET else self._one

    def fetchall(self):
        if self._all_seq is not None:
            return self._all_seq.pop(0) if self._all_seq else []
        return [] if self._all is _UNSET else self._all

    def fetchmany(self, size=None):
        return self.fetchall()

    def close(self):
        return None

    # convenience for assertions
    @property
    def last_sql(self) -> str:
        return self.executed[-1][0] if self.executed else ""

    def all_sql(self) -> str:
        return "\n".join(sql for sql, _ in self.executed)


def make_conn(**cursor_kwargs) -> MagicMock:
    """Build a fake connection whose ``.cursor(...)`` yields a `FakeCursor`.

    The cursor is reused across ``conn.cursor()`` calls so ``.executed`` and
    sequential ``fetchone`` accumulate across every ``with`` block in a handler.
    Expose it for assertions via ``conn.fake_cursor``.
    """
    cur = FakeCursor(**cursor_kwargs)
    conn = MagicMock(name="conn")
    conn.cursor.return_value = cur
    conn.fake_cursor = cur
    conn.commit = MagicMock(name="commit")
    conn.rollback = MagicMock(name="rollback")
    conn.close = MagicMock(name="close")
    return conn


# ── 3. Flask app factory ──────────────────────────────────────────────────────

def make_app(*blueprints):
    """Minimal Flask app with the given blueprint(s) registered."""
    from flask import Flask

    app = Flask("wf_pb_test")
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "wf-pb-test-secret"  # noqa: S105 (test-only secret)
    for bp in blueprints:
        app.register_blueprint(bp)
    return app


# ── 4. Auth toggles ───────────────────────────────────────────────────────────

@contextlib.contextmanager
def allow_auth():
    """Make every ``permission_required_body`` gate pass (returns None)."""
    with patch("utils.permission_required._evaluate_access", return_value=None):
        yield


@contextlib.contextmanager
def deny_auth(status: int = 403, error: str = "Permission denied"):
    """Make every gate deny with ``(json, status)``.

    Runs inside a request context (the decorator calls it mid-request) so
    ``jsonify`` is safe.
    """
    from flask import jsonify

    def _deny(_required):
        return jsonify({"error": error}), status

    with patch("utils.permission_required._evaluate_access", side_effect=_deny):
        yield


@contextlib.contextmanager
def mock_rds(conn, *aliases: str):
    """Point ``connect_to_rds`` at ``conn`` for the given module aliases.

    Each module that does ``from db.rds_db import connect_to_rds`` gets its own
    binding, so patch them per-module. Always patches the canonical
    ``db.rds_db.connect_to_rds`` too.
    """
    stack = contextlib.ExitStack()
    stack.enter_context(patch("db.rds_db.connect_to_rds", return_value=conn))
    for alias in aliases:
        stack.enter_context(patch(f"{alias}.connect_to_rds", return_value=conn))
    try:
        yield conn
    finally:
        stack.close()
