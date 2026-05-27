"""Property-based fuzz tests for pure-logic utilities in utils/normal.py
and tests_routes/webhook_auth.py.

Uses Hypothesis for property-based testing. All tests are skipped gracefully
if Hypothesis is not installed (it is an optional dev dependency).

Each test validates a never-raise or output-invariant property across the
full input space — the kind of guarantee that hand-written examples cannot
exhaustively cover.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Skip entire module if hypothesis is not available
# ---------------------------------------------------------------------------

hypothesis = pytest.importorskip(
    "hypothesis",
    reason="hypothesis not installed; install with: pip install hypothesis",
)
given = hypothesis.given
settings = hypothesis.settings
st = hypothesis.strategies

# ---------------------------------------------------------------------------
# Stub heavy imports before loading the modules under test
# ---------------------------------------------------------------------------

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db",
             # utils.normal imports these at module level
             "pptx", "pptx.util", "bs4", "pytz", "yaml", "docx"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

sys.modules.setdefault("utils.s3_utils", MagicMock())
sys.modules.setdefault("utils.base_logger", MagicMock(get_logger=MagicMock(return_value=MagicMock())))

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from utils.normal import (  # noqa: E402
    can_reply_to_email,
    parse_composite_user_id,
    sanitize_value,
    strip_html,
)
from tests_routes.categories import (  # noqa: E402
    is_backend_category,
    is_delegated,
    is_valid_category,
)

# Set the env var for HMAC tests before importing webhook_auth
os.environ.setdefault("FRONTEND_TESTS_WEBHOOK_SECRET", "test-secret-for-fuzz")
from tests_routes.webhook_auth import sign  # noqa: E402

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.text())
def test_parse_composite_user_id_never_raises(raw):
    """parse_composite_user_id never raises for any string input."""
    result = parse_composite_user_id(raw)
    assert isinstance(result, tuple)
    assert len(result) == 2


@pytest.mark.fuzz
@settings(max_examples=200)
@given(
    st.text(min_size=1).filter(
        lambda s: "##SU##" not in s and "%23%23SU%23%23" not in s
    )
)
def test_parse_composite_user_id_plain_returns_same(plain):
    """For a plain user_id (no ##SU## separator), both returned values equal the input."""
    import urllib.parse
    actor, target = parse_composite_user_id(plain)
    decoded = urllib.parse.unquote(plain)
    assert actor == decoded
    assert target == decoded


@pytest.mark.fuzz
@settings(max_examples=1)
def test_parse_composite_user_id_none_input():
    """parse_composite_user_id(None) always returns (None, None)."""
    actor, target = parse_composite_user_id(None)
    assert actor is None
    assert target is None


@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.text())
def test_can_reply_to_email_never_raises(s):
    """can_reply_to_email never raises for any string input."""
    result = can_reply_to_email(s)
    # No exception expected; result type is checked in the next test


@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.text())
def test_can_reply_to_email_returns_bool(s):
    """can_reply_to_email always returns a bool."""
    result = can_reply_to_email(s)
    assert isinstance(result, bool)


@pytest.mark.fuzz
@settings(max_examples=200)
@given(
    st.one_of(
        st.text(),
        st.integers(),
        st.floats(allow_nan=False),
        st.booleans(),
        st.none(),
    )
)
def test_sanitize_value_never_raises_on_primitives(v):
    """sanitize_value never raises for any primitive value."""
    sanitize_value(v)


@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.text())
def test_sanitize_value_string_result_escapes_lt_gt(s):
    """If '<' is in the input string, it must not appear literally in the output string."""
    result = sanitize_value(s)
    result_str = str(result)
    if "<" in s:
        assert "<" not in result_str, (
            f"sanitize_value failed to escape '<' in: {s!r} -> {result_str!r}"
        )


@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.text())
def test_strip_html_never_raises(s):
    """strip_html never raises for any string input."""
    strip_html(s)


@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.binary())
def test_webhook_hmac_sign_deterministic(body):
    """sign(body) is deterministic: same body always produces the same signature."""
    os.environ["FRONTEND_TESTS_WEBHOOK_SECRET"] = "test-secret-for-fuzz"
    sig1 = sign(body)
    sig2 = sign(body)
    assert sig1 == sig2
    assert sig1.startswith("sha256=")


@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.text())
def test_category_helpers_never_raise(s):
    """is_valid_category, is_backend_category, and is_delegated never raise."""
    # All three are pure dict lookups — they must not raise for any string
    is_valid_category(s)
    is_backend_category(s)
    is_delegated(s)
