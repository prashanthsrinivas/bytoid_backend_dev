"""Guardrail enforcement layer.

Called from inside every LLM wrapper in ``utils/fireworkzz.py`` and
``utils/chatopenzz.py``.  Two entry points:

    check_input(prompt, ctx)  -> str   # may redact; raises on block
    check_output(text, ctx)   -> str   # may redact; raises on block

``ctx`` is a dict carrying ``{user_id, org_admin_id, feature, model, request_id}``.

Rules are loaded via ``rules_store.list_rules_cached`` and dispatched to a
type-specific evaluator.  On match the configured action runs:
    block  → raise GuardrailViolation
    redact → return mutated text
    warn   → log + violation row, pass through
    audit  → violation row only, pass through

The enforcer never raises on infrastructure errors — if the DB or cache is
down the LLM call proceeds (fail-open).  Hard failures only happen for
explicit ``block`` rule matches.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from ai_governance.rules_store import list_rules_cached, record_violation
from services.audit_log_service import AI_GUARDRAIL_VIOLATION, log_audit_event

logger = logging.getLogger(__name__)


# ── Public surface ────────────────────────────────────────────────────────────


class GuardrailViolation(Exception):
    """Raised when a ``block`` rule matches."""

    def __init__(self, rule_id: str, rule_name: str, message: str):
        super().__init__(message)
        self.rule_id = rule_id
        self.rule_name = rule_name
        self.message = message


def check_input(prompt: str, ctx: dict | None = None) -> str:
    """Run all input-facing rules. Returns possibly-redacted prompt."""
    return _check(prompt, ctx or {}, direction="input")


def check_output(text: str, ctx: dict | None = None) -> str:
    """Run all output-facing rules. Returns possibly-redacted text."""
    return _check(text, ctx or {}, direction="output")


# ── Core loop ─────────────────────────────────────────────────────────────────


def _check(text: str, ctx: dict, direction: str) -> str:
    if not text or not isinstance(text, str):
        return text

    org_admin_id = ctx.get("org_admin_id")
    if not org_admin_id:
        return text  # fail-open if we can't resolve the org

    try:
        rules = list_rules_cached(org_admin_id)
    except Exception as exc:
        logger.warning("enforcer: rules fetch failed: %s", exc)
        return text

    feature = ctx.get("feature")
    model = ctx.get("model")
    out = text

    for rule in rules:
        if not _applies(rule, direction, feature, model):
            continue
        try:
            matches = _EVALUATORS[rule["rule_type"]](out, rule, direction)
        except Exception as exc:
            logger.warning(
                "enforcer: evaluator %s raised: %s", rule.get("rule_type"), exc
            )
            continue
        if not matches:
            continue
        out = _act(rule, out, matches, ctx, direction)
    return out


def _applies(rule: dict, direction: str, feature: str | None, model: str | None) -> bool:
    if not rule.get("enabled", True):
        return False
    applies_to = rule.get("applies_to", "both")
    if applies_to != "both" and applies_to != direction:
        return False
    scope = rule.get("scope") or {}
    feats = scope.get("features") or []
    if feats and feature and feature not in feats:
        return False
    models = scope.get("models") or []
    if models and model and model not in models:
        return False
    return True


def _act(
    rule: dict,
    text: str,
    matches: list[dict],
    ctx: dict,
    direction: str,
) -> str:
    action = rule.get("action", "audit")
    rule_id = rule.get("rule_id")
    rule_name = rule.get("name", rule_id)
    excerpt = matches[0].get("excerpt", "") if matches else ""

    _log_violation(rule, action, excerpt, ctx, direction)

    if action == "block":
        raise GuardrailViolation(
            rule_id=rule_id,
            rule_name=rule_name,
            message=f"Blocked by guardrail '{rule_name}'.",
        )
    if action == "redact":
        for m in matches:
            replacement = m.get("replacement", "[REDACTED]")
            span = m.get("span")
            if span:
                start, end = span
                text = text[:start] + replacement + text[end:]
            else:
                needle = m.get("excerpt")
                if needle:
                    text = text.replace(needle, replacement)
        return text
    if action == "warn":
        logger.warning(
            "guardrail warn: rule=%s feature=%s direction=%s",
            rule_name,
            ctx.get("feature"),
            direction,
        )
    return text


def _log_violation(
    rule: dict, action: str, excerpt: str, ctx: dict, direction: str
) -> None:
    payload = {
        "rule_id": rule.get("rule_id"),
        "rule_name": rule.get("name"),
        "org_admin_id": ctx.get("org_admin_id"),
        "user_id": ctx.get("user_id"),
        "feature": ctx.get("feature"),
        "model": ctx.get("model"),
        "direction": direction,
        "action_taken": action,
        "excerpt": excerpt,
        "request_id": ctx.get("request_id"),
    }
    record_violation(payload)
    try:
        log_audit_event(
            AI_GUARDRAIL_VIOLATION,
            endpoint=ctx.get("feature") or "llm_wrapper",
            ip=None,
            status="violation",
            actor_user_id=ctx.get("user_id"),
            metadata={
                "rule_id": rule.get("rule_id"),
                "rule_name": rule.get("name"),
                "action": action,
                "direction": direction,
                "model": ctx.get("model"),
            },
        )
    except Exception:
        logger.debug("enforcer: audit log failed", exc_info=True)


# ── Rule evaluators ───────────────────────────────────────────────────────────
# Each evaluator returns a list of match dicts: [{excerpt, span?, replacement?}, ...]
# Empty list = no match.


def _eval_blocked_phrase(text: str, rule: dict, _direction: str) -> list[dict]:
    cfg = rule.get("config") or {}
    phrases = cfg.get("phrases") or []
    case_sensitive = bool(cfg.get("case_sensitive", False))
    hay = text if case_sensitive else text.lower()
    out: list[dict] = []
    for phrase in phrases:
        if not phrase:
            continue
        needle = phrase if case_sensitive else phrase.lower()
        idx = hay.find(needle)
        if idx >= 0:
            out.append(
                {
                    "excerpt": text[idx : idx + len(phrase)],
                    "span": (idx, idx + len(phrase)),
                    "replacement": cfg.get("replacement", "[REDACTED]"),
                }
            )
    return out


_REGEX_CACHE: dict[str, re.Pattern] = {}
_REGEX_LOCK = threading.Lock()


def _compile_regex(pattern: str, flags: int = 0) -> re.Pattern:
    key = f"{flags}::{pattern}"
    with _REGEX_LOCK:
        compiled = _REGEX_CACHE.get(key)
        if compiled is None:
            compiled = re.compile(pattern, flags)
            _REGEX_CACHE[key] = compiled
    return compiled


def _eval_regex(text: str, rule: dict, _direction: str) -> list[dict]:
    cfg = rule.get("config") or {}
    pattern = cfg.get("pattern")
    if not pattern:
        return []
    flags = re.IGNORECASE if cfg.get("case_insensitive", True) else 0
    try:
        rx = _compile_regex(pattern, flags)
    except re.error:
        return []
    out: list[dict] = []
    for m in rx.finditer(text):
        out.append(
            {
                "excerpt": m.group(0),
                "span": m.span(),
                "replacement": cfg.get("replacement", "[REDACTED]"),
            }
        )
    return out


_PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    ),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ip": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


def _eval_pii(text: str, rule: dict, _direction: str) -> list[dict]:
    cfg = rule.get("config") or {}
    entities = cfg.get("entities") or list(_PII_PATTERNS.keys())
    out: list[dict] = []
    for ent in entities:
        rx = _PII_PATTERNS.get(ent)
        if not rx:
            continue
        for m in rx.finditer(text):
            out.append(
                {
                    "excerpt": m.group(0),
                    "span": m.span(),
                    "replacement": f"[REDACTED:{ent}]",
                }
            )
    return out


def _eval_topic(text: str, rule: dict, _direction: str) -> list[dict]:
    cfg = rule.get("config") or {}
    keywords = [k for k in (cfg.get("keywords") or []) if k]
    threshold = int(cfg.get("threshold", 1))
    if not keywords:
        return []
    low = text.lower()
    hits = [k for k in keywords if k.lower() in low]
    if len(hits) >= threshold:
        return [
            {
                "excerpt": ", ".join(hits)[:200],
                "replacement": cfg.get("replacement", "[TOPIC_BLOCKED]"),
            }
        ]
    return []


def _eval_max_tokens(text: str, rule: dict, direction: str) -> list[dict]:
    if direction != "input":
        return []
    cfg = rule.get("config") or {}
    limit = int(cfg.get("max_words", 0) or 0)
    if limit <= 0:
        return []
    word_count = len(text.split())
    if word_count > limit:
        return [
            {
                "excerpt": f"{word_count} words (limit {limit})",
            }
        ]
    return []


def _eval_model_allowlist(_text: str, _rule: dict, _direction: str) -> list[dict]:
    # Handled in _applies (scope match); when invoked it's a no-op.
    # Kept for completeness — if a future variant inspects content, plug here.
    return []


_EVALUATORS: dict[str, Any] = {
    "blocked_phrase": _eval_blocked_phrase,
    "regex": _eval_regex,
    "pii": _eval_pii,
    "topic": _eval_topic,
    "max_tokens": _eval_max_tokens,
    "model_allowlist": _eval_model_allowlist,
}


# ── Context helper used by LLM wrappers ───────────────────────────────────────


def build_ctx(
    *,
    user_id: str | None,
    feature: str | None,
    model: str | None,
) -> dict:
    """Construct an enforcer ``ctx`` dict.

    Resolves ``org_admin_id`` and ``request_id`` from Flask ``g`` when available.
    Safe to call inside Celery tasks (no Flask context) — returns a best-effort
    dict; the enforcer fails open when ``org_admin_id`` is missing.
    """
    org_admin_id: str | None = None
    request_id: str | None = None
    try:
        from flask import g  # imported lazily so non-Flask callers don't pay the cost

        org_admin_id = getattr(g, "org_admin_id", None)
        if not org_admin_id:
            org_admin_id = getattr(g, "audit_owner_id", None)
        request_id = getattr(g, "request_id", None)
        if not user_id:
            user_id = getattr(g, "user_id", None)
    except Exception:
        # No Flask context (Celery / scripts) — fall through to best-effort ctx.
        logger.debug("enforcer: Flask context unavailable", exc_info=True)

    if not org_admin_id and user_id:
        # Self-access: the user is their own workspace owner.
        org_admin_id = str(user_id)

    return {
        "user_id": str(user_id) if user_id else None,
        "org_admin_id": str(org_admin_id) if org_admin_id else None,
        "feature": feature,
        "model": model,
        "request_id": request_id,
    }
