"""Unit tests for services.metrics_service.

Verifies the Prometheus-backed metrics facade behaves like the stub
MetricsClient interface, that unknown metric names are silently ignored
(no crash), that label cardinality is enforced, and that /metrics is
guarded by the IP allowlist.
"""

import os

import pytest


@pytest.fixture
def metrics_module():
    from services import metrics_service
    return metrics_service


def test_increment_known_counter(metrics_module):
    """increment() on a registered counter updates the Prometheus counter."""
    before = _read_counter(
        metrics_module._registry,
        "workflow_optimistic_lock_conflict_total",
        {"doc_type": "policy"},
    )
    metrics_module.metrics.increment(
        "workflow.optimistic_lock.conflict.count",
        tags={"doc_type": "policy"},
    )
    after = _read_counter(
        metrics_module._registry,
        "workflow_optimistic_lock_conflict_total",
        {"doc_type": "policy"},
    )
    assert after == before + 1


def test_increment_with_value(metrics_module):
    before = _read_counter(
        metrics_module._registry,
        "fireworks_call_total",
        {"feature": "edit"},
    )
    metrics_module.metrics.increment(
        "fireworks.call.count",
        value=3,
        tags={"feature": "edit"},
    )
    after = _read_counter(
        metrics_module._registry,
        "fireworks_call_total",
        {"feature": "edit"},
    )
    assert after == before + 3


def test_increment_unknown_metric_does_not_raise(metrics_module):
    """Unknown metric names are silently ignored — never crash callers."""
    # Should not raise even though the metric isn't registered
    metrics_module.metrics.increment("nonexistent.metric", tags={"x": "y"})


def test_timing_known_histogram(metrics_module):
    """timing() observes into a Prometheus histogram."""
    before_count = _read_histogram_count(
        metrics_module._registry,
        "fireworks_call_latency_ms",
        {"feature": "generation"},
    )
    metrics_module.metrics.timing(
        "fireworks.call.latency_ms",
        420.0,
        tags={"feature": "generation"},
    )
    after_count = _read_histogram_count(
        metrics_module._registry,
        "fireworks_call_latency_ms",
        {"feature": "generation"},
    )
    assert after_count == before_count + 1


def test_timing_unknown_metric_does_not_raise(metrics_module):
    metrics_module.metrics.timing("nonexistent.timing", 100.0, tags={})


def test_gauge_set(metrics_module):
    """gauge() sets a Prometheus gauge to an absolute value."""
    metrics_module.metrics.gauge(
        "workflow_email.dlq.depth",
        42,
        tags={"org_id": "test-org"},
    )
    val = _read_gauge(
        metrics_module._registry,
        "workflow_email_dlq_depth",
        {"org_id": "test-org"},
    )
    assert val == 42

    # Setting again replaces, not adds
    metrics_module.metrics.gauge(
        "workflow_email.dlq.depth",
        7,
        tags={"org_id": "test-org"},
    )
    val2 = _read_gauge(
        metrics_module._registry,
        "workflow_email_dlq_depth",
        {"org_id": "test-org"},
    )
    assert val2 == 7


def test_gauge_unknown_metric_does_not_raise(metrics_module):
    metrics_module.metrics.gauge("nonexistent.gauge", 1.0)


def test_missing_tag_defaults_to_empty_string(metrics_module):
    """If a tag is missing from the call site, it's still recorded under label ''."""
    metrics_module.metrics.increment(
        "workflow.optimistic_lock.conflict.count",
        # No tags at all
    )
    val = _read_counter(
        metrics_module._registry,
        "workflow_optimistic_lock_conflict_total",
        {"doc_type": ""},
    )
    assert val >= 1


def test_extra_tags_are_ignored(metrics_module):
    """Tags not registered on the metric are silently dropped (no exception)."""
    metrics_module.metrics.increment(
        "policy_yaml.etag.conflict.count",
        tags={"unregistered_tag": "should-be-ignored"},
    )


def test_metrics_endpoint_blocks_unauthorized_ip(metrics_module):
    """GET /metrics returns 403 when remote_addr is not in the allowlist."""
    from flask import Flask

    os.environ["METRICS_SCRAPE_ALLOWED_IPS"] = "10.0.0.1"
    app = Flask("metrics-test-app-1")
    metrics_module.register_metrics_endpoint(app)

    with app.test_client() as client:
        # Default test client remote_addr is 127.0.0.1
        resp = client.get("/metrics")
        assert resp.status_code == 403


def test_metrics_endpoint_serves_to_allowed_ip(metrics_module):
    """GET /metrics returns Prometheus exposition when remote_addr is allowed."""
    from flask import Flask

    os.environ["METRICS_SCRAPE_ALLOWED_IPS"] = "127.0.0.1"
    app = Flask("metrics-test-app-2")
    metrics_module.register_metrics_endpoint(app)

    with app.test_client() as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # Sanity-check Prometheus exposition format
        assert "workflow_transition_total" in body or "# HELP" in body


def test_metrics_endpoint_allowlist_parses_csv(metrics_module):
    """METRICS_SCRAPE_ALLOWED_IPS accepts multiple comma-separated IPs."""
    from flask import Flask

    os.environ["METRICS_SCRAPE_ALLOWED_IPS"] = "10.0.0.1,127.0.0.1, 192.168.1.1 "
    app = Flask("metrics-test-app-3")
    metrics_module.register_metrics_endpoint(app)

    with app.test_client() as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200


# ── helpers ───────────────────────────────────────────────────────────────────


def _read_counter(registry, name: str, labels: dict) -> float:
    """Read the current value of a labeled counter from the registry."""
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return 0.0


def _read_histogram_count(registry, name: str, labels: dict) -> float:
    """Read the observation count of a labeled histogram."""
    count_name = f"{name}_count"
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == count_name and sample.labels == labels:
                return sample.value
    return 0.0


def _read_gauge(registry, name: str, labels: dict) -> float:
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None
