"""Regression: tickets list handlers must return a clean 400 (not 500) when the
required `last_updated_in` query param is absent — previously they crashed on
`None.replace(...)`. External deps are stubbed so the module imports without a DB.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest
from flask import Flask


@pytest.fixture(scope="module")
def tickets_routes():
    keys = [
        "utils.s3_utils", "db", "db.rds_db", "cust_helpers", "cust_helpers.pathconfig",
        "utils.normal", "ai_assistant_chat", "ai_assistant_chat.routes", "tickets.routes",
    ]
    saved = {k: sys.modules.get(k) for k in keys}
    for k in keys:
        if k == "tickets.routes":
            sys.modules.pop(k, None)
            continue
        mod = types.ModuleType(k)
        mod.__getattr__ = lambda name, _n=k: MagicMock(name=f"{_n}.{name}")
        sys.modules[k] = mod
    # connect_to_rds must never be reached by the validation path under test.
    sys.modules["db.rds_db"].connect_to_rds = MagicMock(
        side_effect=AssertionError("DB must not be hit before validation")
    )

    import tickets.routes as routes
    yield routes

    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    sys.modules.pop("tickets.routes", None)


@pytest.mark.unit
@pytest.mark.regression
def test_get_user_tickets_missing_cursor_param_returns_400(tickets_routes):
    app = Flask(__name__)
    with app.test_request_context("/tickets/u1"):  # no last_updated_in
        resp, status = tickets_routes.get_user_tickets("u1")
    assert status == 400
    assert "last_updated_in" in resp.get_json()["error"]


@pytest.mark.unit
@pytest.mark.regression
def test_filter_tickets_missing_cursor_param_returns_400(tickets_routes):
    app = Flask(__name__)
    with app.test_request_context("/filter_tickets/u1"):
        resp, status = tickets_routes.filter_tickets("u1")
    assert status == 400


@pytest.mark.unit
@pytest.mark.regression
def test_search_tickets_missing_cursor_param_returns_400(tickets_routes):
    app = Flask(__name__)
    with app.test_request_context("/search_tickets/u1"):
        resp, status = tickets_routes.search_tickets("u1")
    assert status == 400
