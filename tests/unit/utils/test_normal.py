"""Unit tests for utils/normal.py.

utils/normal.py imports pptx, pytz, and docx at the top level.  Those packages
may not be installed in CI, so we pre-stub them in sys.modules before the first
import of utils.normal so the module loads successfully.  The functions under
test (parse_composite_user_id, can_reply_to_email, sanitize_value, strip_html)
do not use any of those heavy libraries — they rely only on re, urllib.parse,
markupsafe, and bs4 (all available in the dev environment).
"""

import sys
import types
from unittest.mock import MagicMock

import pytest
from markupsafe import Markup

# ---------------------------------------------------------------------------
# Pre-stub heavy optional deps that utils/normal.py imports at module level
# ---------------------------------------------------------------------------

def _stub_if_missing(mod_name: str):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock(name=f"{mod_name}_stub")

for _mod in ("pptx", "pptx.util", "docx", "pytz"):
    _stub_if_missing(_mod)

# pytz.timezone() is called at runtime in some functions; make it return a usable mock
if not hasattr(sys.modules.get("pytz", object()), "_real"):
    import datetime
    _pytz_stub = sys.modules["pytz"]
    _pytz_stub.timezone = MagicMock(return_value=MagicMock(
        localize=lambda self, dt: dt,
        __str__=lambda self: "Asia/Kolkata",
    ))

from utils.normal import (  # noqa: E402
    parse_composite_user_id,
    can_reply_to_email,
    sanitize_value,
    strip_html,
)


# ===========================================================================
# parse_composite_user_id
# ===========================================================================


@pytest.mark.unit
def test_parse_composite_literal_separator():
    """admin##SU##target splits correctly."""
    logged_in, target = parse_composite_user_id("admin123##SU##target456")
    assert logged_in == "admin123"
    assert target == "target456"


@pytest.mark.unit
def test_parse_composite_url_encoded_separator():
    """URL-encoded variant %23%23SU%23%23 must be decoded and split correctly."""
    logged_in, target = parse_composite_user_id("admin%23%23SU%23%23target456")
    assert logged_in == "admin"
    assert target == "target456"


@pytest.mark.unit
def test_parse_plain_user_id_returns_same_both():
    """A plain (non-composite) user id returns (uid, uid)."""
    logged_in, target = parse_composite_user_id("plain_user")
    assert logged_in == "plain_user"
    assert target == "plain_user"


@pytest.mark.unit
def test_parse_none_returns_none_pair():
    """None input must return (None, None)."""
    logged_in, target = parse_composite_user_id(None)
    assert logged_in is None
    assert target is None


@pytest.mark.unit
def test_parse_empty_string_returns_none_pair():
    """Empty string must return (None, None)."""
    logged_in, target = parse_composite_user_id("")
    assert logged_in is None
    assert target is None


@pytest.mark.unit
def test_parse_composite_whitespace_stripped():
    """Leading/trailing whitespace around each segment should be stripped."""
    logged_in, target = parse_composite_user_id("admin ##SU## target ")
    assert logged_in == "admin"
    assert target == "target"


@pytest.mark.unit
def test_parse_user_id_with_at_sign():
    """Email-like user ids (no ##SU##) should be returned as-is."""
    logged_in, target = parse_composite_user_id("user@org.com")
    assert logged_in == "user@org.com"
    assert target == "user@org.com"


# ===========================================================================
# can_reply_to_email
# ===========================================================================


@pytest.mark.unit
def test_can_reply_normal_com_email():
    """Standard .com email with no blocked keywords should be allowed."""
    assert can_reply_to_email("alice@company.com") is True


@pytest.mark.unit
def test_can_reply_bytoid_domain_bypasses_all_rules():
    """Any @bytoid.* address must always be allowed regardless of TLD."""
    assert can_reply_to_email("alice@bytoid.ca") is True
    assert can_reply_to_email("support@bytoid.io") is True
    assert can_reply_to_email("noreply@bytoid.com") is True  # blocked keyword ignored for bytoid


@pytest.mark.unit
def test_cannot_reply_noreply_keyword():
    """noreply in the local part must be blocked."""
    assert can_reply_to_email("noreply@company.com") is False


