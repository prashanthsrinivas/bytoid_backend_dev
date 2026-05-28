"""Catalog of guardrail authoring options exposed to the frontend.

Single source of truth for rule types, directions, actions, and the PII / SPI
entity dictionary.  The UI fetches this via ``GET /ai-governance/metadata`` so
that adding a new entity or rule type is a pure backend change — no frontend
release is needed to surface it.

The PII patterns here are also consumed by the enforcer's ``_eval_pii``;
keep additions regex-safe (no catastrophic backtracking) and ASCII-anchored.
"""

from __future__ import annotations

import re


# ── PII / SPI entity dictionary ──────────────────────────────────────────────
#
# category "pii" — classic personal identifiers (regex-detectable with good
#                  precision)
# category "spi" — sensitive personal attributes (demographics, beliefs).
#                  Regex matches are best-effort; for production-grade name
#                  detection a NER engine (e.g. Presidio) is required.

_ENTITIES: list[dict] = [
    # ── PII ───────────────────────────────────────────────────────────────────
    {
        "key": "email",
        "label": "Email",
        "category": "pii",
        "pattern": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    },
    {
        "key": "phone",
        "label": "Phone number",
        "category": "pii",
        "pattern": re.compile(
            r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
        ),
    },
    {
        "key": "ssn",
        "label": "US SSN",
        "category": "pii",
        "pattern": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    },
    {
        "key": "credit_card",
        "label": "Credit card",
        "category": "pii",
        "pattern": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    },
    {
        "key": "ip",
        "label": "IP address (v4)",
        "category": "pii",
        "pattern": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    },
    {
        "key": "ipv6",
        "label": "IP address (v6)",
        "category": "pii",
        "pattern": re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
    },
    {
        "key": "mac_address",
        "label": "MAC address",
        "category": "pii",
        "pattern": re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b"),
    },
    {
        "key": "iban",
        "label": "IBAN",
        "category": "pii",
        "pattern": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    },
    {
        "key": "postal_code_us",
        "label": "US ZIP code",
        "category": "pii",
        "pattern": re.compile(r"\b\d{5}(?:-\d{4})?\b"),
    },
    {
        "key": "postal_code_ca",
        "label": "Canadian postal code",
        "category": "pii",
        "pattern": re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b"),
    },
    {
        "key": "date_of_birth",
        "label": "Date of birth",
        "category": "pii",
        "pattern": re.compile(
            r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b"
        ),
    },
    # ── SPI ───────────────────────────────────────────────────────────────────
    {
        "key": "age",
        "label": "Age",
        "category": "spi",
        "pattern": re.compile(
            r"\b(?:aged?\s*(?:is\s*)?\d{1,3}"
            r"|\d{1,3}\s*-?\s*years?\s*-?\s*old"
            r"|I\s+am\s+\d{1,3}"
            r"|I'?m\s+\d{1,3})\b",
            re.IGNORECASE,
        ),
    },
    {
        "key": "gender",
        "label": "Gender",
        "category": "spi",
        "pattern": re.compile(
            r"\b(?:male|female|non[- ]?binary|trans(?:gender)?"
            r"|cis(?:gender)?|genderqueer|gender[- ]?fluid|agender)\b",
            re.IGNORECASE,
        ),
    },
    {
        "key": "sexual_orientation",
        "label": "Sexual orientation",
        "category": "spi",
        "pattern": re.compile(
            r"\b(?:gay|lesbian|bisexual|pansexual|asexual"
            r"|heterosexual|homosexual|queer|LGBTQ\+?)\b",
            re.IGNORECASE,
        ),
    },
    {
        "key": "religion",
        "label": "Religion",
        "category": "spi",
        "pattern": re.compile(
            r"\b(?:christian|catholic|protestant|muslim|islam(?:ic)?"
            r"|jewish|judaism|hindu(?:ism)?|buddhist|buddhism"
            r"|sikh(?:ism)?|atheist|agnostic)\b",
            re.IGNORECASE,
        ),
    },
    {
        "key": "ethnicity",
        "label": "Ethnicity / race",
        "category": "spi",
        "pattern": re.compile(
            r"\b(?:asian|black|african[- ]?american|caucasian"
            r"|hispanic|latino|latina|latinx|native[- ]?american"
            r"|pacific[- ]?islander|middle[- ]?eastern|indigenous)\b",
            re.IGNORECASE,
        ),
    },
    {
        "key": "nationality",
        "label": "Nationality",
        "category": "spi",
        "pattern": re.compile(
            r"\b(?:american|canadian|british|english|french|german"
            r"|italian|spanish|portuguese|russian|chinese|japanese"
            r"|korean|indian|pakistani|mexican|brazilian|nigerian"
            r"|egyptian|israeli|australian)\b",
            re.IGNORECASE,
        ),
    },
]

PII_PATTERNS: dict[str, re.Pattern] = {e["key"]: e["pattern"] for e in _ENTITIES}
ENTITY_KEYS: list[str] = [e["key"] for e in _ENTITIES]


# ── Rule types ────────────────────────────────────────────────────────────────

_RULE_TYPES: list[dict] = [
    {
        "id": "blocked_phrase",
        "label": "Blocked phrase",
        "description": "Match a list of exact phrases.",
        "config_schema": {"phrases": "list[str]"},
    },
    {
        "id": "regex",
        "label": "Regex",
        "description": "Match a custom regular expression.",
        "config_schema": {"pattern": "str", "replacement": "str?"},
    },
    {
        "id": "pii",
        "label": "PII / SPI",
        "description": (
            "Detect personal identifiers (email, phone, SSN, …) and sensitive "
            "personal attributes (age, gender, religion, …)."
        ),
        "config_schema": {"entities": "list[str]"},
    },
    {
        "id": "topic",
        "label": "Topic",
        "description": "Match a topic by a list of keywords with a hit threshold.",
        "config_schema": {"keywords": "list[str]", "threshold": "int"},
    },
    {
        "id": "max_tokens",
        "label": "Max tokens",
        "description": "Reject prompts longer than a word budget.",
        "config_schema": {"max_words": "int"},
    },
    {
        "id": "model_allowlist",
        "label": "Model allowlist",
        "description": "Restrict the rule to a list of model IDs.",
        "config_schema": {"allowed": "list[str]"},
    },
]

VALID_TYPES: set[str] = {rt["id"] for rt in _RULE_TYPES}


# ── Directions ────────────────────────────────────────────────────────────────

_DIRECTIONS: list[dict] = [
    {"id": "input", "label": "Input"},
    {"id": "output", "label": "Output"},
    {"id": "both", "label": "Both"},
]
VALID_APPLIES: set[str] = {d["id"] for d in _DIRECTIONS}


# ── Actions ───────────────────────────────────────────────────────────────────

_ACTIONS: list[dict] = [
    {"id": "block", "label": "Block", "description": "Reject the call."},
    {"id": "redact", "label": "Redact", "description": "Mask the match in the text."},
    {"id": "warn", "label": "Warn", "description": "Log a warning, allow the call."},
    {
        "id": "audit",
        "label": "Audit-only",
        "description": "Record the match without altering the call.",
    },
]
VALID_ACTIONS: set[str] = {a["id"] for a in _ACTIONS}


# ── Public catalog ────────────────────────────────────────────────────────────


def get_catalog() -> dict:
    """Return the full metadata catalog used by the guardrail authoring UI."""
    return {
        "rule_types": _RULE_TYPES,
        "directions": _DIRECTIONS,
        "actions": _ACTIONS,
        "entities": [
            {"key": e["key"], "label": e["label"], "category": e["category"]}
            for e in _ENTITIES
        ],
    }
