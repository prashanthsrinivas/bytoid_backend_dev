"""Exhaustive parametrized regression tests for known-bug invariants.

Each parametrized test locks in a property that was once broken or could
silently regress. The bug context is stated in the test name and docstring.
"""

import json
import os
import sys
import re
from unittest.mock import MagicMock

import pytest

# ── Stub heavy deps so we can import audit_log_service / categories / store ──
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db",
             "pptx", "pptx.util", "docx", "pytz", "bs4", "yaml"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

_s3_stub = MagicMock()
_s3_stub.save_app_runbase_S3 = MagicMock(return_value=None)
sys.modules.setdefault("utils.s3_utils", _s3_stub)
_logger_stub = MagicMock()
_logger_stub.get_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault("utils.base_logger", _logger_stub)

import services.audit_log_service as aud  # noqa: E402
from tests_routes import categories as cats  # noqa: E402
from tests_routes import normalizers as norm  # noqa: E402
from tests_routes import webhook_auth as wh  # noqa: E402


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Regression: every action constant remains self-named ──────────────────────

ALL_ACTIONS = [
    n for n in dir(aud)
    if n.isupper() and isinstance(getattr(aud, n), str) and not n.startswith("_")
]

@pytest.mark.regression
@pytest.mark.parametrize("name", ALL_ACTIONS)
def test_regression_action_constant_self_named(name):
    """If someone changes `LOGIN_SUCCESS = "login_ok"` audit grep breaks."""
    assert getattr(aud, name) == name


# ── Regression: every user-mgmt action stays in user_management category ──────

USER_MGMT_ACTIONS = [
    "USER_CREATED", "USER_INVITED", "INVITE_CANCELLED", "INVITE_RESENT",
    "USER_INVITE_ACCEPTED", "USER_ROLE_CHANGED", "USER_ACCESS_REVOKED",
    "USER_ACCESS_ACTIVATED", "USER_DELETED",
]

@pytest.mark.regression
@pytest.mark.parametrize("name", USER_MGMT_ACTIONS)
def test_regression_user_action_in_user_management_category(name):
    """A misclassification once mis-bucketed USER_TYPE_CHANGED into 'auth'."""
    assert aud.ACTION_CATEGORY.get(name) == "user_management"


# ── Regression: governance constants stay in 'governance' ─────────────────────

GOVERNANCE_ACTIONS = [
    "PROTECTED_MODULE_CHANGE",
    "PROTECTED_MODULE_SUPPRESSION_BLOCKED",
    "PROTECTED_MODULE_AI_PROPOSAL_OPENED",
    "PROTECTED_MODULE_AI_ONLY_APPROVAL_BLOCKED",
]

@pytest.mark.regression
@pytest.mark.parametrize("name", GOVERNANCE_ACTIONS)
def test_regression_governance_action_in_governance(name):
    assert aud.ACTION_CATEGORY[name] == "governance"


# ── Regression: TESTS_* actions stay in 'tests' ───────────────────────────────

TEST_ACTIONS = [n for n in ALL_ACTIONS if n.startswith("TESTS_")]

@pytest.mark.regression
@pytest.mark.parametrize("name", TEST_ACTIONS)
def test_regression_tests_actions_in_tests_category(name):
    if name in aud.ACTION_CATEGORY:
        assert aud.ACTION_CATEGORY[name] == "tests"


# ── Regression: every category retains required keys ──────────────────────────

@pytest.mark.regression
@pytest.mark.parametrize("category", list(cats.ALL_CATEGORIES.keys()))
def test_regression_category_has_display_name(category):
    assert "display_name" in cats.ALL_CATEGORIES[category]

@pytest.mark.regression
@pytest.mark.parametrize("category", list(cats.ALL_CATEGORIES.keys()))
def test_regression_category_has_runner(category):
    assert "runner" in cats.ALL_CATEGORIES[category]

@pytest.mark.regression
@pytest.mark.parametrize("category", list(cats.ALL_CATEGORIES.keys()))
def test_regression_category_has_delegated(category):
    assert "delegated" in cats.ALL_CATEGORIES[category]


# ── Regression: frontend & specific backend categories must remain delegated ──

ALWAYS_DELEGATED = [
    "frontend_unit", "frontend_integration", "frontend_e2e", "frontend_typecheck",
    "frontend_regression",
    "backend_security_sast", "backend_security_secrets", "backend_security_deps",
    "backend_security_authz", "backend_security_api", "backend_security_llm",
    "backend_security_infra", "backend_coverage", "backend_typecheck",
    "backend_lint", "backend_mutation",
]

@pytest.mark.regression
@pytest.mark.parametrize("category", ALWAYS_DELEGATED)
def test_regression_category_remains_delegated(category):
    assert cats.is_delegated(category) is True

