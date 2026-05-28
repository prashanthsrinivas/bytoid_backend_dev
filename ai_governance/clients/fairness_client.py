"""Fairness analysis wrappers for AIF360, Fairlearn, and Aequitas.

All three functions are stateless — they accept plain Python dicts/lists,
run the analysis, and return a JSON-serialisable dict.  They are designed
to be called from Celery tasks (ai_governance/tasks.py) so that heavy
Pandas/NumPy workloads do not block Gunicorn workers.

A built-in sample dataset (simplified Adult/Census) is provided so the
frontend's "Submit job" button can fire a real Celery task with no
payload and surface real fairness metrics end-to-end.

Imports are deferred to function bodies so the module can be imported
without the AI governance packages installed (unit tests stub them).
"""

from typing import Any


def sample_fairness_dataset() -> dict:
    """Tiny built-in fairness dataset (loosely modelled on Adult/Census):
    `sex` is the protected attribute, `income` the binary label, `score`
    the model prediction.  Same rows are used by all three tools so the
    frontend can fire any of them with no upload step."""
    rows = [
        {"sex": 1, "age": 39, "hours_per_week": 40, "score": 0, "income": 0},
        {"sex": 1, "age": 50, "hours_per_week": 13, "score": 0, "income": 0},
        {"sex": 1, "age": 38, "hours_per_week": 40, "score": 0, "income": 0},
        {"sex": 1, "age": 53, "hours_per_week": 40, "score": 0, "income": 0},
        {"sex": 0, "age": 28, "hours_per_week": 40, "score": 0, "income": 0},
        {"sex": 0, "age": 37, "hours_per_week": 40, "score": 0, "income": 0},
        {"sex": 0, "age": 49, "hours_per_week": 16, "score": 0, "income": 0},
        {"sex": 1, "age": 52, "hours_per_week": 45, "score": 1, "income": 1},
        {"sex": 1, "age": 31, "hours_per_week": 50, "score": 1, "income": 1},
        {"sex": 1, "age": 42, "hours_per_week": 40, "score": 1, "income": 1},
        {"sex": 1, "age": 37, "hours_per_week": 80, "score": 1, "income": 1},
        {"sex": 0, "age": 30, "hours_per_week": 40, "score": 0, "income": 1},
        {"sex": 1, "age": 23, "hours_per_week": 30, "score": 0, "income": 0},
        {"sex": 0, "age": 32, "hours_per_week": 50, "score": 1, "income": 1},
        {"sex": 1, "age": 40, "hours_per_week": 40, "score": 0, "income": 1},
        {"sex": 0, "age": 34, "hours_per_week": 45, "score": 1, "income": 1},
        {"sex": 1, "age": 25, "hours_per_week": 35, "score": 0, "income": 0},
        {"sex": 1, "age": 32, "hours_per_week": 40, "score": 1, "income": 0},
        {"sex": 0, "age": 38, "hours_per_week": 45, "score": 1, "income": 1},
        {"sex": 1, "age": 43, "hours_per_week": 45, "score": 0, "income": 0},
    ]
    return {
        "rows": rows,
        "protected_attribute": "sex",
        "label_col": "income",
        "score_col": "score",
        "feature_cols": ["age", "hours_per_week", "sex"],
    }


