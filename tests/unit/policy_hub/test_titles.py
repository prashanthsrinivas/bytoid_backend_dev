"""Unit tests for policy_hub.titles (extract_title + looks_like_uuid)."""

import pytest

from policy_hub.titles import extract_title, looks_like_uuid

_UUID = "8d7c93d2-03db-4e14-a9a5-60ccd467304b"


@pytest.mark.unit
class TestLooksLikeUuid:
    def test_bare_uuid(self):
        assert looks_like_uuid(_UUID) is True

    def test_uppercase_uuid(self):
        assert looks_like_uuid(_UUID.upper()) is True

    def test_uuid_with_whitespace(self):
        assert looks_like_uuid(f"  {_UUID}  ") is True

    def test_normal_title(self):
        assert looks_like_uuid("Access Control Policy") is False

    def test_empty(self):
        assert looks_like_uuid("") is False

    def test_none(self):
        assert looks_like_uuid(None) is False


@pytest.mark.unit
class TestExtractTitle:
    def test_h1_wins(self):
        html = "<h1>Access Control Policy</h1><p>body</p>"
        assert extract_title(html, fallback="fb") == "Access Control Policy"

    def test_h1_with_nested_tags_stripped(self):
        html = "<h1><span>Encryption</span> Standard</h1>"
        assert extract_title(html, fallback="fb") == "Encryption Standard"

    def test_markdown_heading_fallback(self):
        content = "# Incident Response Procedure\n\nSome text"
        assert extract_title(content, fallback="fb") == "Incident Response Procedure"

    def test_uuid_h1_is_rejected_falls_to_fallback(self):
        html = f"<h1>{_UUID}</h1>"
        assert extract_title(html, fallback="Real Title") == "Real Title"

    def test_empty_h1_falls_through(self):
        html = "<h1>   </h1>\n# Markdown Title"
        assert extract_title(html, fallback="fb") == "Markdown Title"

    def test_uuid_fallback_is_rejected(self):
        # No h1/markdown, and the fallback itself is a UUID → Untitled.
        assert extract_title("<p>no heading</p>", fallback=_UUID, doc_type="standard") == "Untitled standard"

    def test_empty_everything_uses_doc_type(self):
        assert extract_title("", fallback="", doc_type="procedure") == "Untitled procedure"

    def test_default_doc_type_is_policy(self):
        assert extract_title("", fallback="") == "Untitled policy"

    def test_fallback_used_when_no_heading(self):
        assert extract_title("<p>body only</p>", fallback="Backup Policy") == "Backup Policy"

    def test_none_content_does_not_crash(self):
        assert extract_title(None, fallback="Backup Policy") == "Backup Policy"