NEVER_DELEGATED = [
    "backend_unit", "backend_integration", "backend_regression",
    "backend_load", "backend_stress", "backend_performance",
]

@pytest.mark.regression
@pytest.mark.parametrize("category", NEVER_DELEGATED)
def test_regression_category_remains_locally_dispatchable(category):
    assert cats.is_delegated(category) is False
    assert cats.is_locally_dispatchable(category) is True


# ── Regression: HMAC verification rejects tampered payloads (cryptographic) ──

VALID_SECRET = "regression-secret"

def _make_req(body: bytes, sig: str, secret: str = VALID_SECRET):
    import flask, os as _os
    app = flask.Flask(__name__)
    ctx = app.test_request_context(
        "/tests/webhook/ci", method="POST", data=body,
        content_type="application/json",
        headers={wh.SIGNATURE_HEADER: sig},
    )
    return app, ctx


TAMPER_CASES = [
    b'{"category":"backend_unit"}\n',           # newline appended
    b' {"category":"backend_unit"}',            # leading space
    b'{"category":"backend_security_sast"}',    # category swap
    b'{"category":"backend_unit","x":1}',       # field added
    b'',                                         # zero-length
    b'{"category":"BACKEND_UNIT"}',             # case change
    b'{"Category":"backend_unit"}',             # key case
]

@pytest.mark.regression
@pytest.mark.parametrize("tampered", TAMPER_CASES)
def test_regression_hmac_rejects_tampered_body(tampered, monkeypatch):
    """Original body signs to sig; any tamper must invalidate."""
    import hmac, hashlib
    monkeypatch.setenv(wh.SECRET_ENV_VAR, VALID_SECRET)
    original = b'{"category":"backend_unit"}'
    sig = "sha256=" + hmac.new(VALID_SECRET.encode(), original, hashlib.sha256).hexdigest()
    if tampered == original:
        # case where parametrize value equals original — verify same body still passes
        app, ctx = _make_req(tampered, sig)
    else:
        app, ctx = _make_req(tampered, sig)
    with ctx:
        import flask
        if tampered == original:
            assert wh.verify_hmac(flask.request) is True
        else:
            assert wh.verify_hmac(flask.request) is False


# ── Regression: empty/garbage scanner outputs are never crashes ──────────────

PARSERS = [
    norm.parse_bandit_json, norm.parse_semgrep_sarif, norm.parse_gitleaks_sarif,
    norm.parse_pip_audit_json, norm.parse_safety_json, norm.parse_coverage_xml,
    norm.parse_mypy_json, norm.parse_pylint_json, norm.parse_ruff_sarif,
    norm.parse_mutmut_results,
]

# Inputs the normalizers contractually accept. JSON literals like "null"/"true"
# are NOT in the contract — scanners always emit objects/SARIF/XML.
GARBAGE_INPUTS = [
    "", " ", "\n", "{}",
    "<bad", "<?xml version=\"1.0\"?><x/>", "not json or xml",
    "{ malformed ",
]

@pytest.mark.regression
@pytest.mark.parametrize("fn", PARSERS)
@pytest.mark.parametrize("txt", GARBAGE_INPUTS)
def test_regression_parsers_never_crash_on_garbage(fn, txt):
    """Prior regression: a malformed scanner output crashed the post-script."""
    out = fn(category="x", run_id="r",
             raw_text=txt, started_at="2026-01-01T00:00:00+00:00",
             finished_at="2026-01-01T00:01:00+00:00", returncode=0)
    assert isinstance(out, dict)
    assert "summary" in out and "status" in out and "tests" in out


# ── Regression: webhook signature header name must remain X-Bytoid-Signature ──

@pytest.mark.regression
def test_regression_signature_header_name():
    """bytoiddev CI signs with this exact header; renaming breaks ingest."""
    assert wh.SIGNATURE_HEADER == "X-Bytoid-Signature"

@pytest.mark.regression
def test_regression_secret_env_var_name():
    """The env var name must remain stable — both backend .env and GitHub secret use it."""
    assert wh.SECRET_ENV_VAR == "FRONTEND_TESTS_WEBHOOK_SECRET"


# ── Regression: status_from_summary semantics around rc==0 vs non-zero ────────

@pytest.mark.regression
@pytest.mark.parametrize("summary,rc,expected", [
    # rc=0 with no failures → passed
    ({"failed": 0, "errors": 0, "total": 1}, 0, "passed"),
    # any failures → failed regardless of rc
    ({"failed": 1, "errors": 0, "total": 1}, 0, "failed"),
    ({"failed": 0, "errors": 1, "total": 1}, 0, "failed"),
    # rc!=0 with zero tests → failed (means the runner crashed)
    ({"failed": 0, "errors": 0, "total": 0}, 1, "failed"),
    # rc!=0 with tests present → passed (rc may be from skipped exit codes)
    ({"failed": 0, "errors": 0, "total": 5}, 1, "passed"),
])
def test_regression_status_from_summary(summary, rc, expected):
    assert norm._status_from_summary(summary, returncode=rc) == expected


