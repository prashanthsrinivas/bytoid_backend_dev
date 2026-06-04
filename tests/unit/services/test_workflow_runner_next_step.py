"""§4g — ``WorkflowRunnerV2._get_next_uncompleted_step``.

Returns the first step (by ``step_order``) whose id is not in the completed set,
supporting both the online (``{"steps": {...}}``) and testing (``{...}``) shapes
of ``previous_data``.
"""

from __future__ import annotations

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402

pytestmark = pytest.mark.unit


def _runner(steps, step_order, previous_data):
    r = object.__new__(ws.WorkflowRunnerV2)
    r.steps = steps
    r.step_order = step_order
    r.previous_data = previous_data
    return r


_STEPS = {"1": {"id": "1"}, "2": {"id": "2"}, "3": {"id": "3"}}
_ORDER = {"1": 0, "2": 1, "3": 2}


def test_first_step_when_nothing_completed():
    assert _runner(_STEPS, _ORDER, {})._get_next_uncompleted_step() == "1"


def test_skips_completed_testing_shape():
    # testing shape: previous_data keyed directly by completed step ids
    assert _runner(_STEPS, _ORDER, {"1": {}})._get_next_uncompleted_step() == "2"


def test_skips_completed_online_shape():
    # online shape: completed ids live under "steps"
    pd = {"steps": {"1": {}, "2": {}}}
    assert _runner(_STEPS, _ORDER, pd)._get_next_uncompleted_step() == "3"


def test_none_when_all_completed():
    assert _runner(_STEPS, _ORDER, {"1": {}, "2": {}, "3": {}})._get_next_uncompleted_step() is None


def test_respects_step_order_not_dict_order():
    # reverse the order map → step "3" should come first
    assert _runner(_STEPS, {"1": 2, "2": 1, "3": 0}, {})._get_next_uncompleted_step() == "3"
