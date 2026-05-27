"""Abbreviation minter for trackers — e.g. ``RSK-0001`` for "Risk Register".

The abbreviation is shown as a compact tag next to policy/procedure/standard
statements that a tracker row references. Reuses ``policy_hub.doc_ref`` for
prefix derivation so trackers and documents share one naming convention, and a
4-digit zero-padded sequence for the same lexicographic-sort-safety reason
documents use.
"""

import pymysql.cursors

from db.rds_db import connect_to_rds
from policy_hub.doc_ref import derive_prefix
from utils.base_logger import get_logger

logger = get_logger(__name__)

_SEQ_WIDTH = 4
_MAX_SALT = 99


def _ensure_seq_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tab_tracker_abbrev_seq (
            org_id   VARCHAR(255) NOT NULL,
            prefix   VARCHAR(8)   NOT NULL,
            seed     VARCHAR(64)  NOT NULL,
            next_seq INT          NOT NULL DEFAULT 1,
            PRIMARY KEY (org_id, prefix)
        )
        """
    )


def _salt_prefix(prefix: str, attempt: int) -> str:
    if attempt <= 0:
        return prefix
    return f"{prefix}{attempt + 1}"


def _claim_sequence(cur, org_id: str, base_prefix: str, seed: str) -> tuple[str, int]:
    for attempt in range(_MAX_SALT):
        prefix = _salt_prefix(base_prefix, attempt)
        cur.execute(
            "SELECT seed, next_seq FROM tab_tracker_abbrev_seq "
            "WHERE org_id=%s AND prefix=%s FOR UPDATE",
            (org_id, prefix),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO tab_tracker_abbrev_seq (org_id, prefix, seed, next_seq) "
                "VALUES (%s, %s, %s, 2)",
                (org_id, prefix, seed or ""),
            )
            return prefix, 1
        existing_seed = (row.get("seed") if isinstance(row, dict) else row[0]) or ""
        existing_next = row.get("next_seq") if isinstance(row, dict) else row[1]
        if existing_seed == (seed or ""):
            cur.execute(
                "UPDATE tab_tracker_abbrev_seq SET next_seq = next_seq + 1 "
                "WHERE org_id=%s AND prefix=%s",
                (org_id, prefix),
            )
            return prefix, int(existing_next)
        # collision: a different seed holds this prefix — salt and retry

    raise RuntimeError(
        f"tracker abbrev minter exhausted salts for prefix={base_prefix!r} org={org_id!r}"
    )


def mint_tracker_abbrev(org_id: str, tracker_name: str) -> str:
    """Mint an immutable abbreviation like ``RSK-0001`` for *tracker_name*."""
    if not org_id:
        raise ValueError("org_id is required")

    base_prefix, seed = derive_prefix(tracker_name)

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_seq_table(cur)
            prefix, seq = _claim_sequence(cur, org_id, base_prefix, seed)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("tracker abbrev rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()

    return f"{prefix}-{seq:0{_SEQ_WIDTH}d}"


def safe_mint_tracker_abbrev(user_id: str, tracker_name: str) -> str | None:
    """Resolve the user's org and mint, swallowing failures.

    Returns ``None`` when the org can't be resolved or minting fails so tracker
    creation never breaks; such trackers are picked up by a backfill later.
    """
    try:
        from workflow_route.state_machine import get_user_org_id
        org_id = get_user_org_id(user_id)
        if not org_id:
            logger.info("tracker abbrev: no org for user=%s — skipping mint", user_id)
            return None
        return mint_tracker_abbrev(org_id, tracker_name)
    except Exception as exc:
        logger.warning("tracker abbrev: mint failed for user=%s: %s", user_id, exc)
        return None