# ── Regression: every backend category has a runner key matching known runners

VALID_RUNNERS = {
    "pytest", "locust", "multi-sast", "secrets", "deps", "coverage",
    "mypy", "lint", "pytest-security", "mutmut",
    "vitest", "playwright", "tsc",
}

@pytest.mark.regression
@pytest.mark.parametrize("category,meta", list(cats.ALL_CATEGORIES.items()))
def test_regression_runner_value_known(category, meta):
    assert meta["runner"] in VALID_RUNNERS, f"{category} has invalid runner {meta['runner']!r}"


# ── Regression: every is_delegated == metadata.delegated ──────────────────────

@pytest.mark.regression
@pytest.mark.parametrize("category", list(cats.ALL_CATEGORIES.keys()))
def test_regression_is_delegated_matches_metadata(category):
    assert cats.is_delegated(category) == cats.ALL_CATEGORIES[category]["delegated"]


# ── Regression: find_admin_change.py at repo root ─────────────────────────────

@pytest.mark.regression
def test_regression_find_admin_change_at_repo_root():
    assert os.path.isfile(os.path.join(_REPO_ROOT, "find_admin_change.py"))


# ── Regression: post-scan-result.py uses BYTOID_BACKEND_URL ───────────────────

@pytest.mark.regression
def test_regression_post_script_uses_bytoid_backend_url():
    """A renaming bug once changed this to BACKEND_URL, breaking CI -> dashboard."""
    script = os.path.join(_REPO_ROOT, "scripts", "post-scan-result.py")
    with open(script, encoding="utf-8") as f:
        text = f.read()
    assert "BYTOID_BACKEND_URL" in text
    assert "BACKEND_URL" not in re.sub(r"BYTOID_BACKEND_URL", "", text)  # no bare BACKEND_URL


# ── Regression: post-scan-result.py POSTs to /tests/webhook/ci ────────────────

@pytest.mark.regression
def test_regression_post_script_uses_ci_webhook_path():
    script = os.path.join(_REPO_ROOT, "scripts", "post-scan-result.py")
    with open(script, encoding="utf-8") as f:
        text = f.read()
    assert "/tests/webhook/ci" in text


# ── Regression: every protected-module Semgrep rule exists ────────────────────

REQUIRED_PROTECTED_RULES = [
    "no_authz_removal.yml",
    "no_audit_log_removal.yml",
    "no_crypto_downgrade.yml",
    "no_serializer_pickle.yml",
]

@pytest.mark.regression
@pytest.mark.parametrize("filename", REQUIRED_PROTECTED_RULES)
def test_regression_protected_semgrep_rule_present(filename):
    rule_path = os.path.join(_REPO_ROOT, ".semgrep", "protected", filename)
    assert os.path.isfile(rule_path), f"Missing protected Semgrep rule: {filename}"


# ── Regression: CI workflow file present ──────────────────────────────────────

@pytest.mark.regression
@pytest.mark.parametrize("workflow", [
    ".github/workflows/security.yml",
    ".github/workflows/sonarqube.yml",
])
def test_regression_workflow_file_present(workflow):
    assert os.path.isfile(os.path.join(_REPO_ROOT, workflow))


# ── Regression: branch-protection script remains executable shell ─────────────

@pytest.mark.regression
def test_regression_branch_protection_script_present():
    p = os.path.join(_REPO_ROOT, "scripts", "branch-protection-init.sh")
    assert os.path.isfile(p)
    with open(p) as f:
        first = f.readline()
    assert first.startswith("#!"), "branch-protection-init.sh must start with a shebang"


# ── Regression: docs files present (governance) ───────────────────────────────

@pytest.mark.regression
@pytest.mark.parametrize("doc", [
    "docs/security/PROTECTED_MODULES.md",
    "docs/security/SUPPRESSION_PLAYBOOK.md",
    "docs/security/CHANGE_REVIEW_TEMPLATE.md",
])
def test_regression_doc_file_present(doc):
    assert os.path.isfile(os.path.join(_REPO_ROOT, doc)), f"Missing doc: {doc}"


# ── Regression: ACTION_CATEGORY size doesn't shrink unexpectedly ─────────────

@pytest.mark.regression
def test_regression_action_category_size_stays_large():
    """If this drops sharply, someone removed action mappings — investigate."""
    assert len(aud.ACTION_CATEGORY) >= 100


# ── Regression: bandit CWE table size ─────────────────────────────────────────

@pytest.mark.regression
def test_regression_bandit_cwe_table_size():
    assert len(norm._BANDIT_CWE) >= 30
