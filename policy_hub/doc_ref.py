"""Reference-number minter for policy/procedure/standard documents.

Each document gets an immutable, human-readable `doc_ref` like ``ACC-0001`` so
auditors can cite it without juggling UUIDs. The triad shares a prefix:

    ACC-0001       — Access Control Policy
    ACC-P0001      — Access Control Procedure
    ACC-S0001      — Access Control Standard

Width is fixed at 4 digits from day one so lexicographic sorting stays correct
forever and the system never has to retroactively re-pad existing refs.

Layout
------
- ``_PREFIX_OVERRIDES``: case-insensitive substring → 3-letter prefix. Lives
  here so it is reviewable in PR.
- ``derive_prefix(title)``: pure function, no DB. Order: overrides → stopword
  strip → cleaned first-3 alpha.
- ``mint_doc_ref(org_id, doc_type, title)``: derives the prefix, salts on
  collision (different seed claims the same prefix in this org/doc_type),
  atomically increments the per-(org_id, prefix, doc_type) counter in
  ``policy_hub_doc_ref_seq``, and returns the formatted ref.
"""

import re

import pymysql.cursors

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)


_SEQ_WIDTH = 4  # zero-padded width of the trailing sequence number
_MAX_SALT = 99  # how many ACC, ACC2, …, ACC99 attempts before giving up


_TYPE_SUFFIX: dict[str, str] = {
    "policy": "",
    "procedure": "P",
    "standard": "S",
}


# Case-insensitive substring → 3-letter prefix. Order matters when two entries
# could both match a title: the first match in dict iteration wins, so put more
# specific phrases ahead of more general ones.
_PREFIX_OVERRIDES: dict[str, str] = {
    "access control": "ACC",
    "acceptable use": "AUP",
    "asset management": "ASM",
    "business continuity": "BCM",
    "change management": "CHG",
    "data classification": "DCL",
    "data protection": "DPR",
    "data privacy": "DPR",
    "encryption": "ENC",
    "cryptography": "ENC",
    "incident management": "IRM",
    "incident response": "IRM",
    "information security": "ISP",
    "physical security": "PHS",
    "risk management": "RSK",
    "third party": "TPM",
    "vendor": "TPM",
    "vulnerability management": "VLN",
}


_STOPWORDS: frozenset[str] = frozenset({
    "policy", "policies",
    "procedure", "procedures",
    "standard", "standards",
    "the", "a", "an",
    "and", "or", "of", "for", "to", "in", "on", "by",
})


def _normalize_title(title: str) -> str:
    """Collapse whitespace and lowercase for matching."""
    return re.sub(r"\s+", " ", (title or "")).strip().lower()


def _match_override(title_norm: str) -> tuple[str, str] | None:
    """Return ``(matched_substring, prefix)`` for the first override hit."""
    for needle, prefix in _PREFIX_OVERRIDES.items():
        if needle in title_norm:
            return needle, prefix
    return None


def _significant_words(title_norm: str) -> list[str]:
    """Split on non-alnum; drop stopwords and empty tokens."""
    tokens = re.findall(r"[a-z0-9]+", title_norm)
    return [t for t in tokens if t and t not in _STOPWORDS]


def derive_prefix(title: str) -> tuple[str, str]:
    """Return ``(prefix, seed)`` derived from ``title``.

    The ``seed`` identifies what claimed the prefix — either the override
    substring (e.g. ``"access control"``) or the first significant word
    (e.g. ``"accounting"``). The minter uses it to detect collisions where
    two different titles would map to the same prefix.

    Always returns a non-empty 3-character (or longer if no alpha at all
    available) prefix; never raises.
    """
    title_norm = _normalize_title(title)
    if not title_norm:
        return "DOC", ""

    override = _match_override(title_norm)
    if override:
        seed, prefix = override
        return prefix, seed

    words = _significant_words(title_norm)
    # Only words containing a letter can seed an (alphabetic) prefix; a purely
    # numeric token like "123" must not become the prefix.
    alpha_words = [w for w in words if re.search(r"[a-z]", w)]
    if alpha_words:
        first = alpha_words[0]
        # take up to 3 chars from the first significant alpha word
        prefix = first[:3].upper()
        if len(prefix) < 3:
            # pad with chars from subsequent alpha words so we always have 3
            for w in alpha_words[1:]:
                prefix = (prefix + w[: 3 - len(prefix)]).upper()
                if len(prefix) >= 3:
                    break
        return prefix.ljust(3, "X"), first

    # No alpha tokens at all — synthesize from the raw title
    cleaned = re.sub(r"[^a-z]", "", title_norm)
    if cleaned:
        return cleaned[:3].upper().ljust(3, "X"), cleaned[:8]
    return "DOC", ""


