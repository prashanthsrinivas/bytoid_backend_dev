"""Unit tests for the defensive/shaping helpers used by the GRC dashboards.

Heavy deps (db.rds_db hits AWS at import) are stubbed before importing the SUT,
mirroring tests/strategy. We exercise only the pure pieces (no DB calls):
the best-effort `_safe` wrapper and the cloud-posture shaping.
"""

import sys
import types
from unittest.mock import MagicMock

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

from grc_dashboards import helper  # noqa: E402


@pytest.mark.unit
def test_safe_returns_default_on_failure():
    def boom():
        raise ValueError("nope")

    assert helper._safe(boom, {"fallback": True}) == {"fallback": True}
    assert helper._safe(lambda: 42, 0) == 42


@pytest.mark.unit
def test_keep_latest_picks_most_recent_per_provider():
    acc: dict = {}
    helper._keep_latest(acc, "aws", {"audit_id": "a", "last_scan_at": "2026-01-01"})
    helper._keep_latest(acc, "aws", {"audit_id": "b", "last_scan_at": "2026-06-01"})
    helper._keep_latest(acc, "aws", {"audit_id": "c", "last_scan_at": "2026-03-01"})
    assert acc["aws"]["audit_id"] == "b"  # newest wins


@pytest.mark.unit
def test_keep_latest_handles_missing_scan_dates():
    acc: dict = {}
    helper._keep_latest(acc, "gcp", {"audit_id": "x"})  # no last_scan_at
    helper._keep_latest(acc, "gcp", {"audit_id": "y", "last_scan_at": "2026-01-01"})
    assert acc["gcp"]["audit_id"] == "y"


@pytest.mark.unit
def test_provider_card_shape():
    rec = {
        "audit_id": "aud1", "name": "Prod", "scan_state": "complete",
        "last_scan_at": "2026-06-01", "latest_posture_score": 82, "latest_risk_score": 31,
    }
    card = helper._provider_card(rec, "aws")
    assert card == {
        "provider": "aws", "name": "Prod", "scan_state": "complete",
        "last_scan_at": "2026-06-01", "posture_score": 82, "risk_score": 31, "audit_id": "aud1",
    }
