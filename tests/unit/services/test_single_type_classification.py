"""Phase 3 — single-type evidence classification.

Each uploaded file (or image) is attributed to exactly ONE artifact type.
Covers the ``pick_best_artifact`` tie-break, the end-to-end single-type
invariant through ``answer_ques_file_bk`` (text + image), and the legacy
``dedupe_evidence_overview`` normalizer.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import services.workflow_service as ws  # noqa: E402
# dedupe_evidence_overview lives in a dependency-free module so it imports
# without pulling runbook.helper's heavy (pandas) chain.
import runbook.evidence_overview as rh  # noqa: E402

pytestmark = pytest.mark.unit


# ── pick_best_artifact ─────────────────────────────────────────────────────────

def test_pick_best_artifact_highest_score():
    cands = {
        "Policies": {"snippets": ["a"], "score": 0.9},
        "Logs": {"snippets": ["b", "c"], "score": 0.4},
    }
    assert ws.pick_best_artifact(cands) == "Policies"


def test_pick_best_artifact_tie_breaks_on_snippet_count():
    cands = {
        "Policies": {"snippets": ["a"], "score": 0.5},
        "Logs": {"snippets": ["b", "c"], "score": 0.5},
    }
    assert ws.pick_best_artifact(cands) == "Logs"


def test_pick_best_artifact_tie_breaks_lexically():
    cands = {
        "Zeta": {"snippets": ["a"], "score": 0.5},
        "Alpha": {"snippets": ["b"], "score": 0.5},
    }
    assert ws.pick_best_artifact(cands) == "Alpha"


def test_pick_best_artifact_empty():
    assert ws.pick_best_artifact({}) is None


def test_pick_best_artifact_missing_confidence_defaults():
    # An entry with no 'score' key must not crash.
    cands = {"Policies": {"snippets": ["a"]}}
    assert ws.pick_best_artifact(cands) == "Policies"


# ── end-to-end single-type invariant ────────────────────────────────────────────

def _make_runner():
    r = object.__new__(ws.WorkflowRunnerV2)
    r.userid = "u1"
    r.logger = MagicMock()
    r.credits = MagicMock()
    r.workflow_json = {
        "filename": "f.json",
        "assigned_questions": [{"id": "q1", "question": "Q?", "options": {}}],
        "runbook_id": "",
    }
    r.previous_data = {"s1": {"output": [{"id": "q1"}]}}
    r.chat_history = []
    return r


def _route_llm(prompt_text):
    """Route a fake LLM response by the prompt's intent."""
    if "evidence classification expert" in prompt_text:
        # ONE file matches TWO artifacts — classifier must still pick one.
        return json.dumps([
            {"artifact": "Policies", "content": "policy text", "confidence": 0.9},
            {"artifact": "System log file", "content": "log line", "confidence": 0.3},
        ])
    if "satisfy these expectations" in prompt_text:
        return json.dumps({"passes": True, "reason": ""})
    if "STRICT QUESTION ANSWERING" in prompt_text:
        return json.dumps({"id": "q1", "user_answer": "covered"})
    if "satisfy this specific expectation" in prompt_text:
        return json.dumps({"met": True})
    return "{}"


def _file_url_to_artifact(overview):
    """Map file URL → set of artifacts it appears under (across all buckets)."""
    out = {}
    for bucket in ("admissible", "inadmissible", "discarded"):
        for entry in overview.get(bucket, []):
            for f in entry.get("files", []):
                out.setdefault(f, set()).add(entry.get("artifact"))
    return out


def test_each_file_single_artifact_text():
    r = _make_runner()

    user_evidence = [
        {"id": "1", "artifact": "Policies", "type": "Process",
         "nature": "Documentation", "expectations": "approved; owner"},
        {"id": "2", "artifact": "System log file", "type": "Control",
         "nature": "Raw log/export", "expectations": "timestamps; actor"},
    ]
    extracted_files = [
        {"content": "some content here", "s3_key": "u1/uploads/fileA.pdf",
         "filename": "fileA.pdf"},
    ]

    async def _fake_llm(user_message="", **kw):
        return _route_llm(user_message)

    with patch("config_evidences.evidence_helpers.get_only_evidence", return_value=user_evidence), \
         patch.object(ws, "get_fireworks_response2", side_effect=_fake_llm), \
         patch.object(ws, "attach_CLDFRNT_url", side_effect=lambda k: f"https://cf/{k}"), \
         patch("db.lance_db_service.LanceDBServer", MagicMock()), \
         patch.object(ws.WorkflowRunnerV2, "saveworkflowtos3", MagicMock(), create=True), \
         patch.object(ws.WorkflowRunnerV2, "_question_answer_stats",
                      return_value={"all_answered": False, "answered": 0, "total": 1}), \
         patch.object(ws.WorkflowRunnerV2, "_trigger_runbook_owner", MagicMock(), create=True):
        asyncio.run(r.answer_ques_file_bk(extracted_files, "s1", ["u1/uploads/fileA.pdf"]))

    overview = r.workflow_json["evidence_overview"]
    mapping = _file_url_to_artifact(overview)
    assert mapping, "file should appear in the overview"
    for fileurl, artifacts in mapping.items():
        assert len(artifacts) == 1, f"{fileurl} mapped to {artifacts}"
    # Highest-confidence artifact (Policies @0.9) wins over System log file @0.3.
    assert "Policies" in next(iter(mapping.values()))

    # Phase 4: admissible entry carries a per-expectation checklist.
    policies_entry = next(
        e for e in overview["admissible"] if e["artifact"] == "Policies"
    )
    checklist = policies_entry["expectations_checklist"]
    assert [c["expectation"] for c in checklist] == ["approved", "owner"]
    assert all("met" in c for c in checklist)


