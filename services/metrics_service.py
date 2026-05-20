"""Prometheus-backed metrics service.

Drop-in replacement for the MetricsClient stub in db/lance_db_service.py.

Usage:
    from services.metrics_service import metrics
    metrics.increment("workflow.transition.count", tags={"org_id": "x", "to_state": "approved"})
    metrics.timing("fireworks.call.latency_ms", 420.0, tags={"feature": "edit"})
    metrics.gauge("workflow_email.dlq.depth", 12, tags={"org_id": "x"})

A /metrics Flask endpoint is registered via register_metrics_endpoint(app).
In IS_DEV mode every metric event also logs one INFO line to the app logger.
"""

import os
from typing import Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

from utils.app_configs import IS_DEV
from utils.base_logger import get_logger

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")

# ── Registry ──────────────────────────────────────────────────────────────────

_registry = CollectorRegistry(auto_describe=True)

# ── Counter definitions ───────────────────────────────────────────────────────

_COUNTERS: dict[str, Counter] = {
    "workflow.transition.count": Counter(
        "workflow_transition_total",
        "Workflow state transitions",
        ["org_id", "doc_type", "from_state", "to_state"],
        registry=_registry,
    ),
    "workflow.optimistic_lock.conflict.count": Counter(
        "workflow_optimistic_lock_conflict_total",
        "Workflow optimistic-lock 409 conflicts",
        ["doc_type"],
        registry=_registry,
    ),
    "workflow_email.sent.count": Counter(
        "workflow_email_sent_total",
        "Workflow emails sent",
        ["template_name"],
        registry=_registry,
    ),
    "workflow_email.retry.count": Counter(
        "workflow_email_retry_total",
        "Workflow email Celery retries",
        ["template_name"],
        registry=_registry,
    ),
    "workflow_email.dlq.enqueued.count": Counter(
        "workflow_email_dlq_enqueued_total",
        "Workflow emails written to DLQ",
        ["org_id"],
        registry=_registry,
    ),
    "workflow_email.dlq.cap_reached.count": Counter(
        "workflow_email_dlq_cap_reached_total",
        "Times DLQ daily cap was hit",
        ["org_id"],
        registry=_registry,
    ),
    "policy_migration.chunk.count": Counter(
        "policy_migration_chunk_total",
        "Migration chunks processed",
        ["org_id"],
        registry=_registry,
    ),
    "policy_migration.failure.count": Counter(
        "policy_migration_failure_total",
        "Policies that failed migration",
        ["org_id"],
        registry=_registry,
    ),
    "policy_statement.reconcile.similarity_recovery.count": Counter(
        "policy_statement_reconcile_similarity_recovery_total",
        "Statement IDs recovered via similarity (not preserved by LLM)",
        [],
        registry=_registry,
    ),
    "policy_statement.reconcile.superseded.count": Counter(
        "policy_statement_reconcile_superseded_total",
        "Statement IDs marked superseded after LLM edit",
        [],
        registry=_registry,
    ),
    "tracker_policy_mapping.rows_matched.count": Counter(
        "tracker_policy_mapping_rows_matched_total",
        "Tracker rows matched to policy statements",
        ["policy_id"],
        registry=_registry,
    ),
    "fireworks.call.count": Counter(
        "fireworks_call_total",
        "Fireworks LLM calls",
        ["feature"],
        registry=_registry,
    ),
    "policy_yaml.etag.conflict.count": Counter(
        "policy_yaml_etag_conflict_total",
        "Policy YAML etag 409 conflicts",
        [],
        registry=_registry,
    ),
}

# ── Histogram definitions ─────────────────────────────────────────────────────

_HISTOGRAMS: dict[str, Histogram] = {
    "workflow.transition.latency_ms": Histogram(
        "workflow_transition_latency_ms",
        "Workflow transition latency in milliseconds",
        ["doc_type", "to_state"],
        buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000],
        registry=_registry,
    ),
    "fireworks.call.latency_ms": Histogram(
        "fireworks_call_latency_ms",
        "Fireworks LLM call latency in milliseconds",
        ["feature"],
        buckets=[500, 1000, 2500, 5000, 10000, 30000, 60000],
        registry=_registry,
    ),
}

# ── Gauge definitions ─────────────────────────────────────────────────────────

_GAUGES: dict[str, Gauge] = {
    "workflow_email.dlq.depth": Gauge(
        "workflow_email_dlq_depth",
        "Current depth of the workflow email DLQ",
        ["org_id"],
        registry=_registry,
    ),
}


# ── Public facade ─────────────────────────────────────────────────────────────


class _MetricsService:
    """Backwards-compatible facade matching the MetricsClient stub interface."""

    def increment(self, name: str, value: float = 1, tags: Optional[dict] = None):
        tags = tags or {}
        if IS_DEV:
            logger.info("metric=%s value=%s tags=%s", name, value, tags)
        counter = _COUNTERS.get(name)
        if counter is None:
            return
        target = counter.labels(**_label_values(counter, tags)) if counter._labelnames else counter  # noqa: SLF001
        target.inc(value)

    def timing(self, name: str, ms: float, tags: Optional[dict] = None):
        tags = tags or {}
        if IS_DEV:
            logger.info("metric=%s value=%sms tags=%s", name, ms, tags)
        hist = _HISTOGRAMS.get(name)
        if hist is None:
            return
        target = hist.labels(**_label_values(hist, tags)) if hist._labelnames else hist  # noqa: SLF001
        target.observe(ms)

    def gauge(self, name: str, value: float, tags: Optional[dict] = None):
        tags = tags or {}
        if IS_DEV:
            logger.info("metric=%s value=%s tags=%s", name, value, tags)
        g = _GAUGES.get(name)
        if g is None:
            return
        target = g.labels(**_label_values(g, tags)) if g._labelnames else g  # noqa: SLF001
        target.set(value)


def _label_values(metric, tags: dict) -> dict:
    """Return only the label names the metric was registered with, defaulting missing ones to ''."""
    label_names = metric._labelnames  # noqa: SLF001
    return {k: str(tags.get(k, "")) for k in label_names}


metrics = _MetricsService()


# ── Flask /metrics endpoint ───────────────────────────────────────────────────


def register_metrics_endpoint(app):
    """Register GET /metrics on the Flask app, gated by METRICS_SCRAPE_ALLOWED_IPS."""
    from flask import request as _req, Response

    allowed_ips_raw = os.getenv("METRICS_SCRAPE_ALLOWED_IPS", "127.0.0.1")
    allowed_ips = {ip.strip() for ip in allowed_ips_raw.split(",") if ip.strip()}

    @app.route("/metrics", methods=["GET"])
    def _prometheus_metrics():
        remote = _req.remote_addr or ""
        if remote not in allowed_ips:
            return Response("Forbidden", status=403)
        return Response(generate_latest(_registry), mimetype=CONTENT_TYPE_LATEST)
