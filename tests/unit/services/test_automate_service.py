"""§4h — ``services/automate_service.py`` ``AutoMateService``.

Starts with the sync, no-AI method ``get_current_step_data`` (step resolution by
int/str id, missing-id and not-found paths). AI methods are covered separately
with a mocked LLM. Instances are built via ``object.__new__`` to skip the
credential/LLM-client ``__init__``.
"""

from __future__ import annotations

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.automate_service as au  # noqa: E402

pytestmark = pytest.mark.unit


def _svc(current_step_id, workflow):
    s = object.__new__(au.AutoMateService)
    s.current_step_id = current_step_id
    s.workflow = workflow
    s.current_step_data = None
    return s


_WF = {"workflow": {"steps": [{"id": "1", "name": "first"}, {"id": "2", "name": "second"}]}}


def test_get_current_step_data_found_by_str_id():
    s = _svc("2", _WF)
    s.get_current_step_data()
    assert s.current_step_data == {"id": "2", "name": "second"}


def test_get_current_step_data_no_step_id_is_none():
    s = _svc(None, _WF)
    assert s.get_current_step_data() is None
    assert s.current_step_data is None


def test_get_current_step_data_not_found_raises():
    s = _svc("9", _WF)
    with pytest.raises(ValueError):
        s.get_current_step_data()


def test_get_current_step_data_ignores_malformed_steps():
    wf = {"workflow": {"steps": [{"no_id": True}, "junk", {"id": "1", "name": "ok"}]}}
    s = _svc("1", wf)
    s.get_current_step_data()
    assert s.current_step_data == {"id": "1", "name": "ok"}


# ── assign_or_show_questions_from_file (pure) ─────────────────────────────────

import asyncio  # noqa: E402


def test_assign_or_show_questions_success():
    s = _svc(None, {"assigned_questions": [{"id": "q1"}]})
    out = asyncio.run(s.assign_or_show_questions_from_file())
    assert out == {"status": "success", "questions": [{"id": "q1"}]}


def test_assign_or_show_questions_none_assigned():
    s = _svc(None, {})
    out = asyncio.run(s.assign_or_show_questions_from_file())
    assert out["status"] == "error"


def test_assign_or_show_questions_invalid_type():
    s = _svc(None, {"assigned_questions": "not-a-list"})
    out = asyncio.run(s.assign_or_show_questions_from_file())
    assert out["status"] == "error" and "list" in out["message"]


# ── generate_questions_from_file (no-content path) ────────────────────────────

def test_generate_questions_from_file_no_content():
    s = _svc(None, {})
    out = asyncio.run(s.generate_questions_from_file([]))
    assert out == {"error": "No usable content found in file"}


def test_generate_questions_from_file_blank_strings():
    s = _svc(None, {})
    out = asyncio.run(s.generate_questions_from_file([{"filename": "f", "content": "   "}]))
    assert out == {"error": "No usable content found in file"}
