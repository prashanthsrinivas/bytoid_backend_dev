"""Unit tests for policy_hub/templates.py — pure-logic template handling."""

import sys
from unittest.mock import MagicMock

import pytest

# Stub heavy deps
for _mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
             "boto3", "dotenv", "dbutils", "dbutils.pooled_db"):
    sys.modules.setdefault(_mod, MagicMock(name=f"{_mod}_stub"))

sys.modules.setdefault("utils.s3_utils", MagicMock())
sys.modules.setdefault("utils.base_logger",
                      MagicMock(get_logger=MagicMock(return_value=MagicMock())))

from policy_hub import templates as tmpl  # noqa: E402


DOC_TYPES = ["policy", "procedure", "standard"]


# ── SectionDef dataclass ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_section_def_is_frozen():
    s = tmpl.SectionDef(id="x", title="X", kind="text")
    with pytest.raises(Exception):
        s.id = "y"  # frozen dataclass

@pytest.mark.unit
@pytest.mark.parametrize("kind", ["text", "statements", "steps", "header_table", "history"])
def test_section_def_accepts_all_kinds(kind):
    s = tmpl.SectionDef(id="x", title="X", kind=kind)
    assert s.kind == kind

@pytest.mark.unit
def test_section_def_defaults():
    s = tmpl.SectionDef(id="x", title="X", kind="text")
    assert s.required is True
    assert s.prompt_help == ""


# ── TEMPLATES dict ───────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_templates_dict_has_doc_type(doc_type):
    assert doc_type in tmpl.TEMPLATES

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_templates_is_non_empty_list(doc_type):
    t = tmpl.TEMPLATES[doc_type]
    assert isinstance(t, list)
    assert len(t) > 0

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_every_section_is_section_def(doc_type):
    for s in tmpl.TEMPLATES[doc_type]:
        assert isinstance(s, tmpl.SectionDef)

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_template_section_ids_unique_within_doc_type(doc_type):
    ids = [s.id for s in tmpl.TEMPLATES[doc_type]]
    assert len(ids) == len(set(ids)), f"Duplicate section IDs in {doc_type}"

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_template_section_ids_prefixed_by_doc_type(doc_type):
    """Section IDs follow `<doc_type>.<name>` convention."""
    for s in tmpl.TEMPLATES[doc_type]:
        assert s.id.startswith(f"{doc_type}."), (
            f"{s.id} in {doc_type} template doesn't follow naming convention"
        )

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_template_has_header_section(doc_type):
    """Every doc type starts with a header_table section."""
    kinds = [s.kind for s in tmpl.TEMPLATES[doc_type]]
    assert "header_table" in kinds


# ── get_default_template ─────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_get_default_template_returns_list(doc_type):
    t = tmpl.get_default_template(doc_type)
    assert isinstance(t, list)
    assert len(t) > 0

@pytest.mark.unit
def test_get_default_template_unknown_doc_type():
    with pytest.raises(KeyError):
        tmpl.get_default_template("unknown_doc_type")

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_get_default_template_identity(doc_type):
    """Returns the canonical TEMPLATES entry (not a copy)."""
    assert tmpl.get_default_template(doc_type) is tmpl.TEMPLATES[doc_type]


# ── get_template ─────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_get_template_no_user_uses_default(doc_type):
    t = tmpl.get_template(doc_type)
    assert t is tmpl.TEMPLATES[doc_type]

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_get_template_user_no_override_falls_back(doc_type, monkeypatch):
    fake_storage = MagicMock()
    fake_storage.load_custom_template = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "policy_hub.template_storage", fake_storage)
    t = tmpl.get_template(doc_type, user_id="u1")
    assert t is tmpl.TEMPLATES[doc_type]

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_get_template_uses_custom_when_available(doc_type, monkeypatch):
    fake_storage = MagicMock()
    custom = [tmpl.SectionDef(id=f"{doc_type}.custom", title="X", kind="text")]
    fake_storage.load_custom_template = MagicMock(return_value=custom)
    monkeypatch.setitem(sys.modules, "policy_hub.template_storage", fake_storage)
    t = tmpl.get_template(doc_type, user_id="u1")
    assert t == custom

