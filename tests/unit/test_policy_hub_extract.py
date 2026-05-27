"""Unit tests for policy_hub/extract.py.

Tests pure-logic helpers (paragraph splitting, HTML sanitization) and
extract_any dispatcher; heavy PDF/DOCX parsing libs are stubbed.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

sys.modules.setdefault("utils.base_logger",
                      MagicMock(get_logger=MagicMock(return_value=MagicMock())))

# bs4 and policy_hub.extract must be real (not MagicMock stubs from other test files)
for _to_pop in ("policy_hub.extract", "bs4", "bs4.element"):
    sys.modules.pop(_to_pop, None)

import policy_hub.extract as ext  # noqa: E402


# ── _paragraphs_from_text ────────────────────────────────────────────────────

@pytest.mark.unit
def test_paragraphs_from_empty_text():
    assert ext._paragraphs_from_text("") == []

@pytest.mark.unit
def test_paragraphs_from_whitespace():
    assert ext._paragraphs_from_text("   \n\n  ") == []

@pytest.mark.unit
def test_paragraphs_single():
    out = ext._paragraphs_from_text("Hello world")
    assert out == ["<p>Hello world</p>"]

@pytest.mark.unit
def test_paragraphs_multiple():
    out = ext._paragraphs_from_text("First\n\nSecond\n\nThird")
    assert out == ["<p>First</p>", "<p>Second</p>", "<p>Third</p>"]

@pytest.mark.unit
def test_paragraphs_collapse_internal_newlines():
    out = ext._paragraphs_from_text("Line one\nline two\nline three")
    assert out == ["<p>Line one line two line three</p>"]

@pytest.mark.unit
def test_paragraphs_escapes_html_in_text():
    out = ext._paragraphs_from_text("<script>alert(1)</script>")
    assert "&lt;script&gt;" in out[0]
    assert "<script>" not in out[0]

@pytest.mark.unit
@pytest.mark.parametrize("special_char", ["<", ">", "&", "'", '"'])
def test_paragraphs_escapes_special_chars(special_char):
    out = ext._paragraphs_from_text(f"foo{special_char}bar")
    assert "<p>" in out[0]
    # The raw char (other than allowed) should be escaped
    if special_char in ("<", ">"):
        assert special_char not in out[0].replace("<p>", "").replace("</p>", "")


# ── extract_html ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_extract_html_simple():
    html = b"<html><body><p>Hello</p></body></html>"
    out = ext.extract_html(html)
    assert "<p>Hello</p>" in out
    assert 'data-source="upload-html"' in out

@pytest.mark.unit
def test_extract_html_strips_script():
    html = b"<html><body><script>evil()</script><p>safe</p></body></html>"
    out = ext.extract_html(html)
    assert "<script>" not in out
    assert "evil()" not in out
    assert "<p>safe</p>" in out

@pytest.mark.unit
def test_extract_html_strips_style():
    html = b"<html><body><style>body{color:red}</style><p>safe</p></body></html>"
    out = ext.extract_html(html)
    assert "<style>" not in out

@pytest.mark.unit
@pytest.mark.parametrize("dangerous", ["script", "iframe", "form", "object", "embed", "meta", "link"])
def test_extract_html_strips_dangerous_tags(dangerous):
    html = f"<html><body><{dangerous}>bad</{dangerous}><p>good</p></body></html>".encode()
    out = ext.extract_html(html)
    assert f"<{dangerous}>" not in out
    assert "<p>good</p>" in out

@pytest.mark.unit
def test_extract_html_strips_onclick():
    html = b'<html><body><div onclick="evil()">content</div></body></html>'
    out = ext.extract_html(html)
    assert "onclick" not in out
    assert "content" in out

@pytest.mark.unit
@pytest.mark.parametrize("evt", ["onload", "onmouseover", "onerror", "ondblclick"])
def test_extract_html_strips_event_handlers(evt):
    html = f'<html><body><div {evt}="evil()">x</div></body></html>'.encode()
    out = ext.extract_html(html)
    assert evt not in out.lower()

@pytest.mark.unit
def test_extract_html_strips_javascript_href():
    html = b'<html><body><a href="javascript:alert(1)">click</a></body></html>'
    out = ext.extract_html(html)
    assert "javascript:" not in out

@pytest.mark.unit
def test_extract_html_strips_javascript_src():
    html = b'<html><body><img src="javascript:alert(1)"></body></html>'
    out = ext.extract_html(html)
    assert "javascript:" not in out

@pytest.mark.unit
def test_extract_html_empty_returns_empty():
    out = ext.extract_html(b"")
    assert out == ""

@pytest.mark.unit
def test_extract_html_whitespace_only_returns_empty():
    out = ext.extract_html(b"  \n\n  ")
    assert out == ""

@pytest.mark.unit
def test_extract_html_preserves_paragraph_structure():
    html = b"<html><body><h1>Title</h1><p>Para</p></body></html>"
    out = ext.extract_html(html)
    assert "<h1>" in out
    assert "<p>Para</p>" in out


# ── extract_any dispatcher ───────────────────────────────────────────────────

@pytest.mark.unit
def test_extract_any_empty_bytes_returns_empty():
    assert ext.extract_any(b"", "x.pdf") == ""

@pytest.mark.unit
def test_extract_any_unknown_extension_raises():
    with pytest.raises(ValueError, match="Unsupported extension"):
        ext.extract_any(b"x", "file.txt")

@pytest.mark.unit
@pytest.mark.parametrize("filename", ["doc.pdf", "DOC.PDF", "/path/to/x.pdf"])
def test_extract_any_pdf_dispatch(filename):
    with patch("policy_hub.extract.extract_pdf_text", return_value="<p>x</p>") as m:
        out = ext.extract_any(b"PDFCONTENT", filename)
        m.assert_called_once_with(b"PDFCONTENT")
    assert out == "<p>x</p>"

@pytest.mark.unit
@pytest.mark.parametrize("filename", ["doc.docx", "DOC.DOCX"])
def test_extract_any_docx_dispatch(filename):
    with patch("policy_hub.extract.extract_docx_text", return_value="<p>x</p>") as m:
        out = ext.extract_any(b"DOCX", filename)
        m.assert_called_once_with(b"DOCX")
    assert out == "<p>x</p>"

@pytest.mark.unit
@pytest.mark.parametrize("filename", ["doc.html", "doc.htm", "DOC.HTML"])
def test_extract_any_html_dispatch(filename):
    out = ext.extract_any(b"<html><body><p>x</p></body></html>", filename)
    assert "<p>x</p>" in out


# ── _DANGEROUS_TAGS ──────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("tag", ["script", "style", "iframe", "form", "object", "embed", "meta", "link"])
def test_dangerous_tags_includes(tag):
    assert tag in ext._DANGEROUS_TAGS

@pytest.mark.unit
def test_dangerous_tags_is_set():
    assert isinstance(ext._DANGEROUS_TAGS, set)
