"""Finding-contract tests — vra/osint/normalize.py (shared app<->Lambda schema)."""

import pytest

from vra.osint import normalize
from vra.schema import (
    CAT_BREACH,
    CAT_SECURITY,
    SEV_CRITICAL,
    SEV_INFO,
    SEV_LOW,
    SEV_MEDIUM,
)


@pytest.mark.unit
def test_make_finding_minimal_defaults():
    f = normalize.make_finding(
        category=CAT_SECURITY,
        evidence_type="ssl_certificate",
        source="crt.sh",
        finding_summary="Expired certificate",
    )
    assert f["category"] == CAT_SECURITY
    assert f["severity"] == SEV_INFO
    assert f["risk_indicators"] == []
    assert f["supporting_details"] == {}
    assert f["collected_at"].endswith("Z")


@pytest.mark.unit
def test_make_finding_rejects_bad_category():
    with pytest.raises(ValueError):
        normalize.make_finding(
            category="bogus", evidence_type="x", source="y", finding_summary="z"
        )


@pytest.mark.unit
def test_make_finding_rejects_bad_severity():
    with pytest.raises(ValueError):
        normalize.make_finding(
            category=CAT_SECURITY,
            evidence_type="x",
            source="y",
            finding_summary="z",
            severity="apocalyptic",
        )


@pytest.mark.unit
def test_make_finding_strips_blank_risk_indicators():
    f = normalize.make_finding(
        category=CAT_BREACH,
        evidence_type="breach",
        source="HIBP",
        finding_summary="Breach disclosed",
        risk_indicators=["  exposed_emails ", "", "   "],
    )
    assert f["risk_indicators"] == ["exposed_emails"]


@pytest.mark.unit
def test_severity_and_category_counts_zero_filled():
    findings = [
        normalize.make_finding(category=CAT_SECURITY, evidence_type="a", source="s", finding_summary="x", severity=SEV_LOW),
        normalize.make_finding(category=CAT_SECURITY, evidence_type="b", source="s", finding_summary="y", severity=SEV_CRITICAL),
    ]
    sev = normalize.severity_counts(findings)
    cat = normalize.category_counts(findings)
    assert sev[SEV_LOW] == 1 and sev[SEV_CRITICAL] == 1 and sev[SEV_INFO] == 0
    assert cat[CAT_SECURITY] == 2 and cat[CAT_BREACH] == 0


@pytest.mark.unit
def test_snapshot_risk_score_empty_is_zero():
    assert normalize.snapshot_risk_score([]) == 0.0


@pytest.mark.unit
def test_snapshot_risk_score_one_critical_dominates():
    findings = [
        normalize.make_finding(category=CAT_SECURITY, evidence_type="a", source="s", finding_summary="x", severity=SEV_CRITICAL),
        normalize.make_finding(category=CAT_SECURITY, evidence_type="b", source="s", finding_summary="y", severity=SEV_INFO),
    ]
    score = normalize.snapshot_risk_score(findings)
    # 0.7*100 + 0.3*mean(100,0)=50 -> 70 + 15 = 85.0
    assert score == 85.0


@pytest.mark.unit
def test_build_snapshot_shape():
    findings = [
        normalize.make_finding(category=CAT_SECURITY, evidence_type="a", source="s", finding_summary="x", severity=SEV_MEDIUM)
    ]
    snap = normalize.build_snapshot(
        scan_id="scan1",
        assessment_id="a1",
        vendor_name="Acme",
        vendor_domain="acme.com",
        findings=findings,
    )
    assert snap["scan_id"] == "scan1"
    assert snap["counts"]["total"] == 1
    assert snap["counts"]["by_severity"][SEV_MEDIUM] == 1
    assert snap["risk_score"] > 0
    assert snap["scanned_at"].endswith("Z")


@pytest.mark.unit
def test_validate_finding_accepts_make_finding_output():
    f = normalize.make_finding(category=CAT_SECURITY, evidence_type="a", source="s", finding_summary="x")
    assert normalize.validate_finding(f) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"category": CAT_SECURITY},                       # missing fields
        "not-a-dict",
        {"category": "bogus", "evidence_type": "a", "source": "s",
         "finding_summary": "x", "severity": SEV_LOW, "collected_at": "t"},  # bad cat
    ],
)
def test_validate_finding_rejects_malformed(bad):
    assert normalize.validate_finding(bad) is False