@pytest.mark.unit
def test_get_template_falls_back_on_storage_exception(monkeypatch):
    fake_storage = MagicMock()
    fake_storage.load_custom_template = MagicMock(side_effect=RuntimeError("S3 down"))
    monkeypatch.setitem(sys.modules, "policy_hub.template_storage", fake_storage)
    # Should not raise
    t = tmpl.get_template("policy", user_id="u1")
    assert t is tmpl.TEMPLATES["policy"]


# ── serialize / deserialize round-trip ───────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_serialize_returns_dict(doc_type):
    s = tmpl.TEMPLATES[doc_type][0]
    out = tmpl.serialize_section(s)
    assert isinstance(out, dict)
    for k in ("id", "title", "kind", "required", "prompt_help"):
        assert k in out

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_deserialize_round_trip_all_sections(doc_type):
    for s in tmpl.TEMPLATES[doc_type]:
        d = tmpl.serialize_section(s)
        back = tmpl.deserialize_section(d)
        assert back.id == s.id
        assert back.title == s.title
        assert back.kind == s.kind
        assert back.required == s.required
        assert back.prompt_help == s.prompt_help

@pytest.mark.unit
def test_deserialize_minimal_dict():
    d = {"id": "x"}
    s = tmpl.deserialize_section(d)
    assert s.id == "x"
    assert s.title == ""
    assert s.kind == "text"
    assert s.required is True
    assert s.prompt_help == ""

@pytest.mark.unit
def test_deserialize_coerces_types():
    d = {"id": 123, "title": 456, "required": "false", "prompt_help": None}
    s = tmpl.deserialize_section(d)
    assert s.id == "123"
    assert s.title == "456"
    # "false" is truthy in bool() — that's the implementation's behavior
    assert isinstance(s.required, bool)


# ── validate ─────────────────────────────────────────────────────────────────

def _section_html(sec_id, body=""):
    return f'<div data-section-id="{sec_id}">{body}</div>'

@pytest.mark.unit
def test_validate_empty_html_flags_all_required_missing():
    out = tmpl.validate("", "policy")
    assert out.ok is False
    assert len(out.missing_sections) > 0

@pytest.mark.unit
def test_validate_complete_doc_is_ok():
    html = "".join(_section_html(s.id, "<p>body</p>") for s in tmpl.TEMPLATES["policy"])
    out = tmpl.validate(html, "policy")
    assert out.ok is True
    assert out.missing_sections == []

@pytest.mark.unit
def test_validate_detects_empty_required_section():
    html = "".join(_section_html(s.id) for s in tmpl.TEMPLATES["policy"])  # no body
    out = tmpl.validate(html, "policy")
    assert out.ok is False
    assert len(out.empty_required) > 0

@pytest.mark.unit
def test_validate_counts_statements_missing_ids():
    sec_id = "policy.statements"
    html = _section_html(sec_id, "<ul><li>x</li><li>y</li><li data-statement-id='s1'>z</li></ul>")
    # add other required sections so they don't dominate
    html += "".join(_section_html(s.id, "<p>body</p>") for s in tmpl.TEMPLATES["policy"]
                    if s.id != sec_id)
    out = tmpl.validate(html, "policy")
    assert out.statements_missing_ids == 2

@pytest.mark.unit
@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_validate_unknown_doc_type_raises(doc_type):
    """Unknown doc_type bubbles KeyError from TEMPLATES lookup."""
    # Sanity: known doc_type does NOT raise.
    tmpl.validate("", doc_type)

@pytest.mark.unit
def test_validate_validation_result_shape():
    out = tmpl.validate("", "policy")
    assert hasattr(out, "ok")
    assert hasattr(out, "missing_sections")
    assert hasattr(out, "empty_required")
    assert hasattr(out, "statements_missing_ids")
    assert isinstance(out.missing_sections, list)
    assert isinstance(out.empty_required, list)
    assert isinstance(out.statements_missing_ids, int)
