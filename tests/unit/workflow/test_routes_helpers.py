"""§4d — pure helpers in ``workflow_route/routes.py``.

``_milestone_for_hop`` (transition → history milestone) and ``_is_allowed_image``
(upload allow-list). Both are pure. Importing ``workflow_route.routes`` pulls the
real ``services`` package, so we load that first, then stub the live-Redis
``services.redis_service`` before the import.
"""

from __future__ import annotations

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import workflow_route.routes as wr  # noqa: E402

pytestmark = pytest.mark.unit


# ── _milestone_for_hop ────────────────────────────────────────────────────────

def test_milestone_published_hop_skipped():
    assert wr._milestone_for_hop({"from_state": "approval", "to_state": "published"}) is None


@pytest.mark.parametrize("frm,to,action,summary", [
    ("draft", "quality_review", "submitted", "Submitted for quality review."),
    ("quality_review", "governance_review", "quality_approved", "Quality review approved."),
    ("governance_review", "approval", "governance_approved", "Governance review approved."),
])
def test_milestone_forward_hops(frm, to, action, summary):
    assert wr._milestone_for_hop({"from_state": frm, "to_state": to}) == (action, summary)


def test_milestone_forward_hop_with_comment():
    out = wr._milestone_for_hop(
        {"from_state": "draft", "to_state": "quality_review", "comment": "looks good"}
    )
    assert out == ("submitted", "Submitted for quality review: looks good.")


def test_milestone_auto_advanced_suppresses_comment():
    out = wr._milestone_for_hop(
        {"from_state": "draft", "to_state": "quality_review",
         "auto": True, "comment": "ignored"}
    )
    assert out == ("submitted", "Submitted for quality review (auto-advanced).")


def test_milestone_send_back_to_draft():
    out = wr._milestone_for_hop({"from_state": "quality_review", "to_state": "draft"})
    assert out == ("sent_back", "Sent back from quality review to draft.")


def test_milestone_generic_fallback():
    out = wr._milestone_for_hop({"from_state": "approval", "to_state": "governance_review"})
    assert out == ("transition", "approval → governance_review.")


# ── _is_allowed_image ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,ctype,expected", [
    ("a.png", "image/png", True),
    ("a.PNG", "image/png", True),
    ("photo.jpg", "image/jpeg", True),
    ("anim.gif", "image/gif", True),
    ("x.webp", "image/webp", True),
    ("doc.txt", "image/png", False),     # ext not allowed
    ("a.png", "text/plain", False),      # content-type not image/*
    ("a.svg", "image/svg+xml", False),   # svg not in allow-list
    ("", "image/png", False),
    ("a.png", "", False),
    ("noext", "image/png", False),
])
def test_is_allowed_image(filename, ctype, expected):
    assert wr._is_allowed_image(filename, ctype) is expected
