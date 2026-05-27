"""Clients for LLM evaluation tools: Giskard, TruLens, and DeepEval.

All three are grouped here because they share the same usage pattern:
  - Giskard wraps a model + dataset and runs an automated vulnerability scan
  - TruLens records feedback on LLM app responses
  - DeepEval runs assertion-based metric evaluations

Imports are deferred to function bodies so this module can be imported
without the AI governance packages installed.

Required env vars:
    GISKARD_URL      — e.g. http://giskard.internal:19000
    GISKARD_API_KEY  — project API key
"""

import os
import threading
from typing import Any

# ── Giskard ───────────────────────────────────────────────────────────────────

_giskard_lock = threading.Lock()
_giskard_client = None


def get_giskard():
    """Return the shared Giskard client singleton."""
    global _giskard_client
    if _giskard_client is not None:
        return _giskard_client
    with _giskard_lock:
        if _giskard_client is None:
            import giskard

            _giskard_client = giskard.GiskardClient(
                url=os.environ["GISKARD_URL"],
                key=os.environ["GISKARD_API_KEY"],
            )
    return _giskard_client


# ── TruLens ───────────────────────────────────────────────────────────────────

_trulens_lock = threading.Lock()
_trulens_session = None


def get_trulens_session():
    """Return the shared TruLens TruSession singleton."""
    global _trulens_session
    if _trulens_session is not None:
        return _trulens_session
    with _trulens_lock:
        if _trulens_session is None:
            from trulens.core import TruSession

            _trulens_session = TruSession()
            _trulens_session.reset_database()
    return _trulens_session


# ── DeepEval ──────────────────────────────────────────────────────────────────


def run_deepeval_suite(test_cases: list[dict], metric_names: list[str]) -> list[dict]:
    """Run a DeepEval evaluation suite and return per-case results.

    Args:
        test_cases:   list of {"input": str, "actual_output": str,
                               "expected_output": str (optional),
                               "context": [str] (optional)}
        metric_names: list of metric identifiers, e.g.
                      ["answer_relevancy", "faithfulness", "contextual_precision"]

    Returns:
        list of {"input": str, "passed": bool, "scores": {metric: float}}
    """
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        FaithfulnessMetric,
    )
    from deepeval.test_case import LLMTestCase

    _METRIC_MAP = {
        "answer_relevancy": AnswerRelevancyMetric,
        "faithfulness": FaithfulnessMetric,
        "contextual_precision": ContextualPrecisionMetric,
    }

    metrics = [
        _METRIC_MAP[name](threshold=0.7)
        for name in metric_names
        if name in _METRIC_MAP
    ]

    results = []
    for tc in test_cases:
        test_case = LLMTestCase(
            input=tc["input"],
            actual_output=tc["actual_output"],
            expected_output=tc.get("expected_output", ""),
            retrieval_context=tc.get("context"),
        )
        passed = True
        scores: dict[str, Any] = {}
        for metric in metrics:
            metric.measure(test_case)
            scores[metric.__class__.__name__] = metric.score
            if not metric.is_successful():
                passed = False
        results.append({"input": tc["input"], "passed": passed, "scores": scores})

    return results