def test_each_image_single_artifact():
    r = _make_runner()
    user_evidence = [
        {"id": "1", "artifact": "Screenshot", "type": "Control",
         "nature": "Image", "expectations": "timestamp; url"},
    ]

    async def _fake_llm(user_message="", **kw):
        return _route_llm(user_message)

    async def _fake_vision(**kw):
        return {"found": [
            {"artifact": "Screenshot", "content": "a", "confidence": 0.8},
            {"artifact": "System screenshot", "content": "b", "confidence": 0.6},
        ], "image_meta": {}}

    with patch("config_evidences.evidence_helpers.get_only_evidence", return_value=user_evidence), \
         patch.object(ws, "get_fireworks_response2", side_effect=_fake_llm), \
         patch.object(ws, "get_think_bedrock_vision_image", side_effect=_fake_vision), \
         patch.object(ws, "attach_CLDFRNT_url", side_effect=lambda k: f"https://cf/{k}"), \
         patch("db.lance_db_service.LanceDBServer", MagicMock()), \
         patch.object(ws.WorkflowRunnerV2, "saveworkflowtos3", MagicMock(), create=True), \
         patch.object(ws.WorkflowRunnerV2, "_question_answer_stats",
                      return_value={"all_answered": False, "answered": 0, "total": 1}), \
         patch.object(ws.WorkflowRunnerV2, "_trigger_runbook_owner", MagicMock(), create=True):
        asyncio.run(r.answer_ques_file_bk(
            [], "s1", [], inp_links=["data:image/png;base64,xxx"],
            inp_link_keys=["u1/uploads/img.png"]))

    overview = r.workflow_json["evidence_overview"]
    mapping = _file_url_to_artifact(overview)
    for fileurl, artifacts in mapping.items():
        assert len(artifacts) == 1, f"{fileurl} mapped to {artifacts}"


def test_malformed_classification_does_not_crash():
    r = _make_runner()
    user_evidence = [{"id": "1", "artifact": "Policies", "type": "P",
                      "nature": "Documentation", "expectations": "x"}]
    extracted_files = [{"content": "c", "s3_key": "k", "filename": "f.pdf"}]

    async def _bad_llm(user_message="", **kw):
        if "evidence classification expert" in user_message:
            return "not json at all {{{"
        return "{}"

    with patch("config_evidences.evidence_helpers.get_only_evidence", return_value=user_evidence), \
         patch.object(ws, "get_fireworks_response2", side_effect=_bad_llm), \
         patch.object(ws, "attach_CLDFRNT_url", side_effect=lambda k: f"cf/{k}"), \
         patch("db.lance_db_service.LanceDBServer", MagicMock()), \
         patch.object(ws.WorkflowRunnerV2, "saveworkflowtos3", MagicMock(), create=True), \
         patch.object(ws.WorkflowRunnerV2, "_question_answer_stats",
                      return_value={"all_answered": False, "answered": 0, "total": 1}), \
         patch.object(ws.WorkflowRunnerV2, "_trigger_runbook_owner", MagicMock(), create=True):
        # Must complete without raising; file lands as inadmissible (unclassified).
        asyncio.run(r.answer_ques_file_bk(extracted_files, "s1", ["k"]))
    assert "evidence_overview" in r.workflow_json


# ── dedupe_evidence_overview ─────────────────────────────────────────────────

def test_dedupe_collapses_legacy_multi_artifact():
    legacy = {
        "admissible": [
            {"artifact": "Policies", "files": ["f1", "f2"], "summary": "x"},
            {"artifact": "Logs", "files": ["f1"], "summary": "y"},  # f1 dup
        ],
        "inadmissible": [
            {"artifact": "Scan", "files": ["f2"], "summary": "z"},  # f2 dup
        ],
        "discarded": [],
    }
    out = rh.dedupe_evidence_overview(legacy)
    # f1 only under Policies (first wins), f2 only under Policies
    file_artifacts = {}
    for bucket in ("admissible", "inadmissible", "discarded"):
        for e in out[bucket]:
            for f in e.get("files", []):
                file_artifacts.setdefault(f, []).append(e["artifact"])
    assert file_artifacts["f1"] == ["Policies"]
    assert file_artifacts["f2"] == ["Policies"]
    # Logs entry had only f1 (claimed) → dropped; Scan entry had only f2 → dropped
    assert [e["artifact"] for e in out["admissible"]] == ["Policies"]
    assert out["inadmissible"] == []


def test_dedupe_tolerates_odd_shapes():
    assert rh.dedupe_evidence_overview(None) is None
    assert rh.dedupe_evidence_overview({"admissible": "weird"})["admissible"] == "weird"
    # entry without files list is kept verbatim
    ov = {"admissible": [{"artifact": "P", "summary": "s"}]}
    assert rh.dedupe_evidence_overview(ov)["admissible"][0]["artifact"] == "P"


def test_dedupe_preserves_extra_keys():
    ov = {"admissible": [], "inadmissible": [], "discarded": [], "extra": 1}
    assert rh.dedupe_evidence_overview(ov)["extra"] == 1
