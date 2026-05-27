#!/usr/bin/env bash
# Emit the list of Python files changed in this PR, one per line.
#
# Used by CI to scope SAST/typecheck/lint to changed files only (PRs)
# while still running the full repo on push-to-main.
#
# Usage:
#   ./scripts/changed-python-files.sh                 # auto-detects base ref
#   BASE_REF=develop ./scripts/changed-python-files.sh
#
# When run inside GitHub Actions on a pull_request event, GITHUB_BASE_REF
# is set automatically.

set -euo pipefail

BASE="${BASE_REF:-${GITHUB_BASE_REF:-main}}"

# Try the PR diff first (triple-dot = merge-base, what GH UI compares).
if git rev-parse --verify "origin/${BASE}" >/dev/null 2>&1; then
  range="origin/${BASE}...HEAD"
else
  range="${BASE}...HEAD"
fi

# --diff-filter=ACMRT: added/copied/modified/renamed/type-changed.
# Excludes deletes (D) so we don't try to lint removed files.
git diff --name-only --diff-filter=ACMRT "$range" -- '*.py' || true
