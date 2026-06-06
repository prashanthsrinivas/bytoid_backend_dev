"""Unit tests for runbook/risk_engine.py.

Covers the deterministic scorer's new ``finding_id`` stamping and the
``apply_risk_overrides`` preservation layer that keeps manual risk overrides alive
across re-runs / chat-modify. Importing ``risk_engine`` drags in ``db.rds_db`` (which
hits AWS Secrets Manager at import), so these tests use the shared ``db_stubs``
fixture and import the module lazily against the stub.
"""

import sys

import pytest


@pytest.fixture
def risk_engine(db_stubs):
    # Re-import against the db.rds_db stub installed by db_stubs.
    sys.modules.pop("runbook.risk_engine", None)
    from runbook import risk_engine as mod

    yield mod
    sys.modules.pop("runbook.risk_engine", None)


def _risks():
    return [
        {"threat": "Data exfiltration", "vulnerability": "Logs leak PII",
         "impact": 5, "likelihood": 5},
        {"threat": "Weak alerting", "vulnerability": "No SIEM rules",
         "impact": 2, "likelihood": 2},
    ]


# ── finding_id ────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_compute_risk_stamps_stable_finding_id(risk_engine):
    a = risk_engine.compute_risk(_risks())
    b = risk_engine.compute_risk(_risks())
    ids_a = [r["finding_id"] for r in a["risks"]]
    ids_b = [r["finding_id"] for r in b["risks"]]
    assert all(ids_a)  # non-empty
    assert ids_a == ids_b  # deterministic across runs
    assert ids_a[0] != ids_a[1]  # distinct findings → distinct ids
    assert a["rev"] == 0  # fresh analysis starts at rev 0


# ── report-level override preservation ─────────────────────────────────────────

@pytest.mark.unit
def test_apply_overrides_preserves_report_level(risk_engine):
    prior = risk_engine.compute_risk(_risks())
    prior["risk_overridden"] = True
    prior["final_risk_score"] = 60
    prior["risk_level"] = "Critical"
    prior["rev"] = 3

    fresh = risk_engine.compute_risk(_risks())
    out, dropped = risk_engine.apply_risk_overrides(fresh, prior)

    assert out["risk_overridden"] is True
    assert out["final_risk_score"] == 60
    assert out["risk_level"] == "Critical"
    assert dropped == []


@pytest.mark.unit
def test_report_override_survives_even_if_findings_change(risk_engine):
    prior = risk_engine.compute_risk(_risks())
    prior["risk_overridden"] = True
    prior["final_risk_score"] = 42
    prior["risk_level"] = "High"

    different = risk_engine.compute_risk([
        {"threat": "Totally new", "vulnerability": "different finding",
         "impact": 1, "likelihood": 1},
    ])
    out, _ = risk_engine.apply_risk_overrides(different, prior)
    assert out["final_risk_score"] == 42
    assert out["risk_level"] == "High"


# ── finding-level override preservation + drops ─────────────────────────────────

@pytest.mark.unit
def test_apply_overrides_preserves_matching_finding(risk_engine):
    prior = risk_engine.compute_risk(_risks())
    # User overrides the first finding.
    prior["risks"][0]["overridden"] = True
    prior["risks"][0]["risk_score"] = 99
    prior["risks"][0]["risk_level"] = "Critical"

    fresh = risk_engine.compute_risk(_risks())
    out, dropped = risk_engine.apply_risk_overrides(fresh, prior)

    matched = next(r for r in out["risks"]
                   if r["finding_id"] == prior["risks"][0]["finding_id"])
    assert matched["overridden"] is True
    assert matched["risk_score"] == 99
    assert matched["risk_level"] == "Critical"
    # The untouched finding stays engine-computed (no override flag).
    other = next(r for r in out["risks"]
                 if r["finding_id"] != prior["risks"][0]["finding_id"])
    assert "overridden" not in other
    assert dropped == []


@pytest.mark.unit
def test_unmatched_finding_override_is_dropped_not_silent(risk_engine):
    prior = risk_engine.compute_risk(_risks())
    prior["risks"][0]["overridden"] = True
    prior["risks"][0]["risk_score"] = 99

    # Regeneration reworded the first finding so its content hash no longer matches.
    reworded = risk_engine.compute_risk([
        {"threat": "Data exfiltration (reworded)", "vulnerability": "logs expose secrets",
         "impact": 5, "likelihood": 5},
        {"threat": "Weak alerting", "vulnerability": "No SIEM rules",
         "impact": 2, "likelihood": 2},
    ])
    out, dropped = risk_engine.apply_risk_overrides(reworded, prior)
    assert len(dropped) == 1
    assert dropped[0]["risk_score"] == 99
    # No fresh finding falsely carries the override.
    assert not any(r.get("overridden") for r in out["risks"])


# ── rev monotonicity ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_rev_carried_forward_never_resets(risk_engine):
    prior = risk_engine.compute_risk(_risks())
    prior["rev"] = 7  # several prior edits

    fresh = risk_engine.compute_risk(_risks())  # fresh analysis has rev 0
    out, _ = risk_engine.apply_risk_overrides(fresh, prior)
    assert out["rev"] == 7  # carried forward, not reset to 0


@pytest.mark.unit
def test_apply_overrides_noop_on_empty_prior(risk_engine):
    fresh = risk_engine.compute_risk(_risks())
    out, dropped = risk_engine.apply_risk_overrides(fresh, None)
    assert out is fresh
    assert dropped == []
    _out2, dropped2 = risk_engine.apply_risk_overrides(fresh, {})
    assert dropped2 == []
