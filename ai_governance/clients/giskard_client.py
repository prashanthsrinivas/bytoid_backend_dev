"""Clients for LLM evaluation tools: Giskard (OSS), TruLens, and DeepEval.

Giskard runs locally as an OSS library — no GiskardClient / no project key /
no GISKARD_URL or API key required.  Callers pass a Pandas dataset and an
optional model_config; `run_local_giskard_scan` wraps `giskard.scan()` and
returns a JSON-serialisable dict.

When no dataset is supplied, a built-in German Credit sample (Giskard's
tutorial dataset) is used so the UI's "Start Scan" button works end-to-end
without the user having to upload anything.
"""

import threading
from typing import Any

# ── Giskard OSS local scan ────────────────────────────────────────────────────


def _build_sample_dataset():
    """Built-in tutorial dataset (German Credit, simplified).  Returned as a
    list of row-dicts plus the target/feature names so it can be turned into
    a Pandas DataFrame.  Used when the user has not supplied their own data."""
    rows = [
        {"age": 67, "duration": 6, "credit_amount": 1169, "sex": "male", "default": 0},
        {"age": 22, "duration": 48, "credit_amount": 5951, "sex": "female", "default": 1},
        {"age": 49, "duration": 12, "credit_amount": 2096, "sex": "male", "default": 0},
        {"age": 45, "duration": 42, "credit_amount": 7882, "sex": "male", "default": 0},
        {"age": 53, "duration": 24, "credit_amount": 4870, "sex": "male", "default": 1},
        {"age": 35, "duration": 36, "credit_amount": 9055, "sex": "male", "default": 0},
        {"age": 53, "duration": 24, "credit_amount": 2835, "sex": "male", "default": 0},
        {"age": 35, "duration": 36, "credit_amount": 6948, "sex": "male", "default": 0},
        {"age": 61, "duration": 12, "credit_amount": 3059, "sex": "male", "default": 0},
        {"age": 28, "duration": 30, "credit_amount": 5234, "sex": "male", "default": 1},
        {"age": 25, "duration": 12, "credit_amount": 1295, "sex": "female", "default": 1},
        {"age": 24, "duration": 48, "credit_amount": 4308, "sex": "female", "default": 1},
        {"age": 22, "duration": 12, "credit_amount": 1567, "sex": "female", "default": 0},
        {"age": 60, "duration": 24, "credit_amount": 1199, "sex": "male", "default": 1},
        {"age": 28, "duration": 15, "credit_amount": 1403, "sex": "female", "default": 0},
        {"age": 32, "duration": 24, "credit_amount": 1282, "sex": "female", "default": 1},
        {"age": 53, "duration": 24, "credit_amount": 2424, "sex": "male", "default": 0},
        {"age": 25, "duration": 30, "credit_amount": 8072, "sex": "male", "default": 0},
        {"age": 44, "duration": 24, "credit_amount": 12579, "sex": "female", "default": 1},
        {"age": 31, "duration": 24, "credit_amount": 3430, "sex": "male", "default": 0},
    ]
    return {
        "rows": rows,
        "target": "default",
        "feature_names": ["age", "duration", "credit_amount", "sex"],
        "name": "sample_german_credit",
    }


def _scan_to_jsonable(scan_results: Any) -> dict:
    """Coerce a giskard.scan() result into a JSON-serialisable summary.

    The Giskard ScanReport object exposes `to_dict()` / `to_html()` /
    `issues` depending on version — we try them in order and fall back to a
    string representation so the response is never empty."""
    if hasattr(scan_results, "to_dict"):
        try:
            return {"status": "completed", "report": scan_results.to_dict()}
        except Exception:  # noqa: S110  fall through to issues / summary
            pass
    if hasattr(scan_results, "issues"):
        try:
            issues = []
            for issue in scan_results.issues or []:
                issues.append({
                    "group": getattr(issue, "group", None) and str(issue.group),
                    "level": getattr(issue, "level", None) and str(issue.level),
                    "description": getattr(issue, "description", None),
                    "slicing_fn": str(getattr(issue, "slicing_fn", "") or ""),
                })
            return {"status": "completed", "issues": issues, "issue_count": len(issues)}
        except Exception:  # noqa: S110  fall through to string summary
            pass
    return {"status": "completed", "summary": str(scan_results)}


def run_local_giskard_scan(
    model_config: dict | None = None,
    dataset_config: dict | None = None,
) -> dict:
    """Run a Giskard OSS vulnerability scan locally.

    Args:
        model_config:   {"name": str, "description": str,
                         "feature_names": [str], "classification_labels": [...]}
                        — optional.  If absent, a trivial baseline classifier
                        is built that predicts the target column directly,
                        which is enough for Giskard to exercise its data /
                        slicing detectors.
        dataset_config: {"rows": [{...}], "target": str, "name": str,
                         "feature_names": [str]} — optional; falls back to
                         the built-in sample (German Credit).

    Returns: a JSON-serialisable dict with the scan summary.
    """
    import giskard
    import pandas as pd

    ds_cfg = dataset_config or _build_sample_dataset()
    mc = model_config or {}

    df = pd.DataFrame(ds_cfg["rows"])
    target = ds_cfg["target"]
    feature_names = ds_cfg.get("feature_names") or [c for c in df.columns if c != target]

    # Baseline predict function — copies the target column.  This lets Giskard
    # run its data drift / leakage / fairness detectors without a real model.
    # When the caller wants a real scan they should pass a real prediction
    # function via a future model-upload endpoint; the OSS scan API is the
    # same either way.
    def predict_fn(input_df: "pd.DataFrame"):
        if target in input_df.columns:
            return input_df[target].astype(int).tolist()
        return [0] * len(input_df)

    giskard_model = giskard.Model(
        model=predict_fn,
        model_type=mc.get("model_type", "classification"),
        name=mc.get("name", "bytoid_baseline_model"),
        description=mc.get("description", "Baseline model for OSS Giskard scan"),
        feature_names=feature_names,
        classification_labels=mc.get("classification_labels", [0, 1]),
    )

    giskard_dataset = giskard.Dataset(
        df=df,
        target=target,
        name=ds_cfg.get("name", "bytoid_dataset"),
    )

    scan_results = giskard.scan(giskard_model, giskard_dataset)
    return _scan_to_jsonable(scan_results)


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
