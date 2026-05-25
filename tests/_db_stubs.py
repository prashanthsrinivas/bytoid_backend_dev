"""Shared opt-in fixture for stubbing the DB connection layer.

Importing ``db.rds_db`` triggers AWS Secrets Manager (no credentials in CI =
ImportError). The ``db_stubs`` fixture below injects MagicMock placeholders
for ``pymysql``, ``db``, ``db.rds_db``, and a few transitive heavy imports
into ``sys.modules`` for the duration of the test, then RESTORES the
original module table on teardown so other tests are unaffected.

Tests that need a clean import of ``workflow_route.*`` or modules in
``services/`` that pull in ``db.rds_db`` should add ``db_stubs`` to the
fixture chain.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def db_stubs():
    saved: dict[str, object | None] = {}
    keys = (
        "pymysql",
        "pymysql.cursors",
        "db",
        "db.rds_db",
    )
    for k in keys:
        saved[k] = sys.modules.get(k)

    # Install stubs
    sys.modules["pymysql"] = MagicMock(name="pymysql_stub")
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = MagicMock()
    sys.modules["pymysql.cursors"] = cursors_mod

    db_mod = types.ModuleType("db")
    sys.modules["db"] = db_mod
    rds_mod = types.ModuleType("db.rds_db")
    rds_mod.connect_to_rds = MagicMock(return_value=None)
    sys.modules["db.rds_db"] = rds_mod

    try:
        yield {
            "pymysql": sys.modules["pymysql"],
            "db.rds_db": rds_mod,
        }
    finally:
        # Restore original module table
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        # Drop the modules under test so any next import re-resolves cleanly
        # against the restored (real) deps.
        for m in (
            "workflow_route.integration",
            "workflow_route.state_machine",
            "services.document_activity_service",
        ):
            sys.modules.pop(m, None)
