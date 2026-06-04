"""§4 (Phase 4, completion) — every ``workflow_bp`` + ``playbook_bp`` endpoint.

Auto-derives the full route table from the URL map, so the parametrization
covers **every** registered endpoint with no hand-maintained list. Each route is
exercised through the Flask test client and must:
  * never raise an unhandled 500 (the plan's core contract), and
  * deny access when authorization is refused (gated routes → 4xx).

This is the breadth gate; representative endpoints additionally have detailed
happy/validation/contract tests in the sibling integration modules.
"""

from __future__ import annotations

import re

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.routes as pr  # noqa: E402
import workflow_route.routes as wr  # noqa: E402

_APP = stubs.make_app(wr.workflow_bp, pr.playbook_bp)

_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
ROUTES = sorted(
    {
        (rule.rule, method)
        for rule in _APP.url_map.iter_rules()
        if rule.endpoint != "static"
        for method in (rule.methods or set())
        if method in _METHODS
    }
)


def _concrete(rule: str) -> str:
    # substitute path params (<id>, <path:doc_id>, …) with a dummy segment
    return re.sub(r"<[^>]+>", "x", rule)


@pytest.fixture
def client():
    return _APP.test_client()


def test_route_table_is_complete():
    # 16 workflow_bp + ~55 playbook_bp endpoints (method-expanded ≥ 71)
    assert len(ROUTES) >= 71, f"only {len(ROUTES)} routes discovered"


@pytest.mark.integration
@pytest.mark.parametrize("rule,method", ROUTES, ids=[f"{m} {r}" for r, m in ROUTES])
def test_endpoint_no_unhandled_500(client, rule, method):
    """Every endpoint handles an empty/auth-less request without crashing."""
    with stubs.deny_auth():
        resp = client.open(_concrete(rule), method=method, json={})
    assert resp.status_code < 500, (
        f"{method} {rule} returned {resp.status_code} (unhandled server error)"
    )


@pytest.mark.security
@pytest.mark.authz
@pytest.mark.parametrize("rule,method", ROUTES, ids=[f"{m} {r}" for r, m in ROUTES])
def test_endpoint_denies_when_unauthorized(client, rule, method):
    """Under refused authorization, no endpoint returns a 2xx success."""
    with stubs.deny_auth():
        resp = client.open(_concrete(rule), method=method, json={"user_id": "attacker"})
    assert resp.status_code >= 400, (
        f"{method} {rule} returned {resp.status_code} while authorization was denied"
    )
