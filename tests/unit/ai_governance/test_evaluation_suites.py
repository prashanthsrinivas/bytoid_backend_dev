"""LLM evaluation suite tests — Giskard, TruLens, and DeepEval.

OFFLINE (default, always runs in CI / mutmut):
  - All AI libraries are stubbed via conftest.py
  - Tests verify client helper logic, metric mapping, and stub-safe invocations
  - deepeval.assert_test is monkeypatched; the offline test exercises the
    pass/fail branching without hitting a real LLM

LIVE (opt-in, requires RUN_LIVE_LLM=1):
  - Marked with @pytest.mark.live_llm and @pytest.mark.skipif
  - Require real API keys (DEEPEVAL_API_KEY, GISKARD_URL, etc.)
  - Never run during mutation testing
"""

import os
from unittest.mock import MagicMock

import pytest


# ── Offline DeepEval tests ────────────────────────────────────────────────────


class TestDeepEvalOffline:
    """Verify run_deepeval_suite with fully stubbed deepeval library."""

    def _make_metric(self, score: float, success: bool):
        m = MagicMock()
        m.score = score
        m.is_successful.return_value = success
        m.__class__.__name__ = "AnswerRelevancyMetric"
        return m

    def test_all_pass_returns_passed_true(self, monkeypatch):
        import sys
        import ai_governance.clients.giskard_client as gc

        # Patch the modules that run_deepeval_suite resolves via sys.modules
        # (not via `import X as Y`, which goes through the parent stub's attribute).
        dm = sys.modules["deepeval.metrics"]
        dt = sys.modules["deepeval.test_case"]

        mock_tc_cls = MagicMock()
        mock_tc_cls.return_value = MagicMock()

        monkeypatch.setattr(dt, "LLMTestCase", mock_tc_cls)
        monkeypatch.setattr(dm, "AnswerRelevancyMetric", lambda **kw: self._make_metric(0.9, True))
        monkeypatch.setattr(dm, "FaithfulnessMetric", lambda **kw: self._make_metric(0.85, True))
        monkeypatch.setattr(dm, "ContextualPrecisionMetric", lambda **kw: self._make_metric(0.8, True))

        results = gc.run_deepeval_suite(
            test_cases=[{"input": "What is 2+2?", "actual_output": "4"}],
            metric_names=["answer_relevancy"],
        )
        assert len(results) == 1
        assert results[0]["passed"] is True

    def test_metric_failure_returns_passed_false(self, monkeypatch):
        import sys
        import ai_governance.clients.giskard_client as gc

        dm = sys.modules["deepeval.metrics"]
        dt = sys.modules["deepeval.test_case"]

        mock_tc_cls = MagicMock()
        mock_tc_cls.return_value = MagicMock()

        monkeypatch.setattr(dt, "LLMTestCase", mock_tc_cls)
        monkeypatch.setattr(dm, "AnswerRelevancyMetric", lambda **kw: self._make_metric(0.2, False))

        results = gc.run_deepeval_suite(
            test_cases=[{"input": "dangerous prompt", "actual_output": ""}],
            metric_names=["answer_relevancy"],
        )
        assert results[0]["passed"] is False

    def test_unknown_metric_is_skipped(self, monkeypatch):
        import ai_governance.clients.giskard_client as gc

        results = gc.run_deepeval_suite(
            test_cases=[{"input": "q", "actual_output": "a"}],
            metric_names=["nonexistent_metric"],
        )
        # No metrics applied → passed stays True (no failures registered)
        assert results[0]["passed"] is True
        assert results[0]["scores"] == {}


# ── Offline Giskard tests ─────────────────────────────────────────────────────


class TestGiskardClientOffline:
    """Verify get_giskard() singleton behaviour without real API."""

    def test_singleton_returns_same_instance(self, monkeypatch):
        import ai_governance.clients.giskard_client as gc

        fake_client = MagicMock()
        monkeypatch.setenv("GISKARD_URL", "http://fake-giskard:19000")
        monkeypatch.setenv("GISKARD_API_KEY", "fake-key")

        import giskard

        monkeypatch.setattr(giskard, "GiskardClient", lambda url, key: fake_client)

        # Reset singleton
        gc._giskard_client = None

        c1 = gc.get_giskard()
        c2 = gc.get_giskard()
        assert c1 is c2

        # Cleanup
        gc._giskard_client = None


# ── Offline TruLens tests ─────────────────────────────────────────────────────


class TestTruLensClientOffline:
    """Verify get_trulens_session() singleton behaviour."""

    def test_singleton_returns_same_instance(self, monkeypatch):
        import ai_governance.clients.giskard_client as gc

        fake_session = MagicMock()

        import trulens.core as tlc

        monkeypatch.setattr(tlc, "TruSession", lambda: fake_session)

        gc._trulens_session = None

        s1 = gc.get_trulens_session()
        s2 = gc.get_trulens_session()
        assert s1 is s2

        gc._trulens_session = None


# ── Live DeepEval tests (gated by RUN_LIVE_LLM=1) ────────────────────────────


@pytest.mark.live_llm
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_LLM") != "1",
    reason="Live LLM test — set RUN_LIVE_LLM=1 to enable",
)
@pytest.mark.parametrize("prompt,expected_output", [
    ("What is the capital of France?", "Paris"),
    ("What is 2 + 2?", "4"),
])
def test_deepeval_live_answer_relevancy(prompt, expected_output):
    """Live DeepEval test — calls a real model. Gated by RUN_LIVE_LLM=1."""
    from deepeval import assert_test
    from deepeval.metrics import AnswerRelevancyMetric
    from deepeval.test_case import LLMTestCase

    metric = AnswerRelevancyMetric(threshold=0.7)
    test_case = LLMTestCase(input=prompt, actual_output=expected_output)
    assert_test(test_case, [metric])
