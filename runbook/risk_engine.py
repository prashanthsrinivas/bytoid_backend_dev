"""Configurable, deterministic risk scoring.

Risk Score = Impact x Likelihood, where Impact and Likelihood are each scored on
a configurable scale (default 1-5, so the per-risk score maxes at 25). The LLM only
assigns the per-risk impact/likelihood; the math here is deterministic so it stays
auditable and user-modifiable.

The config is stored per-org as a JSON blob on the owner/admin ``users`` row
(``users.risk_config``). The runbook engine already partitions all data by the owner
``user_id``, so reading the config by that same id yields org-level behavior.
"""

import copy
import json

import pymysql

from db.rds_db import connect_to_rds
from utils.app_configs import IS_DEV
from utils.base_logger import get_logger

logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")

# Default config. Impact and Likelihood out of 5 => Risk Score out of 25.
DEFAULT_RISK_CONFIG = {
    "impact_scale": 5,
    "likelihood_scale": 5,
    "formula": "impact * likelihood",  # display string; computed deterministically below
    "aggregation": "average",  # "average" | "max"
    "bands": [
        {"label": "Low", "min": 1, "max": 6, "color": "blue"},
        {"label": "Moderate", "min": 7, "max": 12, "color": "yellow"},
        {"label": "High", "min": 13, "max": 19, "color": "orange"},
        {"label": "Critical", "min": 20, "max": 25, "color": "red"},
    ],
}


def get_risk_config(user_id):
    """Return the (owner) org's risk config, merged over defaults.

    Falls back to ``DEFAULT_RISK_CONFIG`` when the user has no saved config or the
    column/row is missing. Never raises — a config read must not break generation.
    """
    cfg = copy.deepcopy(DEFAULT_RISK_CONFIG)
    if not user_id:
        return cfg
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT risk_config FROM users WHERE user_id=%s", (user_id,)
            )
            row = cursor.fetchone()
        conn.close()
        raw = (row or {}).get("risk_config")
        if not raw:
            return cfg
        saved = raw if isinstance(raw, dict) else json.loads(raw)
        if isinstance(saved, dict):
            # Shallow merge top-level keys; replace `bands` wholesale when present.
            for key, value in saved.items():
                if value is not None:
                    cfg[key] = value
    except Exception:
        logger.warning("get_risk_config failed; using defaults", exc_info=IS_DEV)
    return cfg


# Set once the column is known to exist, so the check below runs at most once per
# process (and re-runs only if it ever fails to confirm/create the column).
_risk_config_column_ready = False


