#!/usr/bin/env python3
"""Protected-module guardrail.

Enforces the requirements in security/protected_modules.yml on every PR.

Modes:
    --mode=ci          (default in GH Actions): full check, exits non-zero on violation.
    --mode=local       runnable via `make protected-check` against the current diff.
    --mode=labels      sets PR labels via gh api; no exit-code enforcement.
    --mode=suppression checks only that no new suppression patterns landed in protected paths.

Inputs (env vars when run in CI):
    GITHUB_BASE_REF        base branch (e.g. "main")
    GITHUB_HEAD_REF        PR head branch
    GITHUB_PR_NUMBER       PR number (set explicitly in the workflow)
    GITHUB_PR_AUTHOR       PR author login
    GITHUB_PR_TITLE        PR title (used to check the [ai-proposal] prefix)
    GITHUB_PR_BODY         PR body (used to check the structured block)
    GITHUB_PR_REVIEWS_JSON JSON-array of {user_login, state} for current reviews
    GITHUB_REPOSITORY      "owner/repo"

The script is intentionally dependency-light: only PyYAML is required. PyYAML
is already installed in the venv (transitive via several packages).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "protected-module-guardrail: PyYAML is required. `pip install pyyaml`.\n"
    )
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "security" / "protected_modules.yml"


@dataclass
class GuardrailFinding:
    code: str
    severity: str  # "block" | "warn"
    message: str
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            **self.extra,
        }


def load_spec() -> dict:
    if not SPEC_PATH.exists():
        raise SystemExit(
            f"protected-module-guardrail: spec missing at {SPEC_PATH}. "
            "Phase 0 governance scaffolding was not applied."
        )
    with SPEC_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT).decode("utf-8")


def changed_files(base_ref: str | None) -> list[str]:
    """Return files changed in the current PR (or working tree if no base)."""
    if base_ref:
        try:
            # `...` (triple dot) = merge-base — semantics we want for PR diffs.
            out = _git("diff", "--name-only", f"origin/{base_ref}...HEAD")
        except subprocess.CalledProcessError:
            out = _git("diff", "--name-only", base_ref)
    else:
        # Local mode: uncommitted + committed-on-this-branch vs main.
        try:
            out = _git("diff", "--name-only", "origin/main...HEAD")
        except subprocess.CalledProcessError:
            out = _git("diff", "--name-only")
    return [line for line in out.splitlines() if line.strip()]


def changed_lines_added(base_ref: str | None, path: str) -> list[str]:
    """Return only added lines (no '+' prefix) for the given path in this PR."""
    range_spec = f"origin/{base_ref}...HEAD" if base_ref else "origin/main...HEAD"
    try:
        diff = _git("diff", "--unified=0", range_spec, "--", path)
    except subprocess.CalledProcessError:
        return []
    added: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return added


def _matches_any(path: str, globs: Iterable[str]) -> bool:
    for g in globs:
        # Translate `**` for fnmatch by treating dirs flexibly.
        if g.endswith("/**"):
            prefix = g[:-3]
            if path.startswith(prefix.rstrip("/") + "/") or path == prefix.rstrip("/"):
                return True
        elif fnmatch.fnmatch(path, g):
            return True
        elif "/**/" in g:
            # crude: replace **/ with .*
            regex = "^" + re.escape(g).replace(r"\*\*/", ".*").replace(r"\*", "[^/]*") + "$"
            if re.match(regex, path):
                return True
    return False


def protected_paths(spec: dict, files: list[str]) -> dict[str, list[str]]:
    """Return {category_name: [files...]} for files that intersect the spec."""
    hit: dict[str, list[str]] = {}
    for cat_name, cat in (spec.get("categories") or {}).items():
        for path in files:
            if _matches_any(path, cat.get("paths") or []):
                hit.setdefault(cat_name, []).append(path)
    return hit


def detect_new_suppressions(
    spec: dict, base_ref: str | None, protected_files: list[str]
) -> list[GuardrailFinding]:
    """Reject diffs that add a forbidden suppression token on a protected line."""
    findings: list[GuardrailFinding] = []
    patterns: list[str] = spec.get("forbidden_suppression_patterns") or []
    if not patterns:
        return findings
    for path in protected_files:
        added = changed_lines_added(base_ref, path)
        for line in added:
            for pat in patterns:
                if pat in line:
                    findings.append(
                        GuardrailFinding(
                            code="PROTECTED_MODULE_SUPPRESSION_BLOCKED",
                            severity="block",
                            message=(
                                f"New suppression token '{pat}' introduced in "
                                f"protected file '{path}'. Suppressions are not "
                                "allowed in protected modules; fix the finding or "
                                "open a CODEOWNERS-reviewed re-classification PR."
                            ),
                            extra={"path": path, "pattern": pat, "line": line.strip()[:200]},
                        )
                    )
    return findings


def detect_ai_only_approvals(spec: dict, reviews_json: str) -> list[GuardrailFinding]:
    """Require at least one approving review from a non-bot human."""
    findings: list[GuardrailFinding] = []
    ai_logins = {login.lower() for login in (spec.get("ai_actor_logins") or [])}
    try:
        reviews = json.loads(reviews_json) if reviews_json else []
    except json.JSONDecodeError:
        reviews = []
    approving = [
        r for r in reviews if (r.get("state") or "").upper() == "APPROVED"
    ]
    human_approving = [
        r for r in approving if (r.get("user_login") or "").lower() not in ai_logins
    ]
    if not human_approving:
        findings.append(
            GuardrailFinding(
                code="PROTECTED_MODULE_AI_ONLY_APPROVAL",
                severity="block",
                message=(
                    "This protected-module PR has no approving review from a "
                    "human (non-bot) account. AI-account reviews don't count "
                    "toward the required-approval threshold."
                ),
                extra={
                    "ai_logins": sorted(ai_logins),
                    "approving_reviewers": [r.get("user_login") for r in approving],
                },
            )
        )
    return findings


_RISK_LABEL_RE = re.compile(r"^risk:(low|medium|high|critical)$", re.IGNORECASE)


def detect_missing_risk_label(labels: list[str]) -> list[GuardrailFinding]:
    if any(_RISK_LABEL_RE.match(label) for label in labels):
        return []
    return [
        GuardrailFinding(
            code="PROTECTED_MODULE_RISK_LABEL_MISSING",
            severity="block",
            message=(
                "This protected-module PR must carry one of the labels "
                "`risk:low`, `risk:medium`, `risk:high`, `risk:critical`. "
                "Apply the appropriate label after reviewing the diff."
            ),
        )
    ]


def detect_missing_ai_proposal_block(
    is_ai_pr: bool, pr_title: str, pr_body: str
) -> list[GuardrailFinding]:
    if not is_ai_pr:
        return []
    findings = []
    if not (pr_title or "").lower().startswith("[ai-proposal]"):
        findings.append(
            GuardrailFinding(
                code="PROTECTED_MODULE_AI_PROPOSAL_TITLE_MISSING",
                severity="block",
                message=(
                    "AI-authored PRs touching protected modules must prefix the "
                    "title with '[ai-proposal]'."
                ),
            )
        )
    required_sections = ("Risk:", "Rationale:", "Test evidence:")
    missing = [s for s in required_sections if s not in (pr_body or "")]
    if missing:
        findings.append(
            GuardrailFinding(
                code="PROTECTED_MODULE_AI_PROPOSAL_BLOCK_MISSING",
                severity="block",
                message=(
                    "AI-authored protected-module PR is missing the structured "
                    f"description block. Required headings: {required_sections}. "
                    f"Missing: {missing}"
                ),
            )
        )
    return findings


def write_github_outputs(
    pr_is_protected: bool,
    categories: list[str],
    findings: list[GuardrailFinding],
) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(f"protected_pr={'true' if pr_is_protected else 'false'}\n")
        f.write(f"protected_categories={','.join(categories)}\n")
        # Use a heredoc-style multiline value for findings.
        f.write("findings<<EOF\n")
        f.write(json.dumps([fnd.as_dict() for fnd in findings], indent=2))
        f.write("\nEOF\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--mode",
        choices=("ci", "local", "labels", "suppression"),
        default="ci",
    )
    p.add_argument(
        "--base-ref",
        default=os.environ.get("GITHUB_BASE_REF") or None,
    )
    args = p.parse_args()

    spec = load_spec()
    files = changed_files(args.base_ref)
    hit = protected_paths(spec, files)
    protected_files = sorted({f for fs in hit.values() for f in fs})
    is_protected = bool(protected_files)

    findings: list[GuardrailFinding] = []

    if is_protected:
        findings.extend(detect_new_suppressions(spec, args.base_ref, protected_files))

        if args.mode == "ci":
            pr_author = os.environ.get("GITHUB_PR_AUTHOR", "")
            ai_logins = {login.lower() for login in (spec.get("ai_actor_logins") or [])}
            is_ai_pr = pr_author.lower() in ai_logins
            findings.extend(
                detect_missing_ai_proposal_block(
                    is_ai_pr,
                    os.environ.get("GITHUB_PR_TITLE", ""),
                    os.environ.get("GITHUB_PR_BODY", ""),
                )
            )
            labels = [
                lbl.strip()
                for lbl in os.environ.get("GITHUB_PR_LABELS", "").split(",")
                if lbl.strip()
            ]
            findings.extend(detect_missing_risk_label(labels))
            findings.extend(
                detect_ai_only_approvals(spec, os.environ.get("GITHUB_PR_REVIEWS_JSON", ""))
            )

    # Human-readable report on stderr; machine-readable on stdout.
    if is_protected:
        sys.stderr.write(
            "protected-module-guardrail: PR intersects protected modules.\n"
        )
        for cat, paths in hit.items():
            sys.stderr.write(f"  [{cat}] {len(paths)} file(s):\n")
            for p_ in paths:
                sys.stderr.write(f"    - {p_}\n")
    else:
        sys.stderr.write(
            "protected-module-guardrail: PR does not intersect any protected module.\n"
        )

    if findings:
        sys.stderr.write("\nFINDINGS:\n")
        for f in findings:
            sys.stderr.write(f"  [{f.severity.upper()}][{f.code}] {f.message}\n")

    sys.stdout.write(
        json.dumps(
            {
                "protected_pr": is_protected,
                "categories": sorted(hit.keys()),
                "protected_files": protected_files,
                "findings": [fnd.as_dict() for fnd in findings],
            },
            indent=2,
        )
    )
    sys.stdout.write("\n")

    write_github_outputs(is_protected, sorted(hit.keys()), findings)

    # In suppression mode we only fail on suppression findings.
    if args.mode == "suppression":
        return 1 if any(f.code == "PROTECTED_MODULE_SUPPRESSION_BLOCKED" for f in findings) else 0

    # In labels mode we don't fail; just emit.
    if args.mode == "labels":
        return 0

    # ci / local: fail on any block finding when the PR is protected.
    if is_protected and any(f.severity == "block" for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
