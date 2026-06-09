"""Evidence mapping + AI-analysis context + runbook bridge tests (Phase 3)."""

import pytest

from vra import evidence as ev
from vra import report_inputs as ri
from vra import runbook_bridge as bridge
from vra.osint.normalize import build_snapshot, make_finding
from vra.schema import CAT_BREACH, CAT_SECURITY, SEV_CRITICAL, SEV_HIGH, SEV_INFO, SEV_MEDIUM
from vra.service import VraService


def _snap(findings, *, scanned_at="2026-06-08T00:00:00Z", scan_id="s1"):
    return build_snapshot(
        scan_id=scan_id, assessment_id="a1", vendor_name="Acme",
        vendor_domain="acme.com", findings=findings, scanned_at=scanned_at,
    )


def _f(cat, sev, summary="x", url="http://e/1"):
    return make_finding(category=cat, evidence_type="t", source="S",
                        finding_summary=summary, source_url=url, severity=sev)


# ── evidence mapping ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_finding_to_evidence_has_required_fields():
    rec = ev.finding_to_evidence(_f(CAT_SECURITY, SEV_HIGH, "exposed", "http://x"))
    for field in ("source", "collection_date", "evidence_type", "finding_summary",
                  "supporting_details", "risk_indicators"):
        assert field in rec
    assert rec["finding_summary"] == "exposed"
    assert rec["category_label"] == "Security Intelligence"


@pytest.mark.unit
def test_snapshot_to_evidence_counts_artifacts():
    snap = _snap([_f(CAT_SECURITY, SEV_HIGH, url="http://a"), _f(CAT_BREACH, SEV_MEDIUM, url="")])
    bundle = ev.snapshot_to_evidence(snap)
    assert bundle["total_findings"] == 2
    assert bundle["evidence_artifacts"] == 1   # only one has a source_url


@pytest.mark.unit
def test_evidence_by_category_groups():
    snap = _snap([_f(CAT_SECURITY, SEV_HIGH), _f(CAT_SECURITY, SEV_INFO), _f(CAT_BREACH, SEV_MEDIUM)])
    grouped = ev.evidence_by_category(snap)
    assert len(grouped[CAT_SECURITY]) == 2 and len(grouped[CAT_BREACH]) == 1


# ── analysis context ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_risk_rating_critical_floor():
    snap = _snap([_f(CAT_SECURITY, SEV_CRITICAL)])
    assert ri.risk_rating(snap) == "Critical"


@pytest.mark.unit
def test_risk_rating_bands():
    assert ri.risk_rating(_snap([])) == "Low"
    # medium-only finding -> low/medium score band
    assert ri.risk_rating(_snap([_f(CAT_SECURITY, SEV_MEDIUM)])) in ("Low", "Medium", "High")


@pytest.mark.unit
def test_key_observations_severity_sorted_with_citations():
    snap = _snap([
        _f(CAT_SECURITY, SEV_MEDIUM, "med", "http://m"),
        _f(CAT_SECURITY, SEV_CRITICAL, "crit", "http://c"),
        _f(CAT_BREACH, SEV_INFO, "info", "http://i"),  # excluded (info)
    ])
    obs = ri.key_observations(snap)
    assert [o["severity"] for o in obs] == [SEV_CRITICAL, SEV_MEDIUM]
    assert obs[0]["source_url"] == "http://c"


@pytest.mark.unit
def test_trend_direction():
    assert ri.trend(None, 50)["direction"] == "baseline"
    assert ri.trend([20.0], 50.0)["direction"] == "worsening"
    assert ri.trend([60.0], 40.0)["direction"] == "improving"
    assert ri.trend([50.0], 50.0)["direction"] == "stable"


@pytest.mark.unit
def test_build_context_and_traceability():
    snap = _snap([_f(CAT_SECURITY, SEV_CRITICAL, "boom", "http://c")])
    ctx = ri.build_analysis_context(snap, prior_scores=[10.0])
    assert ctx["risk_rating"] == "Critical"
    assert ctx["trend"]["direction"] == "worsening"
    assert ctx["traceability"] and ctx["traceability"][0]["source_url"] == "http://c"


@pytest.mark.unit
def test_render_markdown_cites_sources():
    snap = _snap([_f(CAT_SECURITY, SEV_HIGH, "exposed port", "http://src")])
    md = ri.render_context_markdown(ri.build_analysis_context(snap))
    assert "OSINT Intelligence Assessment" in md
    assert "[source](http://src)" in md
    assert "traceable" in md.lower()


@pytest.mark.unit
def test_render_markdown_no_findings():
    md = ri.render_context_markdown(ri.build_analysis_context(_snap([])))
    assert "No material" in md


# ── runbook bridge ───────────────────────────────────────────────────────────

class _MemStore:
    def __init__(self):
        self.db = {}

    def get_assessment(self, u, a):
        return self.db.get((u, a))

    def save_assessment(self, u, r):
        self.db[(u, r["assessment_id"])] = dict(r)
        return r


class _Task:
    def __init__(self):
        self.calls = []

    def delay(self, *args):
        self.calls.append(args)


@pytest.fixture
def svc():
    s = VraService(storage=_MemStore())
    s.storage.save_assessment("u1", {"assessment_id": "a1", "vendor_name": "Acme"})
    return s


@pytest.mark.unit
def test_link_runbook(svc):
    rec = bridge.link_runbook("u1", "a1", runbook_id="rb1", playbook_id="pb1", service=svc)
    assert rec["runbook_id"] == "rb1" and rec["playbook_id"] == "pb1"
    assert bridge.link_runbook("u1", "missing", runbook_id="x", service=svc) is None


@pytest.mark.unit
def test_regeneration_not_linked(svc):
    out = bridge.request_regeneration("u1", "a1", service=svc, task=_Task())
    assert out["status"] == "not_linked"


@pytest.mark.unit
def test_regeneration_queues_existing_task(svc):
    bridge.link_runbook("u1", "a1", runbook_id="rb1", playbook_id="pb1", service=svc)
    task = _Task()
    out = bridge.request_regeneration("u1", "a1", service=svc, task=task)
    assert out["status"] == "queued"
    assert task.calls == [("u1", "pb1", "rb1")]


@pytest.mark.unit
def test_regeneration_not_found():
    out = bridge.request_regeneration("u1", "nope", service=VraService(storage=_MemStore()), task=_Task())
    assert out["status"] == "not_found"