def _salt_prefix(prefix: str, attempt: int) -> str:
    """Apply the Nth salt: ``ACC`` → ``ACC`` (attempt 0) → ``ACC2`` (1) → ``ACC3`` …"""
    if attempt <= 0:
        return prefix
    return f"{prefix}{attempt + 1}"


def _ensure_seq_table(cur) -> None:
    """Idempotent CREATE — safe to call on every mint."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_hub_doc_ref_seq (
            org_id   VARCHAR(255) NOT NULL,
            prefix   VARCHAR(8)   NOT NULL,
            doc_type VARCHAR(16)  NOT NULL,
            seed     VARCHAR(64)  NOT NULL,
            next_seq INT          NOT NULL DEFAULT 1,
            PRIMARY KEY (org_id, prefix, doc_type)
        )
        """
    )


def _claim_sequence(
    cur,
    org_id: str,
    base_prefix: str,
    seed: str,
    doc_type: str,
) -> tuple[str, int]:
    """Reserve the next sequence number under ``(org_id, prefix, doc_type)``.

    Returns ``(final_prefix, seq)``. ``final_prefix`` may be salted (``ACC2``)
    if the natural prefix is already claimed by a different seed in this org
    and doc_type. Raises if every salt up to ``_MAX_SALT`` collides — that
    only happens if the org has truly exhausted the namespace.

    Caller owns the transaction; this function uses SELECT ... FOR UPDATE so
    concurrent mints serialize correctly.
    """
    for attempt in range(_MAX_SALT):
        prefix = _salt_prefix(base_prefix, attempt)
        cur.execute(
            "SELECT seed, next_seq FROM policy_hub_doc_ref_seq "
            "WHERE org_id=%s AND prefix=%s AND doc_type=%s FOR UPDATE",
            (org_id, prefix, doc_type),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO policy_hub_doc_ref_seq "
                "(org_id, prefix, doc_type, seed, next_seq) "
                "VALUES (%s, %s, %s, %s, 2)",
                (org_id, prefix, doc_type, seed or ""),
            )
            return prefix, 1
        existing_seed = (row.get("seed") if isinstance(row, dict) else row[0]) or ""
        existing_next = row.get("next_seq") if isinstance(row, dict) else row[1]
        if existing_seed == (seed or ""):
            cur.execute(
                "UPDATE policy_hub_doc_ref_seq SET next_seq = next_seq + 1 "
                "WHERE org_id=%s AND prefix=%s AND doc_type=%s",
                (org_id, prefix, doc_type),
            )
            return prefix, int(existing_next)
        # different seed already holds this prefix — salt and retry

    raise RuntimeError(
        f"doc_ref minter exhausted salts for prefix={base_prefix!r} "
        f"org={org_id!r} doc_type={doc_type!r}"
    )


def _format_ref(prefix: str, doc_type: str, seq: int) -> str:
    suffix = _TYPE_SUFFIX.get(doc_type, "")
    return f"{prefix}-{suffix}{seq:0{_SEQ_WIDTH}d}"


def mint_doc_ref(org_id: str, doc_type: str, title: str) -> str:
    """Mint a fresh, immutable reference number.

    ``doc_type`` must be one of ``policy``, ``procedure``, ``standard``.
    Returns strings like ``ACC-0001``, ``ACC-P0001``, ``ACC-S0001``. The
    triad shares a prefix when their titles share an override substring
    (or the same first significant word).
    """
    if not org_id:
        raise ValueError("org_id is required")
    if doc_type not in _TYPE_SUFFIX:
        raise ValueError(f"unsupported doc_type: {doc_type!r}")

    base_prefix, seed = derive_prefix(title)

    conn = connect_to_rds()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            _ensure_seq_table(cur)
            final_prefix, seq = _claim_sequence(cur, org_id, base_prefix, seed, doc_type)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            logger.debug("doc_ref mint rollback failed: %s", rb_exc)
        raise
    finally:
        conn.close()

    return _format_ref(final_prefix, doc_type, seq)
