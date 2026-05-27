#!/usr/bin/env bash
# Idempotent branch-protection initializer for the protected-module governance.
#
# Applies branch protection to the default branch with:
#   - required status checks: protected-module-guardrail + sonarqube + (eventually) security/* jobs
#   - required PR reviews: at least 1 approving review, dismiss stale on push
#   - linear history required
#   - force pushes disallowed
#   - branch deletions disallowed
#   - admins are NOT exempt (so security leads can't bypass)
#
# Usage:
#   GITHUB_TOKEN=$(gh auth token) ./scripts/branch-protection-init.sh
#
# Or with a custom repo / branch:
#   REPO=owner/repo BRANCH=main ./scripts/branch-protection-init.sh
#
# Re-run safely: applies the same JSON each time.

set -euo pipefail

REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)}"
BRANCH="${BRANCH:-main}"

if [ -z "${REPO}" ]; then
  echo "Cannot determine repo. Set REPO=owner/repo or run from inside a gh-configured clone." >&2
  exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh (GitHub CLI) is required: https://cli.github.com/" >&2
  exit 2
fi

echo "Applying branch protection to ${REPO}@${BRANCH}…"

# Required status checks: updated in Phase 6 to include the four scanner
# jobs that are now blocking (bandit, semgrep, gitleaks, trufflehog).
# Phase 2 (mypy/ruff/pylint) and Phase 4/5 remain warn-only; add them here
# once their baselines are clean.
required_checks=(
  '"protected-module-guardrail / guardrail"'
  '"analysis"'                              # existing SonarQube workflow
  '"Security & Coverage / bandit"'          # Phase 6: HIGH+ SAST findings block
  '"Security & Coverage / semgrep"'         # Phase 6: ERROR SAST findings block
  '"Security & Coverage / gitleaks"'        # Phase 6: any secret blocks
  '"Security & Coverage / trufflehog"'      # Phase 6: any verified secret blocks
)

contexts=$(IFS=,; echo "${required_checks[*]}")

PAYLOAD=$(cat <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": [ ${contexts} ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "require_last_push_approval": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON
)

echo "$PAYLOAD" | gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  "repos/${REPO}/branches/${BRANCH}/protection" \
  --input -

echo "Branch protection applied. Verify in the GitHub UI under Settings → Branches."
