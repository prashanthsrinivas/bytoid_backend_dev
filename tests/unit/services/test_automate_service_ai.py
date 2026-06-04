"""§4h — ``AutoMateService`` AI methods with a mocked LLM.

Mocks ``get_fireworks_response2`` (the Fireworks call) so the test asserts the
method's contract: prompt assembly inputs flow through, the response is returned
in the documented shape, and the DB connection is released. Built via
``object.__new__`` to skip the credential ``__init__``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.automate_service as au  # noqa: E402

pytestmark = pytest.mark.unit


def _svc():
    s = object.__new__(au.AutoMateService)
    s.userid = "u1"
    s.credits = MagicMock()
    s.connection = MagicMock()      # truthy → skips connect_to_rds()
    return s


def test_create_custom_email_body_returns_stripped_html():
    svc = _svc()
    llm = AsyncMock(return_value="  <html><body>Hi</body></html>  ")
    with patch.object(au, "get_fireworks_response2", llm), \
         patch.object(au, "get_business_info", return_value={"BusinessName": "Acme"}):
        out = asyncio.run(svc.create_custom_email_body("welcome email"))

    assert out == {"email_body_html": "<html><body>Hi</body></html>"}
    assert svc.connection.close.called          # connection released
    # the user request and dynamic args are threaded into the prompt
    prompt = llm.await_args.kwargs["user_message"]
    assert "welcome email" in prompt


def test_create_custom_email_body_threads_dynamic_args_and_business_info():
    svc = _svc()
    llm = AsyncMock(return_value="<html></html>")
    with patch.object(au, "get_fireworks_response2", llm), \
         patch.object(au, "get_business_info", return_value={"BusinessName": "Acme Corp",
                                                             "WebsiteUrl": "https://acme"}):
        asyncio.run(svc.create_custom_email_body("promo", customer_name="Ada", plan="Pro"))

    prompt = llm.await_args.kwargs["user_message"]
    assert "Acme Corp" in prompt and "https://acme" in prompt
    assert "Customer Name: Ada" in prompt and "Plan: Pro" in prompt   # title-cased keys


def test_generate_file_from_ai_writes_txt_and_returns_path():
    # After the await-precedence fix, the LLM call is a normal async coroutine —
    # a plain AsyncMock returning a str is the realistic mock (the previous
    # awaitable-string hack was only needed to get past the bug).
    svc = _svc()
    with patch.object(au, "get_fireworks_response2", AsyncMock(return_value="report.txt")), \
         patch.object(au, "load_yaml_file",
                      return_value={"generate_file_content": {"instructions": "make"}}):
        out = asyncio.run(svc.generate_file_from_ai("write a report"))
    assert "report.txt" in str(out)


def test_create_custom_email_body_lazily_opens_connection_when_none():
    svc = _svc()
    svc.connection = None
    fake_conn = MagicMock()
    llm = AsyncMock(return_value="<html>x</html>")
    with patch.object(au, "get_fireworks_response2", llm), \
         patch.object(au, "connect_to_rds", return_value=fake_conn) as conn_factory, \
         patch.object(au, "get_business_info", return_value={}):
        out = asyncio.run(svc.create_custom_email_body("hello"))

    assert conn_factory.called                  # opened lazily
    assert out["email_body_html"] == "<html>x</html>"
    assert fake_conn.close.called
