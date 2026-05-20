"""Unit tests for policy_hub.migrate_legacy_policies pure helpers.

Covers:
  - _migration_prompt builds a prompt with the right structure for known/unknown doc_types
  - _render_sections_to_html produces valid V2-style HTML usable by validate()
  - The full render → validate roundtrip succeeds for a fully-populated structured doc
"""

import pytest


@pytest.fixture
def migrate_mod():
    from policy_hub import migrate_legacy_policies
    return migrate_legacy_policies


def test_migration_prompt_contains_schema_and_html(migrate_mod):
    prompt = migrate_mod._migration_prompt("<p>Some legacy policy.</p>", "policy")
    assert "TARGET SCHEMA" in prompt
    assert "TEMPLATE SECTIONS" in prompt
    assert "LEGACY DOCUMENT HTML" in prompt
    assert "<p>Some legacy policy.</p>" in prompt
    # Schema example mentions key V2 fields
    assert "template_version" in prompt
    assert "data-statement-id" not in prompt  # schema uses statement.id, not html attr in schema
    assert "Return ONLY valid JSON" in prompt


def test_migration_prompt_with_policy_template_includes_real_section_ids(migrate_mod):
    prompt = migrate_mod._migration_prompt("<p>x</p>", "policy")
    # Real policy template section ids should appear in the prompt
    assert "policy.purpose" in prompt
    assert "policy.statements" in prompt


def test_migration_prompt_with_procedure_template_includes_real_section_ids(migrate_mod):
    prompt = migrate_mod._migration_prompt("<p>x</p>", "procedure")
    assert "procedure.steps" in prompt


def test_migration_prompt_unknown_doc_type_falls_back_gracefully(migrate_mod):
    prompt = migrate_mod._migration_prompt("<p>x</p>", "totally-not-a-real-type")
    assert "(unknown template)" in prompt


def test_migration_prompt_truncates_long_html(migrate_mod):
    # Build an HTML blob larger than 80,000 chars
    huge_html = "<p>" + ("x" * 100_000) + "</p>"
    prompt = migrate_mod._migration_prompt(huge_html, "policy")
    # The legacy HTML segment is bounded at 80000 chars
    body_index = prompt.index("LEGACY DOCUMENT HTML:")
    json_index = prompt.index("\nJSON:")
    legacy_segment = prompt[body_index:json_index]
    # The included slice should not exceed 80000 chars plus the small header line
    assert len(legacy_segment) < 80_500


def test_render_sections_to_html_emits_section_id_attribute(migrate_mod):
    sections = [
        {
            "id": "policy.purpose",
            "title": "Purpose",
            "body_html": "<p>Define access controls.</p>",
        }
    ]
    html = migrate_mod._render_sections_to_html(sections)
    assert 'data-section-id="policy.purpose"' in html
    assert "<h2>Purpose</h2>" in html
    assert "Define access controls" in html


def test_render_sections_to_html_emits_statement_ids(migrate_mod):
    sections = [
        {
            "id": "policy.statements",
            "title": "Policy Statements",
            "statements": [
                {"id": "stmt-1", "text": "All users must use MFA."},
                {"id": "stmt-2", "text": "Privileged access is reviewed quarterly."},
            ],
        }
    ]
    html = migrate_mod._render_sections_to_html(sections)
    assert 'data-statement-id="stmt-1"' in html
    assert 'data-statement-id="stmt-2"' in html
    assert "All users must use MFA." in html


def test_render_sections_handles_statement_without_id(migrate_mod):
    """Missing statement id renders without a data-statement-id attribute."""
    sections = [
        {
            "id": "policy.statements",
            "title": "Policy Statements",
            "statements": [{"text": "Bare statement."}],
        }
    ]
    html = migrate_mod._render_sections_to_html(sections)
    assert "Bare statement." in html
    # No empty data-statement-id attr
    assert 'data-statement-id=""' not in html


def test_render_sections_emits_body_html_for_text_sections(migrate_mod):
    sections = [
        {
            "id": "policy.scope",
            "title": "Scope",
            "body_html": "<p>Applies to all employees.</p>",
        },
    ]
    html = migrate_mod._render_sections_to_html(sections)
    assert "<p>Applies to all employees.</p>" in html


def test_render_then_validate_passes_for_complete_policy(migrate_mod):
    """A fully-populated structured doc renders to HTML that template.validate() accepts."""
    from policy_hub.templates import validate, POLICY_TEMPLATE

    sections = []
    for section in POLICY_TEMPLATE:
        if section.kind == "statements":
            sections.append({
                "id": section.id,
                "title": section.title,
                "statements": [
                    {"id": f"stmt-{section.id}-1", "text": "Sample statement one."},
                    {"id": f"stmt-{section.id}-2", "text": "Sample statement two."},
                ],
            })
        else:
            sections.append({
                "id": section.id,
                "title": section.title,
                "body_html": f"<p>Content for {section.title}.</p>",
            })

    html = migrate_mod._render_sections_to_html(sections)
    result = validate(html, "policy")
    assert result.ok, (
        f"validation failed: missing={result.missing_sections} "
        f"empty={result.empty_required} no_ids={result.statements_missing_ids}"
    )
    assert result.statements_missing_ids == 0
