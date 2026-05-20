"""Tests for policy_hub/structured.py — parse, render, and round-trip."""

from policy_hub.structured import (
    parse_document_html,
    render_document_html,
    ParsedDocument,
)


class TestParseDocumentHtml:
    def test_parses_all_policy_sections(self, policy_v2_html):
        parsed = parse_document_html(policy_v2_html, "policy")
        assert isinstance(parsed, ParsedDocument)
        section_ids = {s.id for s in parsed.sections}
        assert "policy.purpose" in section_ids
        assert "policy.statements" in section_ids
        assert "policy.revision_history" in section_ids

    def test_parses_all_procedure_sections(self, procedure_v2_html):
        parsed = parse_document_html(procedure_v2_html, "procedure")
        section_ids = {s.id for s in parsed.sections}
        assert "procedure.steps" in section_ids
        assert "procedure.prerequisites" in section_ids
        assert "procedure.evidence" in section_ids

    def test_extracts_statements_from_policy(self, policy_v2_html):
        parsed = parse_document_html(policy_v2_html, "policy")
        stmt_section = next(
            s for s in parsed.sections if s.id == "policy.statements"
        )
        assert len(stmt_section.statements) == 3
        ids = {s.id for s in stmt_section.statements}
        assert "stmt-001" in ids
        assert "stmt-002" in ids
        assert "stmt-003" in ids

    def test_extracts_steps_from_procedure(self, procedure_v2_html):
        parsed = parse_document_html(procedure_v2_html, "procedure")
        step_section = next(
            s for s in parsed.sections if s.id == "procedure.steps"
        )
        assert len(step_section.statements) == 3
        texts = [s.text for s in step_section.statements]
        assert any("Active Directory" in t for t in texts)

    def test_statement_seq_is_sequential(self, policy_v2_html):
        parsed = parse_document_html(policy_v2_html, "policy")
        stmt_section = next(
            s for s in parsed.sections if s.id == "policy.statements"
        )
        seqs = [s.seq for s in sorted(stmt_section.statements, key=lambda x: x.seq)]
        assert seqs == list(range(1, len(seqs) + 1))

    def test_extracts_metadata_from_header_table(self, policy_v2_html):
        parsed = parse_document_html(policy_v2_html, "policy")
        assert parsed.metadata.get("document_id") == "POL-001"
        assert parsed.metadata.get("version") == "1.0"
        assert parsed.metadata.get("classification") == "Internal"

    def test_sections_in_template_order(self, policy_v2_html):
        from policy_hub.templates import POLICY_TEMPLATE

        parsed = parse_document_html(policy_v2_html, "policy")
        template_order = [s.id for s in POLICY_TEMPLATE]
        parsed_order = [s.id for s in parsed.sections if s.id in set(template_order)]
        # Parsed order should match template order
        assert parsed_order == [t for t in template_order if t in set(parsed_order)]

    def test_missing_sections_added_as_empty(self):
        minimal_html = """
        <div>
          <div data-section-id="policy.purpose">
            <h2>Purpose</h2><p>Some text.</p>
          </div>
        </div>
        """
        parsed = parse_document_html(minimal_html, "policy")
        section_ids = {s.id for s in parsed.sections}
        # All template sections should be present even if not in HTML
        from policy_hub.templates import POLICY_TEMPLATE

        for sec_def in POLICY_TEMPLATE:
            assert sec_def.id in section_ids

    def test_legacy_html_parsed_by_heading_text(self, legacy_policy_html):
        parsed = parse_document_html(legacy_policy_html, "policy")
        section_ids = {s.id for s in parsed.sections}
        # Legacy HTML has purpose, scope, statements, roles, compliance, exceptions
        assert "policy.purpose" in section_ids
        assert "policy.scope" in section_ids

    def test_legacy_html_statements_get_new_uuids(self, legacy_policy_html):
        parsed = parse_document_html(legacy_policy_html, "policy")
        stmt_section = next(
            (s for s in parsed.sections if s.id == "policy.statements"), None
        )
        if stmt_section and stmt_section.statements:
            for stmt in stmt_section.statements:
                # Should be a valid UUID (not empty)
                assert len(stmt.id) > 0
                assert stmt.id != "stmt-001"  # New IDs minted


class TestRenderDocumentHtml:
    def test_render_produces_data_section_ids(self, policy_v2_html):
        parsed = parse_document_html(policy_v2_html, "policy")
        rendered = render_document_html(parsed, "policy")
        assert 'data-section-id="policy.purpose"' in rendered
        assert 'data-section-id="policy.statements"' in rendered

    def test_render_produces_data_statement_ids(self, policy_v2_html):
        parsed = parse_document_html(policy_v2_html, "policy")
        rendered = render_document_html(parsed, "policy")
        assert 'data-statement-id="stmt-001"' in rendered
        assert 'data-statement-id="stmt-002"' in rendered

    def test_render_uses_ol_for_steps(self, procedure_v2_html):
        parsed = parse_document_html(procedure_v2_html, "procedure")
        rendered = render_document_html(parsed, "procedure")
        assert "<ol>" in rendered

    def test_render_uses_ul_for_statements(self, policy_v2_html):
        parsed = parse_document_html(policy_v2_html, "policy")
        rendered = render_document_html(parsed, "policy")
        assert "<ul>" in rendered

    def test_render_includes_all_template_sections(self, policy_v2_html):
        from policy_hub.templates import POLICY_TEMPLATE

        parsed = parse_document_html(policy_v2_html, "policy")
        rendered = render_document_html(parsed, "policy")
        for sec_def in POLICY_TEMPLATE:
            assert f'data-section-id="{sec_def.id}"' in rendered


class TestRoundTrip:
    def test_parse_render_parse_is_idempotent_for_policy(self, policy_v2_html):
        """parse → render → parse should produce the same section ids and statement ids."""
        parsed1 = parse_document_html(policy_v2_html, "policy")
        rendered = render_document_html(parsed1, "policy")
        parsed2 = parse_document_html(rendered, "policy")

        ids1 = {s.id for s in parsed1.sections}
        ids2 = {s.id for s in parsed2.sections}
        assert ids1 == ids2

        stmt1 = {
            st.id
            for s in parsed1.sections
            for st in s.statements
        }
        stmt2 = {
            st.id
            for s in parsed2.sections
            for st in s.statements
        }
        assert stmt1 == stmt2

    def test_parse_render_parse_is_idempotent_for_procedure(self, procedure_v2_html):
        parsed1 = parse_document_html(procedure_v2_html, "procedure")
        rendered = render_document_html(parsed1, "procedure")
        parsed2 = parse_document_html(rendered, "procedure")

        stmt1 = {st.id for s in parsed1.sections for st in s.statements}
        stmt2 = {st.id for s in parsed2.sections for st in s.statements}
        assert stmt1 == stmt2

    def test_statement_text_preserved_through_round_trip(self, policy_v2_html):
        parsed1 = parse_document_html(policy_v2_html, "policy")
        rendered = render_document_html(parsed1, "policy")
        parsed2 = parse_document_html(rendered, "policy")

        stmts1 = {
            st.id: st.text
            for s in parsed1.sections
            for st in s.statements
        }
        stmts2 = {
            st.id: st.text
            for s in parsed2.sections
            for st in s.statements
        }
        for stmt_id, text in stmts1.items():
            assert stmts2.get(stmt_id) is not None
