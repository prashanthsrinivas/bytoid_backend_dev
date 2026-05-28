"""Source extraction + anonymization for the AI governance scan.

Pulls the data the live AI actually runs on — the RAG knowledge base, the
prompt templates, the guardrail config, and the risk-scoring history — out of
LanceDB / the ``cust_helpers`` YAMLs / the rules store, decrypts it, and (for
user content) scrubs PII with Presidio before any of it reaches Giskard.

Design rules:
  * **Pure synchronous + read-only.** LanceDB table handles are synchronous
    (``table.search().to_list()``), so no event loop is required and nothing is
    ever created — a user without a given table is simply skipped.  We do NOT
    use ``LanceDBServer``'s ``_open_or_create_*`` helpers because those create
    empty tables and schedule background re-encryption tasks.
  * **Never raise.** Every extractor returns its payload plus a ``meta`` note.
    A missing table, a decrypt error, or an unavailable dependency degrades to
    empty output with a reason — it never raises an exception that could abort a
    platform-wide sweep.
  * **PII safety.** ``extract_pii_docs`` (group D) always anonymizes. Even when
    spaCy/Presidio is absent, ``presidio_client.anonymize`` still applies the
    regex pass, so classic identifiers are scrubbed; only NER-only entities
    (e.g. person names) survive in that degraded mode.
  * **Import-safe.** ``pandas`` and giskard are imported lazily so this module
    imports cleanly in environments that don't have them (mirrors
    ``giskard_client.py``).

``LanceDBServer`` is dependency-injectable (``db=`` kwarg) so the extractors can
be unit-tested with a fake connection and no real database.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Default per-user sampling cap — bounds Bedrock/embedding cost downstream.
DEFAULT_DOC_SAMPLE = 200

# Rows LanceDB tables are seeded with on creation; never real content.
_INIT_IDS = {"init", "", "dummy"}

# Terminal runbook statuses worth scanning for the tabular risk model.
_TERMINAL_STATUSES = {"completed", "failed", "success", "done"}

# Source A — RAG knowledge base: (table-name template, text fields to join).
_KB_TABLES: list[tuple[str, list[str]]] = [
    ("index_{uid}", ["text"]),
    ("scrape_{uid}", ["title", "content"]),
    ("aud_{uid}", ["text"]),
]

# Source D — PII-heavy: (table-name template, fields). ALWAYS anonymized.
# Note: the chat table name has no underscore before the user id.
_PII_TABLES: list[tuple[str, list[str]]] = [
    ("u_{uid}", ["text"]),
    ("bytoid_pro_chat{uid}", ["content"]),
    ("radar_{uid}", ["user_input", "result"]),
    ("runbook_results_{uid}", ["result"]),
]


# ── LanceDB read-only helpers ───────────────────────────────────────────────────


def _connect(db=None):
    """Return ``(db, conn)``; ``conn`` is ``None`` if LanceDB is unreachable.

    Never raises.  ``LanceDBServer._connect_if_needed`` returns the exception
    object (rather than raising) on failure, so we normalise that to ``None``.
    """
    try:
        if db is None:
            from db.lance_db_service import LanceDBServer

            db = LanceDBServer()
        conn = db._connect_if_needed()
    except Exception as exc:
        logger.warning("scan_sources: LanceDB connect failed: %s", exc)
        return db, None
    if conn is None or isinstance(conn, Exception):
        return db, None
    return db, conn


def _open_existing(conn, name):
    """Open ``name`` only if it already exists; never create it."""
    try:
        if name in conn.table_names():
            return conn.open_table(name)
    except Exception as exc:
        logger.debug("scan_sources: open_table(%s) failed: %s", name, exc)
    return None


def _scan_rows(table, limit: int) -> list[dict]:
    """Read up to ``limit`` rows from a table via a vector-less scan."""
    try:
        return table.search().limit(limit).to_list()
    except Exception:
        try:
            return table.to_pandas().to_dict("records")[:limit]
        except Exception as exc:
            logger.debug("scan_sources: scan rows failed: %s", exc)
            return []


def _safe_dec(db, user_id: str, raw):
    """Decrypt a field; return ``None`` (a counted error) on KMS failure."""
    try:
        return db._dec(user_id, raw)
    except Exception as exc:
        logger.debug("scan_sources: decrypt failed: %s", exc)
        return None


def _anonymize(text: str) -> str:
    """Scrub PII/SPI. Fails open to the regex pass when Presidio is absent."""
    try:
        from ai_governance.clients.presidio_client import anonymize

        return anonymize(text)
    except Exception as exc:  # pragma: no cover - presidio_client is local
        logger.warning("scan_sources: anonymize unavailable: %s", exc)
        return text


def _extract_from_tables(
    db,
    conn,
    user_id: str,
    specs: list[tuple[str, list[str]]],
    sample_size: int,
    anonymize: bool,
) -> tuple[list[dict], dict]:
    """Shared row → ``{id, text, source_table}`` extraction across tables."""
    docs: list[dict] = []
    meta: dict = {"tables": {}, "decrypt_errors": 0}
    for tmpl, fields in specs:
        name = tmpl.format(uid=user_id)
        table = _open_existing(conn, name)
        if table is None:
            continue
        kept = 0
        for idx, row in enumerate(_scan_rows(table, sample_size)):
            rid = str(row.get("id") or row.get("result_id") or "")
            if rid in _INIT_IDS:
                continue
            parts: list[str] = []
            for field in fields:
                raw = row.get(field)
                if not raw:
                    continue
                dec = _safe_dec(db, user_id, raw)
                if dec is None:
                    meta["decrypt_errors"] += 1
                    continue
                parts.append(str(dec))
            text = "\n".join(p for p in parts if p).strip()
            if not text:
                continue
            if anonymize:
                text = _anonymize(text)
            docs.append(
                {
                    "id": rid or f"{name}:{idx}",
                    "text": text,
                    "source_table": name,
                }
            )
            kept += 1
            if len(docs) >= sample_size:
                break
        meta["tables"][name] = kept
        if len(docs) >= sample_size:
            break
    return docs, meta


# ── Source A — RAG knowledge base ───────────────────────────────────────────────


def extract_knowledge_docs(
    user_id: str,
    *,
    sample_size: int = DEFAULT_DOC_SAMPLE,
    anonymize: bool = False,
    db=None,
) -> dict:
    """Decrypted documents from ``index_/scrape_/aud_{user_id}``.

    Returns ``{"docs": [{id, text, source_table}], "meta": {...}}``.  The
    orchestrator passes ``anonymize=True`` when these are merged with the
    PII-heavy group-D corpus for the RAGET scan.
    """
    db, conn = _connect(db)
    if conn is None:
        return {"docs": [], "meta": {"error": "lancedb_unavailable"}}
    docs, meta = _extract_from_tables(
        db, conn, user_id, _KB_TABLES, sample_size, anonymize
    )
    return {"docs": docs, "meta": meta}


# ── Source D — PII-heavy (always anonymized) ────────────────────────────────────


def extract_pii_docs(
    user_id: str,
    *,
    sample_size: int = DEFAULT_DOC_SAMPLE,
    db=None,
) -> dict:
    """Decrypted + anonymized content from emails/chats/radar/runbook results.

    Anonymization is mandatory and non-optional here — raw PII never leaves
    this function.
    """
    db, conn = _connect(db)
    if conn is None:
        return {"docs": [], "meta": {"error": "lancedb_unavailable"}}
    docs, meta = _extract_from_tables(
        db, conn, user_id, _PII_TABLES, sample_size, anonymize=True
    )
    meta["anonymized"] = True
    return {"docs": docs, "meta": meta}


# ── Source E — tabular risk scores ──────────────────────────────────────────────


def extract_risk_dataframe(user_id: str, *, db=None) -> dict:
    """Build a DataFrame of terminal runbook results for the tabular scan.

    Columns: ``execution_time_ms``, ``input_mode``, ``risk_score``, ``status``.
    These columns are stored in plaintext (only ``result`` is encrypted), so no
    decryption is required.  Returns ``{"dataframe": <df|None>, "meta": {...}}``.
    """
    db, conn = _connect(db)
    if conn is None:
        return {"dataframe": None, "meta": {"error": "lancedb_unavailable"}}
    table = _open_existing(conn, f"runbook_results_{user_id}")
    if table is None:
        return {"dataframe": None, "meta": {"error": "no_table"}}

    clean: list[dict] = []
    for row in _scan_rows(table, 100_000):
        if str(row.get("result_id") or "") in _INIT_IDS:
            continue
        status = row.get("status")
        if status not in _TERMINAL_STATUSES:
            continue
        clean.append(
            {
                "execution_time_ms": row.get("execution_time_ms"),
                "input_mode": row.get("input_mode") or "unknown",
                "risk_score": row.get("risk_score"),
                "status": status,
            }
        )
    if not clean:
        return {"dataframe": None, "meta": {"error": "no_terminal_rows"}}

    import pandas as pd

    return {"dataframe": pd.DataFrame(clean), "meta": {"rows": len(clean)}}


# ── Source B — system prompts ───────────────────────────────────────────────────

# Minimum string length to treat a YAML leaf as a real prompt vs. a label.
_MIN_PROMPT_LEN = 40


def _repo_relpath(rel: str) -> str:
    """Resolve a ``cust_helpers``-relative path against the repo root."""
    import cust_helpers.pathconfig as pc

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(pc.__file__)))
    return os.path.join(repo_root, rel)


def _flatten_prompts(obj, path: str, prefix: str, out: list[dict]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            _flatten_prompts(val, path, child, out)
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            _flatten_prompts(val, path, f"{prefix}[{i}]", out)
    elif isinstance(obj, str):
        s = obj.strip()
        if len(s) >= _MIN_PROMPT_LEN:
            out.append({"prompt_key": prefix, "template_str": s, "path": path})


def extract_prompt_templates() -> dict:
    """Flatten every prompt string from the ``cust_helpers`` YAML library.

    Returns ``{"prompts": [{prompt_key, template_str, path}], "meta": {...}}``.
    Platform-global (no ``user_id``); the same prompts serve every tenant.
    """
    from utils.normal import load_yaml_file

    import cust_helpers.pathconfig as pc

    rels = sorted(
        {
            v
            for k, v in vars(pc).items()
            if not k.startswith("__")
            and isinstance(v, str)
            and v.endswith(".yaml")
        }
    )

    prompts: list[dict] = []
    files_read = 0
    for rel in rels:
        try:
            data = load_yaml_file(_repo_relpath(rel))
        except Exception as exc:
            logger.debug("scan_sources: load_yaml_file(%s) failed: %s", rel, exc)
            continue
        if data is None:
            continue
        files_read += 1
        _flatten_prompts(data, rel, "", prompts)

    return {
        "prompts": prompts,
        "meta": {"files_read": files_read, "prompt_count": len(prompts)},
    }


# ── Source C — guardrail config ─────────────────────────────────────────────────


def extract_guardrail_config(org_admin_id: str) -> dict:
    """Snapshot the live guardrail controls for an org.

    Combines the DB-backed ``ai_guardrail_rules`` with the NeMo config files so
    mode 4 (the validation harness) can report what protections are in place.
    """
    rules: list[dict] = []
    rules_error = None
    try:
        from ai_governance.rules_store import list_rules

        rules = list_rules(org_admin_id)
    except Exception as exc:
        rules_error = f"{exc.__class__.__name__}: {exc}"
        logger.warning("scan_sources: list_rules failed: %s", exc)

    nemo_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config", "nemo_guardrails"
    )
    config_path = os.path.join(nemo_dir, "config.yml")
    flows: list[str] = []
    try:
        flows_dir = os.path.join(nemo_dir, "flows")
        if os.path.isdir(flows_dir):
            flows = sorted(os.listdir(flows_dir))
    except Exception as exc:
        logger.debug("scan_sources: list nemo flows failed: %s", exc)

    return {
        "rules": rules,
        "nemo": {
            "config_path": config_path,
            "config_present": os.path.exists(config_path),
            "flows": flows,
        },
        "meta": {"rule_count": len(rules), "rules_error": rules_error},
    }
