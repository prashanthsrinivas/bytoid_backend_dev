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
#                  detection a NER engine (Presidio + spaCy) is used.
#
# An entity is detected via:
#   - ``pattern``  — compiled regex, evaluated in-process by the enforcer
#   - ``ner_entity`` — Presidio entity name (e.g. "PERSON"), evaluated via
#                     the Presidio analyzer singleton.  Requires Presidio +
#                     spaCy + a language model to be installed; otherwise
#                     the entity is exposed in the catalog as unavailable
#                     and the enforcer silently skips it.
# An entity may declare both — regex is preferred when both are present.

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
    # ── NER-backed (Presidio + spaCy) ─────────────────────────────────────────
    {
        "key": "name",
        "label": "Person name",
        "category": "pii",
        "ner_entity": "PERSON",
    },
    {
        "key": "location",
        "label": "Location / address",
        "category": "pii",
        "ner_entity": "LOCATION",
    },
    {
        "key": "organization",
        "label": "Organization",
        "category": "pii",
        "ner_entity": "ORGANIZATION",
    },
    {
        "key": "nrp",
        "label": "Nationality / religion / politics (NER)",
        "category": "spi",
        "ner_entity": "NRP",
    },
    {
        "key": "medical_license",
        "label": "Medical license number",
        "category": "pii",
        "ner_entity": "MEDICAL_LICENSE",
    },
    {
        "key": "crypto_wallet",
        "label": "Crypto wallet",
        "category": "pii",
        "ner_entity": "CRYPTO",
    },
    {
        "key": "url",
        "label": "URL",
        "category": "pii",
        "ner_entity": "URL",
    },
]

PII_PATTERNS: dict[str, re.Pattern] = {
    e["key"]: e["pattern"] for e in _ENTITIES if e.get("pattern") is not None
}

# Key → Presidio entity name, for entities that require the NER engine.
# Used by the enforcer when an entity has no in-process regex.
NER_ENTITY_MAP: dict[str, str] = {
    e["key"]: e["ner_entity"] for e in _ENTITIES if e.get("ner_entity")
}

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
    """Return the full metadata catalog used by the guardrail authoring UI.

    Each entity carries:
      ``requires_ner`` — true when detection needs the Presidio analyzer
      ``available``    — false only for NER entities when Presidio + spaCy
                         are not importable in this environment
    """
    # Import is local so the metadata module remains free of heavy deps.
    from ai_governance.clients.presidio_client import (
        is_available as ner_available,
        unavailable_reason as ner_unavailable_reason,
    )

    ner_on = ner_available()
    ner_reason = ner_unavailable_reason() if not ner_on else None

    def _entity_payload(e: dict) -> dict:
        requires_ner = e.get("pattern") is None and bool(e.get("ner_entity"))
        return {
            "key": e["key"],
            "label": e["label"],
            "category": e["category"],
            "requires_ner": requires_ner,
            "available": (not requires_ner) or ner_on,
        }

    return {
        "rule_types": _RULE_TYPES,
        "directions": _DIRECTIONS,
        "actions": _ACTIONS,
        "entities": [_entity_payload(e) for e in _ENTITIES],
        "ner": {
            "available": ner_on,
            "unavailable_reason": ner_reason,
        },
    }