@pytest.mark.unit
def test_cannot_reply_notifications_keyword():
    """'notifications' in the address should be blocked."""
    assert can_reply_to_email("notifications@example.com") is False


@pytest.mark.unit
def test_cannot_reply_blocked_domain_google():
    """google.com is a blocked domain."""
    assert can_reply_to_email("alice@google.com") is False


@pytest.mark.unit
def test_cannot_reply_blocked_domain_microsoft():
    """microsoft.com is a blocked domain."""
    assert can_reply_to_email("user@microsoft.com") is False


@pytest.mark.unit
def test_cannot_reply_wrong_tld_org():
    """.org TLD is not in the allowed set {.com, .in, .ai}."""
    assert can_reply_to_email("alice@company.org") is False


@pytest.mark.unit
def test_cannot_reply_wrong_tld_net():
    """.net TLD is not allowed."""
    assert can_reply_to_email("alice@example.net") is False


@pytest.mark.unit
def test_can_reply_dot_in_tld():
    """.in TLD is allowed."""
    assert can_reply_to_email("hello@startup.in") is True


@pytest.mark.unit
def test_can_reply_dot_ai_tld():
    """.ai TLD is allowed."""
    assert can_reply_to_email("contact@product.ai") is True


@pytest.mark.unit
def test_cannot_reply_empty_string():
    """Empty string must return False."""
    assert can_reply_to_email("") is False


@pytest.mark.unit
def test_cannot_reply_not_an_email():
    """Strings without an @ should return False."""
    assert can_reply_to_email("notanemail") is False


@pytest.mark.unit
def test_cannot_reply_missing_tld():
    """Address with no TLD dot should return False."""
    assert can_reply_to_email("user@nodomain") is False


# ===========================================================================
# sanitize_value
# ===========================================================================


@pytest.mark.unit
def test_sanitize_value_escapes_html_string():
    """HTML special characters in strings must be escaped."""
    result = sanitize_value("<script>alert(1)</script>")
    result_str = str(result)
    assert "<script>" not in result_str
    assert "&lt;script&gt;" in result_str


@pytest.mark.unit
def test_sanitize_value_passthrough_int():
    """Non-string primitives (int) must be returned unchanged."""
    assert sanitize_value(42) == 42


@pytest.mark.unit
def test_sanitize_value_passthrough_none():
    """None must pass through unchanged."""
    assert sanitize_value(None) is None


@pytest.mark.unit
def test_sanitize_value_list_escapes_elements():
    """Each string element inside a list must be escaped."""
    result = sanitize_value(["<b>bold</b>", "ok"])
    result_strs = [str(r) for r in result]
    assert "&lt;b&gt;" in result_strs[0]
    assert result_strs[1] == "ok"


@pytest.mark.unit
def test_sanitize_value_dict_escapes_values():
    """String values inside a dict must be escaped; keys are unchanged."""
    result = sanitize_value({"key": "<img onerror=alert(1)>"})
    assert "&lt;" in str(result["key"])


@pytest.mark.unit
def test_sanitize_value_nested_structure():
    """Nested list-of-dicts must have all string values escaped."""
    result = sanitize_value([{"x": "<b>"}])
    assert "&lt;b&gt;" in str(result[0]["x"])


# ===========================================================================
# strip_html
# ===========================================================================


@pytest.mark.unit
def test_strip_html_removes_tags():
    """HTML tags must be removed, leaving plain text."""
    assert strip_html("<p>hello</p>") == "hello"


@pytest.mark.unit
def test_strip_html_passthrough_non_string():
    """Integers must pass through unchanged."""
    assert strip_html(99) == 99


@pytest.mark.unit
def test_strip_html_list():
    """strip_html must recurse into lists."""
    result = strip_html(["<b>bold</b>", "plain"])
    assert result == ["bold", "plain"]


@pytest.mark.unit
def test_strip_html_dict():
    """strip_html must recurse into dict values."""
    result = strip_html({"a": "<em>em</em>", "b": "text"})
    assert result == {"a": "em", "b": "text"}


@pytest.mark.unit
def test_strip_html_complex_tag():
    """Attributes inside tags must also be stripped."""
    assert strip_html('<a href="https://example.com">link</a>') == "link"