def run_aif360_metrics(
    dataset_dict: dict,
    privileged_groups: list[dict],
    unprivileged_groups: list[dict],
) -> dict:
    """Compute AIF360 binary label dataset metrics.

    Args:
        dataset_dict: {"df": [{col: val, ...}, ...], "label_col": str,
                       "favorable_label": 1, "protected_attribute_names": [str]}
        privileged_groups:   e.g. [{"race": 1}]
        unprivileged_groups: e.g. [{"race": 0}]

    Returns:
        dict with disparate_impact, statistical_parity_difference, etc.
    """
    import pandas as pd
    from aif360.datasets import BinaryLabelDataset
    from aif360.metrics import BinaryLabelDatasetMetric

    df = pd.DataFrame(dataset_dict["df"])
    label_col = dataset_dict["label_col"]
    favorable_label = dataset_dict.get("favorable_label", 1)
    protected_attribute_names = dataset_dict["protected_attribute_names"]

    bld = BinaryLabelDataset(
        df=df,
        label_names=[label_col],
        protected_attribute_names=protected_attribute_names,
        favorable_label=favorable_label,
        unfavorable_label=1 - favorable_label,
    )

    metric = BinaryLabelDatasetMetric(
        bld,
        privileged_groups=privileged_groups,
        unprivileged_groups=unprivileged_groups,
    )

    return {
        "disparate_impact": metric.disparate_impact(),
        "statistical_parity_difference": metric.statistical_parity_difference(),
        "consistency": metric.consistency()[0],
        "num_positives_privileged": metric.num_positives(privileged=True),
        "num_positives_unprivileged": metric.num_positives(privileged=False),
    }


def run_fairlearn_mitigation(
    X_dict: list[dict],
    y: list[Any],
    sensitive_features: list[Any],
    estimator_config: dict,
) -> dict:
    """Apply Fairlearn ExponentiatedGradient with DemographicParity.

    Args:
        X_dict:             list of feature-row dicts
        y:                  target labels (0/1)
        sensitive_features: list of sensitive attribute values (one per row)
        estimator_config:   {"type": "logistic_regression", "C": 1.0} etc.

    Returns:
        dict with fairness_constraint, n_predictors, and predictors summary.
    """
    import pandas as pd
    from fairlearn.reductions import DemographicParity, ExponentiatedGradient
    from sklearn.linear_model import LogisticRegression

    X = pd.DataFrame(X_dict)
    estimator_type = estimator_config.get("type", "logistic_regression")

    if estimator_type == "logistic_regression":
        base_estimator = LogisticRegression(
            C=estimator_config.get("C", 1.0),
            max_iter=estimator_config.get("max_iter", 200),
        )
    else:
        raise ValueError(f"Unsupported estimator type: {estimator_type}")

    mitigator = ExponentiatedGradient(
        base_estimator,
        constraints=DemographicParity(),
    )
    mitigator.fit(X, y, sensitive_features=sensitive_features)

    return {
        "fairness_constraint": "DemographicParity",
        "estimator_type": estimator_type,
        "n_predictors": len(mitigator.predictors_),
        "predictors_summary": [
            {"weight": float(w)}
            for w in mitigator.weights_
        ],
    }


def run_aequitas_audit(
    df_dict: list[dict],
    score_col: str,
    label_col: str,
    attr_cols: list[str],
) -> dict:
    """Run an Aequitas bias audit.

    Args:
        df_dict:   list of row dicts — must include score_col, label_col,
                   and all attr_cols
        score_col: column name of the model score/prediction (0/1)
        label_col: column name of the ground-truth label (0/1)
        attr_cols: list of sensitive attribute column names

    Returns:
        dict with group-level bias metrics and fairness summary.
    """
    import pandas as pd
    from aequitas.bias import Bias
    from aequitas.fairness import Fairness
    from aequitas.group import Group

    df = pd.DataFrame(df_dict)

    # Aequitas requires columns named "score" and "label_value"
    df = df.rename(columns={score_col: "score", label_col: "label_value"})

    g = Group()
    xtab, _ = g.get_crosstabs(df, attr_cols=attr_cols)

    b = Bias()
    bdf = b.get_disparity_predefined_groups(
        xtab,
        original_df=df,
        ref_groups_dict={col: df[col].mode()[0] for col in attr_cols},
    )

    f = Fairness()
    fdf = f.get_group_value_fairness(bdf)

    return {
        "group_metrics": xtab.to_dict(orient="records"),
        "bias_metrics": bdf[[c for c in bdf.columns if "disparity" in c or "parity" in c]].to_dict(orient="records"),
        "fairness_summary": fdf[["attribute_name", "attribute_value", "Fairness Determined"]].to_dict(orient="records")
        if "Fairness Determined" in fdf.columns
        else [],
    }
