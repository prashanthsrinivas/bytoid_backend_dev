#!/usr/bin/env python3
"""Re-score legacy runbook reports onto the current Impact x Likelihood scale.

Reports generated before the deterministic risk engine (commit f07d1ee) were
scored by an LLM that emitted a 1-100 risk score. The new engine
(``runbook/risk_engine.py``) computes ``risk_score = impact * likelihood`` per
risk and aggregates them on a per-org configurable scale (default 1-5 each =>
max 25). Stored scores from the old path (e.g. 55) sit above the new max, so
the UI clamps every one of them to the top band ("Critical").

The old blobs still carry each risk's ``impact`` and ``likelihood`` on the old
1-10 scale, so we can faithfully re-derive: rescale those onto the org's current
scale, re-run ``compute_risk``, and overwrite both the ``risk_score`` column and
the blob's ``risk_analysis`` (per-risk scores + levels, aggregate, level).

Scope: ``runbook_results_*`` LanceDB tables only (the entities/reports shown in
the RADAR workspace). Standalone radar reviews don't carry these scores.

Safety:
  * Dry-run by default — prints what WOULD change. Pass --apply to write.
  * Idempotent — a report is only rewritten when it's still on the old scale
    (final score above the current max, or any factor above the current scale).
    Reports already within range are left untouched, so re-running is a no-op.
  * Per result_id, deletes then re-inserts the latest finalized row (mirrors
    LanceDBServer.update_runbook_result, which avoids append-only duplicates),
    additionally updating the float ``risk_score`` column.

Usage:
    python scripts/backfill_risk_scores.py                 # dry-run, all orgs
    python scripts/backfill_risk_scores.py --user-id <id>  # dry-run, one org
    python scripts/backfill_risk_scores.py --apply         # write changes
    python scripts/backfill_risk_scores.py --old-scale 10  # legacy factor scale

Required env: same as the app (AWS creds / KMS for blob decryption, LanceDB URI).
Run it where the backend normally runs (so secrets + LanceDB are reachable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types

# --- Make the project's own packages win over any site-packages shadow. ---
# Run as `scripts/backfill_risk_scores.py`, only scripts/ (not the repo root)
# lands on sys.path, so project imports must be bootstrapped. Worse, this
# project's top-level packages (db, runbook, utils, services) are *namespace*
# packages (no __init__.py), and a namespace package loses to any *regular*
# same-named package found later on sys.path. Some venvs carry a stray PyPI
# `db` distribution (Python 2 code) that shadows ours and blows up on import.
# Pre-register the local directories as the canonical packages to avoid both.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
for _pkg in ("db", "runbook", "utils", "services"):
    _pkg_dir = os.path.join(_REPO_ROOT, _pkg)
    if os.path.isdir(_pkg_dir) and _pkg not in sys.modules:
        _mod = types.ModuleType(_pkg)
        _mod.__path__ = [_pkg_dir]
        sys.modules[_pkg] = _mod

from db.lance_db_service import LanceDBServer  # noqa: E402
from runbook.risk_engine import compute_risk, get_risk_config  # noqa: E402
from runbook.utils import _safe_json_parse_full as _parse_blob  # noqa: E402

FINAL_STATUSES = {"completed", "success", "done", "draft"}
RESULTS_PREFIX = "runbook_results_"


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rescale(value, old_scale, new_scale):
    """Map a 1..old_scale rating onto 1..new_scale, clamped to [1, new_scale]."""
    if old_scale <= 0:
        return 1
    scaled = round(_num(value, 1) * new_scale / old_scale)
    return max(1, min(int(new_scale), int(scaled)))


def _is_legacy(risk_analysis, impact_scale, likelihood_scale):
    """True when this report is still scored on the pre-engine scale."""
    max_score = impact_scale * likelihood_scale
    frs = risk_analysis.get("final_risk_score")
    if isinstance(frs, (int, float)) and frs > max_score:
        return True
    for r in risk_analysis.get("risks") or []:
        if not isinstance(r, dict):
            continue
        if _num(r.get("impact")) > impact_scale or _num(r.get("likelihood")) > likelihood_scale:
            return True
    return False


def _rescore(risk_analysis, cfg, old_scale):
    """Return a freshly computed risk_analysis dict from rescaled factors."""
    impact_scale = int(cfg.get("impact_scale", 5) or 5)
    likelihood_scale = int(cfg.get("likelihood_scale", 5) or 5)
    rescaled = []
    for r in risk_analysis.get("risks") or []:
        if not isinstance(r, dict):
            continue
        nr = dict(r)
        nr["impact"] = _rescale(r.get("impact"), old_scale, impact_scale)
        nr["likelihood"] = _rescale(r.get("likelihood"), old_scale, likelihood_scale)
        nr.pop("risk_score", None)   # force full recompute
        nr.pop("risk_level", None)
        rescaled.append(nr)
    computed = compute_risk(rescaled, cfg)
    computed["justification"] = risk_analysis.get("justification", "")
    return computed


def _iter_user_ids(db):
    for name in db.table_names():
        if name.startswith(RESULTS_PREFIX):
            yield name[len(RESULTS_PREFIX):], name


def backfill(server, only_user=None, old_scale=10, apply=False):
    db = server._connect_if_needed()
    if db is None or isinstance(db, Exception):
        print(f"❌ could not connect to LanceDB: {db}", file=sys.stderr)
        return 1

    totals = {"scanned": 0, "legacy": 0, "rewritten": 0, "skipped_no_risk": 0, "errors": 0}

    for user_id, table_name in _iter_user_ids(db):
        if only_user and user_id != only_user:
            continue
        cfg = get_risk_config(user_id)
        impact_scale = int(cfg.get("impact_scale", 5) or 5)
        likelihood_scale = int(cfg.get("likelihood_scale", 5) or 5)

        table = db.open_table(table_name)
        rows = table.search().where('status != "running"').limit(10_000_000).to_list()

        # Collapse to one finalized row per result_id (latest by ended_at).
        latest = {}
        for row in rows:
            if row.get("status") not in FINAL_STATUSES:
                continue
            rid = row.get("result_id")
            if not rid:
                continue
            if row.get("ended_at", 0) >= latest.get(rid, {}).get("ended_at", -1):
                latest[rid] = row

        for rid, row in latest.items():
            totals["scanned"] += 1
            try:
                blob = _parse_blob(server._dec(user_id, row.get("result", "{}"))) or {}
            except Exception as exc:
                totals["errors"] += 1
                print(f"  ⚠️  {user_id}/{rid}: decrypt/parse failed: {exc}")
                continue

            ra = blob.get("risk_analysis")
            if not isinstance(ra, dict) or not (ra.get("risks")):
                totals["skipped_no_risk"] += 1
                continue
            if not _is_legacy(ra, impact_scale, likelihood_scale):
                continue  # already on the current scale — idempotent skip

            totals["legacy"] += 1
            old_score = ra.get("final_risk_score")
            old_level = ra.get("risk_level")
            computed = _rescore(ra, cfg, old_scale)
            new_score = computed["final_risk_score"]
            new_level = computed["risk_level"]

            name = blob.get("report_name") or rid
            print(
                f"  {'APPLY' if apply else 'DRY '} {user_id}/{rid} "
                f"[{name}]: score {old_score}->{new_score}, level {old_level}->{new_level}"
            )

            if not apply:
                continue

            new_blob = dict(blob)
            new_blob["risk_analysis"] = computed
            new_blob["risk_score"] = new_score
            updated_row = dict(row)
            updated_row["risk_score"] = float(new_score or 0.0)
            updated_row["result"] = server._enc(user_id, json.dumps(new_blob))
            try:
                table.delete(f'result_id == "{rid}"')
                table.add([updated_row])
                totals["rewritten"] += 1
            except Exception as exc:
                totals["errors"] += 1
                print(f"  ❌ {user_id}/{rid}: write failed: {exc}")

    print(
        "\nSummary: "
        f"scanned={totals['scanned']} legacy={totals['legacy']} "
        f"rewritten={totals['rewritten']} no_risk={totals['skipped_no_risk']} "
        f"errors={totals['errors']}"
    )
    if not apply and totals["legacy"]:
        print("Dry-run only. Re-run with --apply to write these changes.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", dest="user_id", help="Limit to one owner user_id (table suffix).")
    parser.add_argument("--old-scale", dest="old_scale", type=int, default=10,
                        help="Per-factor scale the legacy LLM used (default 10).")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run).")
    args = parser.parse_args()

    server = LanceDBServer()
    return backfill(server, only_user=args.user_id, old_scale=args.old_scale, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