def ensure_risk_config_column():
    """Idempotently make sure ``users.risk_config`` exists before a write.

    The column is normally added by ``create_db.update_users_risk_config()``, but
    that migration is run manually and separately from a code deploy — so a freshly
    deployed/unmigrated DB has the code but not the column, and every save 500s with
    "Unknown column 'risk_config'". The read path tolerates this (falls back to
    defaults), which hides the gap until the first save. Self-heal the write path so
    saving works without a manual migration step. Guarded by INFORMATION_SCHEMA and
    cached, so it's a no-op after the first successful check.
    """
    global _risk_config_column_ready
    if _risk_config_column_ready:
        return
    conn = connect_to_rds()
    if conn is None:
        return
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'users'
                  AND COLUMN_NAME = 'risk_config'
                """
            )
            (col_exists,) = cursor.fetchone()
            if not col_exists:
                cursor.execute("ALTER TABLE users ADD COLUMN risk_config JSON")
                logger.info("Added missing 'risk_config' column to 'users' table.")
        conn.commit()
        _risk_config_column_ready = True
    except Exception:
        logger.warning("ensure_risk_config_column failed", exc_info=IS_DEV)
    finally:
        conn.close()


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def risk_analysis_disabled(runbook):
    """True when the runbook was created with risk analysis turned off.

    Checks the in-memory ``disable_risk_analysis`` flag (set at creation, carried on the
    runbook dict passed to the engine) and, as a persistent fallback for re-runs, the
    ``risk_analysis_enabled`` marker embedded in the stored ``structure_theme`` JSON.
    """
    if not isinstance(runbook, dict):
        return False
    if _truthy(runbook.get("disable_risk_analysis")):
        return True
    raw = runbook.get("structure_theme")
    if not raw:
        return False
    try:
        structure = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return False
    return isinstance(structure, dict) and structure.get("risk_analysis_enabled") is False


def _clamp(value, low, high):
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = low
    return max(low, min(high, n))


def _level_for_score(score, bands):
    """Return the band label whose [min, max] contains ``score``."""
    for band in bands:
        try:
            if band["min"] <= score <= band["max"]:
                return band.get("label", "")
        except (KeyError, TypeError):
            continue
    # Score below the lowest band -> first band; above the highest -> last band.
    if bands:
        return bands[0]["label"] if score < bands[0].get("min", 0) else bands[-1]["label"]
    return ""


def compute_risk(risks, config=None):
    """Deterministically score a list of risks per ``config``.

    Each input risk is expected to carry ``threat``, ``vulnerability``, ``impact`` and
    ``likelihood`` (the latter two assigned by the LLM on the configured scale). This
    sets ``risk_score`` per risk and computes the overall ``final_risk_score`` and
    ``risk_level``. Returns the enriched ``risk_analysis`` dict.
    """
    config = config or DEFAULT_RISK_CONFIG
    impact_scale = int(config.get("impact_scale", 5) or 5)
    likelihood_scale = int(config.get("likelihood_scale", 5) or 5)
    bands = config.get("bands") or DEFAULT_RISK_CONFIG["bands"]
    aggregation = (config.get("aggregation") or "average").lower()

    scored = []
    for risk in risks or []:
        if not isinstance(risk, dict):
            continue
        impact = round(_clamp(risk.get("impact"), 1, impact_scale))
        likelihood = round(_clamp(risk.get("likelihood"), 1, likelihood_scale))
        score = impact * likelihood
        entry = dict(risk)
        entry["impact"] = impact
        entry["likelihood"] = likelihood
        entry["risk_score"] = score
        entry["risk_level"] = _level_for_score(score, bands)
        scored.append(entry)

    scores = [r["risk_score"] for r in scored]
    if not scores:
        final_score = 0
    elif aggregation == "max":
        final_score = max(scores)
    else:
        final_score = round(sum(scores) / len(scores), 1)

    return {
        "risks": scored,
        "final_risk_score": final_score,
        "risk_level": _level_for_score(final_score, bands),
        "max_score": impact_scale * likelihood_scale,
        "config": {
            "impact_scale": impact_scale,
            "likelihood_scale": likelihood_scale,
            "aggregation": aggregation,
            "bands": bands,
        },
    }


def relabel_risk_analysis(blob, config):
    """Re-derive risk_level labels from stored numeric scores using current bands.

    Read-time only: mutates and returns ``blob`` in place, leaving every stored
    ``risk_score``/``final_risk_score`` untouched — it only remaps each score to a
    label via the current org config so band renames / range edits show up on
    existing reports without a migration. Tolerant of missing keys; never raises.
    """
    if not isinstance(blob, dict):
        return blob
    bands = (config or {}).get("bands") or DEFAULT_RISK_CONFIG["bands"]
    ra = blob.get("risk_analysis")
    if isinstance(ra, dict):
        if ra.get("final_risk_score") is not None:
            ra["risk_level"] = _level_for_score(ra["final_risk_score"], bands)
        for r in ra.get("risks") or []:
            if isinstance(r, dict) and r.get("risk_score") is not None:
                r["risk_level"] = _level_for_score(r["risk_score"], bands)
    return blob


def validate_risk_config(config):
    """Validate a user-submitted config. Returns (ok, error_message)."""
    if not isinstance(config, dict):
        return False, "config must be an object"
    try:
        impact_scale = int(config.get("impact_scale", 5))
        likelihood_scale = int(config.get("likelihood_scale", 5))
    except (TypeError, ValueError):
        return False, "impact_scale and likelihood_scale must be integers"
    if not (1 <= impact_scale <= 10) or not (1 <= likelihood_scale <= 10):
        return False, "scales must be between 1 and 10"

    aggregation = (config.get("aggregation") or "average").lower()
    if aggregation not in ("average", "max"):
        return False, "aggregation must be 'average' or 'max'"

    bands = config.get("bands")
    if not isinstance(bands, list) or not bands:
        return False, "bands must be a non-empty list"
    max_score = impact_scale * likelihood_scale
    prev_max = 0
    for band in bands:
        if not isinstance(band, dict) or "min" not in band or "max" not in band:
            return False, "each band needs min, max and label"
        try:
            lo, hi = int(band["min"]), int(band["max"])
        except (TypeError, ValueError):
            return False, "band min/max must be integers"
        if lo > hi:
            return False, f"band '{band.get('label')}' has min > max"
        if lo != prev_max + 1:
            return False, "bands must be contiguous (each min = previous max + 1)"
        prev_max = hi
    if prev_max != max_score:
        return False, f"bands must cover 1..{max_score} (got up to {prev_max})"
    return True, ""
