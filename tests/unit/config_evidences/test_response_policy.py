"""Phase 1 — per-artifact ``responsePolicy`` on evidence config entries.

Covers:
  * defaults applied lazily, in-memory, on read (NO S3 re-save → no KMS churn);
  * ``responsePolicy`` is plaintext (not in the KMS-encrypted text fields);
  * enum validation in ``_update_entry_by_id`` / ``_add_entry``;
  * every one of the default artifact types carries a valid policy;
  * the POST routes accept / validate the field;
  * ``run_evidence_check_job`` strips text options for ``evidence_only`` artifacts.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import sys  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

# config_evidences.routes does ``from services.redis_service import RedisService``;
# the shared stub only exposes ``get_redis`` — add the class so the import resolves.
_rs = sys.modules.get("services.redis_service")
if _rs is not None and not hasattr(_rs, "RedisService"):
    _rs.RedisService = MagicMock(name="RedisService")

import config_evidences.evidence_helpers as eh  # noqa: E402
from config_evidences.routes import config_evidences_bp  # noqa: E402


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_apply_defaults_stamps_missing_and_invalid():
    entries = [
        {"id": "1", "artifact": "Policies"},                       # missing
        {"id": "2", "artifact": "Logs", "responsePolicy": "bogus"},  # invalid
        {"id": "3", "artifact": "SOP", "responsePolicy": "text_fallback_allowed"},
    ]
    out = eh._apply_response_policy_defaults(entries)
    assert out[0]["responsePolicy"] == eh.DEFAULT_RESPONSE_POLICY == "evidence_only"
    assert out[1]["responsePolicy"] == "evidence_only"
    # a valid existing value is preserved
    assert out[2]["responsePolicy"] == "text_fallback_allowed"


def test_default_evidence_all_have_valid_policy():
    evidence = eh._load_default_evidence()
    assert evidence, "default evidence should not be empty"
    for entry in evidence:
        assert entry.get("responsePolicy") in eh.VALID_RESPONSE_POLICIES


def test_response_policy_is_plaintext_not_encrypted():
    # Excluded from the KMS-encrypted text fields → round-trips as plaintext.
    assert "responsePolicy" not in eh._EV_TEXT_FIELDS
    entry = {"id": "1", "artifact": "Policies", "responsePolicy": "evidence_only",
             "nature": "Doc", "primaryUse": "x", "expectations": "a; b"}
    with patch.object(eh._ev_kms, "encrypt", side_effect=lambda uid, v: {"ciphertext": v, "iv": "i", "encrypted_key": "k"}):
        enc = eh._encrypt_evidence_list("u1", [dict(entry)])
    assert enc[0]["responsePolicy"] == "evidence_only"  # untouched


def test_update_entry_validates_policy_enum():
    evidence = [{"id": "1", "artifact": "Policies", "expectations": "x"}]
    eh._update_entry_by_id(evidence, "1", response_policy="text_fallback_allowed")
    assert evidence[0]["responsePolicy"] == "text_fallback_allowed"
    with pytest.raises(ValueError):
        eh._update_entry_by_id(evidence, "1", response_policy="nope")


def test_update_entry_expectations_only_leaves_policy_untouched():
    evidence = [{"id": "1", "artifact": "Policies", "expectations": "x",
                 "responsePolicy": "text_fallback_allowed"}]
    eh._update_entry_by_id(evidence, "1", expectations="new")
    assert evidence[0]["expectations"] == "new"
    assert evidence[0]["responsePolicy"] == "text_fallback_allowed"


def test_add_entry_stamps_default_policy():
    evidence = [{"id": "1"}]
    entry_data = {"type": "Process", "number": 2, "artifact": "SOP",
                  "nature": "Doc", "primaryUse": "x", "expectations": "a"}
    _, new_entry = eh._add_entry(evidence, entry_data)
    assert new_entry["responsePolicy"] == "evidence_only"


def test_add_entry_rejects_invalid_policy():
    evidence = [{"id": "1"}]
    entry_data = {"type": "P", "number": 2, "artifact": "SOP", "nature": "D",
                  "primaryUse": "x", "expectations": "a", "responsePolicy": "bad"}
    with pytest.raises(ValueError):
        eh._validate_evidence_entry(entry_data)


def test_get_response_policy_map():
    fake = [
        {"artifact": "Policies", "responsePolicy": "evidence_only"},
        {"artifact": "Logs", "responsePolicy": "text_fallback_allowed"},
        {"artifact": "NoPolicy"},  # → default
    ]
    with patch.object(eh, "get_only_evidence", return_value=fake):
        m = eh.get_response_policy_map("u1")
    assert m["Policies"] == "evidence_only"
    assert m["Logs"] == "text_fallback_allowed"
    assert m["NoPolicy"] == "evidence_only"


def test_get_user_evidence_applies_defaults_without_resave():
    """A legacy user file missing responsePolicy must get defaults in-memory
    but NOT trigger a re-save (which would re-encrypt every field via KMS)."""
    legacy = [{"id": "1", "artifact": "Policies", "nature": "Doc",
               "primaryUse": "x", "expectations": "a; b"}]
    # Pretend the stored fields are already encrypted so was_migrated stays False.
    with patch.object(eh, "read_json_from_s3", return_value=legacy), \
         patch.object(eh, "_is_ev_enc", return_value=True), \
         patch.object(eh, "_dec_ev", side_effect=lambda uid, v: v), \
         patch.object(eh, "_save_user_evidence") as save_mock:
        evidence, is_custom = eh._get_user_evidence("u1")
    assert is_custom is True
    assert evidence[0]["responsePolicy"] == "evidence_only"
    save_mock.assert_not_called()


# ── deterministic text-option stripping ────────────────────────────────────────

def test_strip_text_options_for_evidence_only():
    qa = [
        {"subsection": "Policies", "options": {
            "A": "I will fix and re-upload", "B": "Accept as-is",
            "C": "Custom explanation"}, "discard_process": ["A"]},
        {"subsection": "Logs", "options": {
            "A": "I will fix and re-upload", "C": "Custom explanation"}},
    ]
    policy_map = {"Policies": "evidence_only", "Logs": "text_fallback_allowed"}
    out = eh._strip_text_options_for_evidence_only(qa, policy_map)
    # evidence_only → "Custom explanation" removed
    assert "Custom explanation" not in out[0]["options"].values()
    assert out[0]["response_policy"] == "evidence_only"
    # text_fallback → untouched
    assert "Custom explanation" in out[1]["options"].values()
    assert out[1]["response_policy"] == "text_fallback_allowed"


def test_strip_never_empties_options():
    # If every option is text-ish, keep them (never produce a dead question).
    qa = [{"subsection": "Policies", "options": {"A": "Custom explanation"}}]
    out = eh._strip_text_options_for_evidence_only(qa, {"Policies": "evidence_only"})
    assert out[0]["options"] == {"A": "Custom explanation"}


# ── route tests ─────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    return stubs.make_app(config_evidences_bp)


def test_update_route_accepts_response_policy(app):
    saved = {}

    def fake_save(uid, data):
        saved["data"] = data
        return {"status": "success"}

    with stubs.allow_auth(), \
         patch.object(eh, "read_json_from_s3", return_value=[
             {"id": "1", "artifact": "Policies", "nature": "Doc",
              "primaryUse": "x", "expectations": "a"}]), \
         patch.object(eh, "_is_ev_enc", return_value=True), \
         patch.object(eh, "_dec_ev", side_effect=lambda uid, v: v), \
         patch("config_evidences.routes._save_user_evidence", side_effect=fake_save), \
         patch("config_evidences.routes.get_email_by_id", return_value="a@b.com"):
        client = app.test_client()
        resp = client.post("/runbook/evidence/config", json={
            "user_id": "u1", "id": "1", "responsePolicy": "text_fallback_allowed"})
    assert resp.status_code == 200
    assert resp.get_json()["updated"]["responsePolicy"] == "text_fallback_allowed"


def test_update_route_rejects_invalid_policy(app):
    with stubs.allow_auth():
        client = app.test_client()
        resp = client.post("/runbook/evidence/config", json={
            "user_id": "u1", "id": "1", "responsePolicy": "garbage"})
    assert resp.status_code == 400


def test_update_route_requires_expectations_or_policy(app):
    with stubs.allow_auth():
        client = app.test_client()
        resp = client.post("/runbook/evidence/config", json={"user_id": "u1", "id": "1"})
    assert resp.status_code == 400


def test_add_route_missing_user_id_returns_400(app):
    # Regression: the handler used to reference user_id before assignment.
    with stubs.allow_auth():
        client = app.test_client()
        resp = client.post("/runbook/evidence/add", json={"type": "P"})
    assert resp.status_code == 400
    assert "user_id" in resp.get_json().get("error", "")
