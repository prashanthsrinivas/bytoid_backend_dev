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


class GiskardUnavailable(RuntimeError):
    """Raised when the ``giskard`` package can't be imported in this environment.

    Scan tasks catch this and report a clean ``status=error`` instead of
    burning Celery retries on an ``ImportError`` (giskard is an optional,
    worker-only dependency — see ``requirements.txt``)."""


def require_giskard():
    """Return the imported ``giskard`` module or raise ``GiskardUnavailable``."""
    try:
        import giskard
    except Exception as exc:  # ImportError + any transitive init failure
        raise GiskardUnavailable(f"giskard import failed: {exc}") from exc
    return giskard


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


def _jsonable_metrics(meta: Any) -> dict:
    """Keep only the JSON-primitive entries from a Giskard issue's `meta` dict
    (metric name → value), so the result survives the Celery/Redis backend."""
    out: dict = {}
    if not isinstance(meta, dict):
        return out
    for key, value in meta.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = value
    return out


def _scan_to_jsonable(scan_results: Any) -> dict:
    """Coerce a giskard.scan() result into a consistent, drill-down-friendly
    summary.

    The Giskard ScanReport exposes an `.issues` list (each issue has `group`,
    `level`, `description`, `slicing_fn`, `features`, `meta`).  We normalise
    every issue into a flat dict and add a `counts_by_level` rollup so the
    frontend can render a severity summary + an expandable per-issue table.
    Falls back to a string summary so the response is never empty."""
    raw_issues = getattr(scan_results, "issues", None)
    if raw_issues is None and not hasattr(scan_results, "issues"):
        return {"status": "completed", "issue_count": 0, "summary": str(scan_results)}

    issues: list[dict] = []
    counts_by_level: dict[str, int] = {}
    for issue in raw_issues or []:
        level_obj = getattr(issue, "level", None)
        level = getattr(level_obj, "value", None) or (str(level_obj) if level_obj is not None else "unknown")
        group_obj = getattr(issue, "group", None)
        group = getattr(group_obj, "name", None) or (str(group_obj) if group_obj is not None else None)

        counts_by_level[level] = counts_by_level.get(level, 0) + 1
        issues.append({
            "group": group,
            "level": level,
            "description": getattr(issue, "description", None),
            "slice": str(getattr(issue, "slicing_fn", "") or "") or None,
            "features": list(getattr(issue, "features", []) or []),
            "metrics": _jsonable_metrics(getattr(issue, "meta", None)),
        })

    return {
        "status": "completed",
        "issue_count": len(issues),
        "counts_by_level": counts_by_level,
        "issues": issues,
    }


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

    import numpy as np

    df = pd.DataFrame(ds_cfg["rows"])
    target = ds_cfg["target"]
    feature_names = ds_cfg.get("feature_names") or [c for c in df.columns if c != target]
    model_type = mc.get("model_type", "classification")
    classification_labels = mc.get("classification_labels", [0, 1])

    # Baseline predict function — copies the target column.  This lets Giskard
    # run its data drift / leakage / fairness detectors without a real model.
    # When the caller wants a real scan they should pass a real prediction
    # function via a future model-upload endpoint; the OSS scan API is the
    # same either way.
    #
    # Giskard requires a *classification* model's predict function to return
    # per-class probabilities (floats), shaped (n_rows, n_classes) and aligned
    # to `classification_labels` — not the integer class labels.  We emit a
    # one-hot probability matrix from the copied target column.  For a
    # regression model we return float predictions directly.
    label_index = {label: i for i, label in enumerate(classification_labels)}

    def predict_fn(input_df: "pd.DataFrame"):
        if target in input_df.columns:
            raw = input_df[target].tolist()
        else:
            raw = [classification_labels[0]] * len(input_df)

        if model_type == "classification":
            probs = np.zeros((len(raw), len(classification_labels)), dtype=float)
            for row, value in enumerate(raw):
                probs[row, label_index.get(value, 0)] = 1.0
            return probs
        return np.asarray(raw, dtype=float)

    giskard_model = giskard.Model(
        model=predict_fn,
        model_type=model_type,
        name=mc.get("name", "bytoid_baseline_model"),
        description=mc.get("description", "Baseline model for OSS Giskard scan"),
        feature_names=feature_names,
        classification_labels=classification_labels,
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
