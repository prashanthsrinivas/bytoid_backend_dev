"""§4y-1 — LLM non-determinism & parser fuzzing.

Every parser that consumes raw model output must survive the FuzzyLLM corpus of
deliberately broken strings with a safe value and **never raise**. LLM output is
always a string, so the corpus is string-typed; the failure mode under test is
broken *content*, not a wrong type.

Invariant per parser: given any fuzz vector → returns the documented type and
raises no exception (parse-or-safe-fallback).
"""

from __future__ import annotations

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs
from tests.workflow_playbook._fuzz_corpus import EXTRACTABLE, FUZZ_VECTORS

stubs.bootstrap_sut()

import playbook.helperzz as h  # noqa: E402

pytestmark = [pytest.mark.fuzz, pytest.mark.resilience]

_IDS = [vid for vid, _ in FUZZ_VECTORS]
_RAWS = [raw for _, raw in FUZZ_VECTORS]


@pytest.mark.parametrize("raw", _RAWS, ids=_IDS)
def test_clean_json_block_never_crashes(raw):
    out = h.clean_json_block(raw)
    assert isinstance(out, str)


@pytest.mark.parametrize("raw", _RAWS, ids=_IDS)
def test_extract_json_from_llm_output_never_crashes(raw):
    out = h.extract_json_from_llm_output(raw)
    assert isinstance(out, str)


@pytest.mark.parametrize("raw", _RAWS, ids=_IDS)
def test_clean_yaml_block_never_crashes(raw):
    out = h.clean_yaml_block(raw)
    assert isinstance(out, str)


@pytest.mark.parametrize("raw", _RAWS, ids=_IDS)
def test_extract_questions_never_crashes(raw):
    out = h.extract_questions(raw)
    assert isinstance(out, list)


@pytest.mark.parametrize("vid,raw", EXTRACTABLE, ids=[v for v, _ in EXTRACTABLE])
def test_extractor_recovers_fenced_json(vid, raw):
    """For markdown-fenced output, the extractor must strip the fence so the
    result is parseable JSON, not the raw fenced blob."""
    import json

    out = h.extract_json_from_llm_output(raw)
    assert "```" not in out
    assert json.loads(out) == {"a": 1}
