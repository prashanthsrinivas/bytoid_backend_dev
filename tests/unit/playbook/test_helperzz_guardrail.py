"""§4e / §5 (llm_attack) — ``is_inappropriate`` guardrail.

Foul-word detection and short-gibberish rejection. Pure; ties to LLM input
safety (rejects abusive / junk instruction text before it reaches the model).
"""

from __future__ import annotations

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.helperzz as h  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.llm_attack]


@pytest.mark.parametrize("text", [
    "this plan is stupid",
    "shit happens here",
    "what a useless idea",
    "WTF is this",          # case-insensitive
])
def test_foul_words_flagged(text):
    assert h.is_inappropriate(text) is True


@pytest.mark.parametrize("text", [
    "create a monthly compliance report",
    "schedule a meeting with the security team",
    "summarize the latest audit findings",
])
def test_clean_instructions_pass(text):
    assert h.is_inappropriate(text) is False


@pytest.mark.parametrize("text", ["12345", "@#$%", "!!!"])
def test_short_gibberish_flagged(text):
    # <= 3 words and no alphabetic chars → rejected
    assert h.is_inappropriate(text) is True


def test_foul_word_must_be_whole_word():
    # "classify" contains "ass"-like fragments but no foul *word* boundary match
    assert h.is_inappropriate("classify the documents properly") is False
