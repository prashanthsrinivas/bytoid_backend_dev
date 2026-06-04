"""§4g — ``services/workflow_service.py`` ``WorkflowRunnerV2`` sync helpers + ``base_name``.

Pure / no-I/O methods. Instances are built via ``object.__new__`` (as the proven
trigger test does) to skip the AWS/RDS/S3 ``__init__``; only the attributes each
method reads are set by hand.
"""

from __future__ import annotations

import types

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402

pytestmark = pytest.mark.unit


def _runner(**attrs):
    r = object.__new__(ws.WorkflowRunnerV2)
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


# ── module-level base_name ────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,expected", [
    ("abcdefghij.yaml", "abcdefgh"),
    ("ab.txt", "ab"),
    ("noext", "noext"),
    ("", ""),
])
def test_base_name(filename, expected):
    assert ws.base_name(filename) == expected


# ── is_yes ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("yes", True),
    ("Yeah, sure", True),
    ("OK", True),
    ("affirmative.", True),
    ("no", False),
    ("nope not at all", False),
    ("", False),
    ("123 !!!", False),
])
def test_is_yes(text, expected):
    assert _runner().is_yes(text) is expected


# ── generate_unique_id ────────────────────────────────────────────────────────

def test_generate_unique_id_is_6_digits_and_avoids_collisions():
    r = _runner()
    uid = r.generate_unique_id(set())
    assert uid.isdigit() and len(uid) == 6


def test_generate_unique_id_skips_existing(monkeypatch):
    r = _runner()
    seq = iter(["111111", "111111", "222222"])
    monkeypatch.setattr(
        ws.uuid, "uuid4",
        lambda: types.SimpleNamespace(int=int(next(seq) + "0" * 12)),
    )
    # existing has 111111 → must skip to 222222
    assert r.generate_unique_id({"111111"}) == "222222"


# ── check_step_exists ─────────────────────────────────────────────────────────

def test_check_step_exists():
    r = _runner(steps={"1": {}, "2": {}})
    assert r.check_step_exists("1") is True
    assert r.check_step_exists(1) is True
    assert r.check_step_exists("9") is False
    assert r.check_step_exists(None) is False


# ── get_step_data ─────────────────────────────────────────────────────────────

def test_get_step_data_str_and_missing():
    r = _runner(steps={"1": {"id": "1", "name": "a"}})
    assert r.get_step_data("1") == {"id": "1", "name": "a"}
    assert r.get_step_data("nope") is None


# ── _get_first_step (the unreferenced node) ───────────────────────────────────

def test_get_first_step_returns_unreferenced():
    r = _runner(steps={
        "1": {"id": "1", "next_step": "2"},
        "2": {"id": "2", "next_step": None},
    })
    assert r._get_first_step() == "1"


def test_get_first_step_none_when_all_referenced():
    r = _runner(steps={
        "1": {"id": "1", "next_step": "2"},
        "2": {"id": "2", "next_step": "1"},
    })
    assert r._get_first_step() is None


# ── _find_step_by_ref (id match, then 1-based position) ───────────────────────

def test_find_step_by_ref_direct_id():
    r = _runner(steps={"alpha": {"id": "alpha"}}, workflow_json={})
    assert r._find_step_by_ref("alpha") == {"id": "alpha"}


def test_find_step_by_ref_by_position():
    r = _runner(
        steps={},
        workflow_json={"workflow": {"steps": [{"id": "a"}, {"id": "b"}]}},
    )
    assert r._find_step_by_ref("2") == {"id": "b"}   # 1-based


def test_find_step_by_ref_missing():
    r = _runner(steps={}, workflow_json={"workflow": {"steps": []}})
    assert r._find_step_by_ref("nope") is None
