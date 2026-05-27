#!/usr/bin/env python3
"""Post a normalized scanner result to the backend webhook.

Used by .github/workflows/security.yml after each scanner step. Reads the
scanner's raw output, calls the appropriate normalizer in
tests_routes/normalizers.py, signs the canonical JSON payload with HMAC,
and POSTs it to /tests/webhook/ci.

Usage:
    post-scan-result.py --tool bandit --category backend_security_sast \\
                        --input bandit.json --returncode $bandit_rc
    post-scan-result.py --tool semgrep --category backend_security_sast \\
                        --input semgrep.sarif --returncode $semgrep_rc
    post-scan-result.py --tool pip-audit --category backend_security_deps \\
                        --input pip-audit.json --returncode $rc
    post-scan-result.py --tool coverage --category backend_coverage \\
                        --input coverage.xml --returncode $rc

Required env:
    BYTOID_BACKEND_URL                 — e.g. https://api-dev.bytoid.ai
    FRONTEND_TESTS_WEBHOOK_SECRET      — shared HMAC secret (same one bytoiddev uses)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests_routes.normalizers import (  # noqa: E402
    parse_bandit_json,
    parse_coverage_xml,
    parse_gitleaks_sarif,
    parse_pip_audit_json,
    parse_safety_json,
    parse_semgrep_sarif,
)

TOOLS = {
    "bandit": parse_bandit_json,
    "semgrep": parse_semgrep_sarif,
    "gitleaks": parse_gitleaks_sarif,
    "pip-audit": parse_pip_audit_json,
    "safety": parse_safety_json,
    "coverage": parse_coverage_xml,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tool", required=True, choices=sorted(TOOLS.keys()))
    p.add_argument("--category", required=True)
    p.add_argument("--input", required=True, help="Path to scanner output.")
    p.add_argument("--returncode", type=int, default=0)
    p.add_argument(
        "--started-at",
        default=_now_iso(),
        help="ISO timestamp of when the scanner started.",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Optional run id; defaults to a fresh UUID prefixed with the GH run.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the normalized payload without POSTing.",
    )
    args = p.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"::error::Scanner output not found at {input_path}", file=sys.stderr)
        # Still emit a synthetic failure payload so the dashboard sees the
        # tool was attempted.
        raw_text = ""
    else:
        raw_text = input_path.read_text(encoding="utf-8", errors="replace")

    run_id = args.run_id or f"gh-{os.environ.get('GITHUB_RUN_ID', uuid.uuid4().hex[:8])}-{args.tool}"
    finished_at = _now_iso()

    parser = TOOLS[args.tool]
    payload = parser(
        category=args.category,
        run_id=run_id,
        raw_text=raw_text,
        started_at=args.started_at,
        finished_at=finished_at,
        returncode=args.returncode,
    )

    body = json.dumps(payload).encode("utf-8")

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    backend_url = os.environ.get("BYTOID_BACKEND_URL")
    secret = os.environ.get("FRONTEND_TESTS_WEBHOOK_SECRET")
    if not backend_url or not secret:
        print(
            "::warning::BYTOID_BACKEND_URL / FRONTEND_TESTS_WEBHOOK_SECRET not set; "
            "skipping POST (treating as non-fatal).",
            file=sys.stderr,
        )
        return 0

    sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    endpoint = backend_url.rstrip("/") + "/tests/webhook/ci"
    req = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Bytoid-Signature": sig,
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            print(f"webhook POST ok ({resp.status}): {resp.read(500).decode()}")
        return 0
    except HTTPError as e:
        print(f"::error::webhook POST failed: {e.code} {e.read().decode(errors='replace')[:500]}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"::error::webhook POST connection error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
