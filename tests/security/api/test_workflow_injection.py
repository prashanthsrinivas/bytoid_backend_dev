"""§5 / §5a security — ``workflow_bp`` injection & authorization.

Maps to OWASP A03 (Injection) / CWE-89 (SQLi) and A01 / CWE-862-863 (broken
access control). Uses the fake DB to assert that user-controlled values reach the
driver as *bound parameters*, never interpolated into SQL text.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import workflow_route.routes as wr  # noqa: E402
import workflow_route.state_machine as sm  # noqa: E402

pytestmark = [pytest.mark.security, pytest.mark.api_security]

_ALIAS = "workflow_route.routes"

_SQLI = "1' OR '1'='1"


@pytest.fixture
def client():
    return stubs.make_app(wr.workflow_bp).test_client()


@pytest.mark.cwe
@pytest.mark.owasp
def test_sqli_in_user_id_is_parameterized(client):
    """CWE-89: a SQLi payload in user_id must be bound as a parameter, and the
    executed SQL text must contain no part of the injection."""
    conn = stubs.make_conn(fetchone=None)
    with stubs.mock_rds(conn, _ALIAS):
        resp = client.get("/workflow/assignable-users", query_string={"user_id": _SQLI})

    assert resp.status_code == 200
    sql, params = conn.fake_cursor.executed[0]
    assert "%s" in sql                       # placeholder, not f-string
    assert "OR '1'='1" not in sql            # payload never reached the SQL text
    assert any(_SQLI in str(p) for p in (params or ()))   # carried as a bound param


@pytest.mark.authz
@pytest.mark.owasp
def test_submit_requires_authorization(client):
    """A01 / CWE-862: the gated mutation is denied without permission."""
    with stubs.deny_auth():
        resp = client.post("/workflow/submit",
                            json={"user_id": "attacker", "doc_type": "policy", "doc_id": "d1"})
    assert resp.status_code == 403


@pytest.mark.api_security
@pytest.mark.cwe
def test_get_workflow_history_parameterizes_workflow_id():
    """CWE-89: a UNION-injection workflow_id is bound as a parameter — it never
    appears in the SQL text. Tested at the function level (robust to route-wrapper
    state); also covers ``get_workflow_history``."""
    conn = stubs.make_conn(fetchone={"cnt": 0}, fetchall=[])
    payload = "w1' UNION SELECT * FROM users--"
    # get_workflow_history lazily does `from utils.s3_utils import
    # generate_presigned_url` at call time; isolate from any suite-level s3
    # stub poisoning by injecting a minimal s3 module for the duration.
    fake_s3 = types.ModuleType("utils.s3_utils")
    fake_s3.generate_presigned_url = lambda *a, **k: "https://signed/url"
    with patch.dict(sys.modules, {"utils.s3_utils": fake_s3}), \
         stubs.mock_rds(conn, "workflow_route.state_machine"):
        rows, total = sm.get_workflow_history(payload)

    assert (rows, total) == ([], 0)
    executed = conn.fake_cursor.executed
    assert executed, "expected at least one query"
    for sql, _params in executed:
        assert payload not in sql                      # never interpolated
    assert any(payload in str(p)                       # carried as a bound param
               for _sql, prm in executed for p in (prm or ()))
