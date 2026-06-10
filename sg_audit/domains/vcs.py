"""Source-control (GitHub) domain — org/repo + CI posture (global, opt-in).

Runs ONCE per scan (SCOPE="global"), not per AWS account, since GitHub is
org-global. Requires ``SG_GITHUB_TOKEN`` + ``SG_GITHUB_ORG``; a safe no-op when
unset. Flags public repos, default branches without protection, GitHub Actions
default write permissions, and open secret-scanning alerts. Only api.github.com
(a fixed trusted host) is contacted; repo count is capped to bound rate limits.
Findings are scoped to the GitHub org (entity = repository).
"""

from __future__ import annotations

from contextlib import suppress

from sg_audit import config as sg_config
from sg_audit.analysis.normalize import make_domain_finding
from sg_audit.metadata import (
    VCS_ACTIONS_WRITE_DEFAULT,
    VCS_NO_BRANCH_PROTECTION,
    VCS_PUBLIC_REPO,
    VCS_SECRET_SCANNING_ALERT,
)
from sg_audit.schema import DOMAIN_VCS

DOMAIN = DOMAIN_VCS
SCOPE = "global"
_SOURCE = "github"


# ── pure analyzers ────────────────────────────────────────────────────────────

def _f(org, repo, rule_id, severity, summary, details=None):
    return make_domain_finding(
        rule_id=rule_id, severity=severity, finding_summary=summary,
        account_id=org, entity_type="repository", entity_id=repo, entity_name=repo,
        source=_SOURCE, details=details or {})


def analyze_repo(org, repo) -> list[dict]:
    """One repo dict from the GitHub list API -> repo-visibility finding."""
    out = []
    name = repo.get("full_name") or repo.get("name", "")
    if repo.get("private") is False or repo.get("visibility") == "public":
        out.append(_f(org, name, VCS_PUBLIC_REPO, "medium", f"Repository '{name}' is public"))
    return out


def analyze_repo_controls(org, repo_name, *, protected, default_wf_permission, secret_alerts) -> list[dict]:
    """``protected`` (bool|None), ``default_wf_permission`` (str|None),
    ``secret_alerts`` (list|None) gathered per repo."""
    out = []
    if protected is False:
        out.append(_f(org, repo_name, VCS_NO_BRANCH_PROTECTION, "medium",
                      f"Default branch of '{repo_name}' has no protection rule"))
    if default_wf_permission == "write":
        out.append(_f(org, repo_name, VCS_ACTIONS_WRITE_DEFAULT, "medium",
                      f"GitHub Actions default token has write permissions in '{repo_name}'"))
    for alert in secret_alerts or []:
        sid = alert.get("number")
        kind = alert.get("secret_type_display_name") or alert.get("secret_type") or "secret"
        out.append(_f(org, repo_name, VCS_SECRET_SCANNING_ALERT, "high",
                      f"Open secret-scanning alert in '{repo_name}': {kind}",
                      {"alert": sid, "secret_type": alert.get("secret_type")}))
    return out


# ── GitHub API collect (best-effort) ──────────────────────────────────────────

def _gh(path, token, params=None):
    import requests

    resp = requests.get(
        f"{sg_config.SG_GITHUB_API_URL.rstrip('/')}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params or {}, timeout=15,
    )
    if resp.status_code >= 400:
        return None
    return resp.json()


def collect(session, account_id: str, account_name: str, region: str, regions=None) -> list[dict]:
    if not sg_config.github_enabled():
        return []
    token, org = sg_config.SG_GITHUB_TOKEN, sg_config.SG_GITHUB_ORG
    out: list[dict] = []

    repos: list[dict] = []
    page = 1
    while len(repos) < sg_config.SG_GITHUB_MAX_REPOS:
        batch = _gh(f"/orgs/{org}/repos", token, {"per_page": 100, "page": page, "type": "all"})
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    repos = repos[: sg_config.SG_GITHUB_MAX_REPOS]

    for repo in repos:
        name = repo.get("name", "")
        full = repo.get("full_name") or f"{org}/{name}"
        out += analyze_repo(org, repo)

        protected = None
        default_branch = repo.get("default_branch") or "main"
        with suppress(Exception):
            bp = _gh(f"/repos/{org}/{name}/branches/{default_branch}/protection", token)
            protected = bool(bp)
        wf_perm = None
        with suppress(Exception):
            wf = _gh(f"/repos/{org}/{name}/actions/permissions/workflow", token)
            wf_perm = (wf or {}).get("default_workflow_permissions")
        alerts = None
        with suppress(Exception):
            alerts = _gh(f"/repos/{org}/{name}/secret-scanning/alerts", token, {"state": "open", "per_page": 100})

        out += analyze_repo_controls(org, full, protected=protected,
                                     default_wf_permission=wf_perm, secret_alerts=alerts)
    return out
