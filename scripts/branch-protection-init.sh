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

# Minimum status checks we expect to exist. Add more as later phases land:
#   security/* jobs come in Phase 1
#   backend_typecheck / backend_lint come in Phase 2
required_checks=(
  '"protected-module-guardrail / guardrail"'
  '"analysis"'   # the existing SonarQube workflow
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
