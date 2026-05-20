"""Tests for reconcile_statement_ids — the statement ID stability guarantee.

This is the linchpin of Requirement 3: statement IDs must survive LLM edits
so that tracker mappings remain valid. Every branch of the reconciliation
logic has an explicit test case.
"""

from policy_hub.structured import Statement, reconcile_statement_ids


def _stmt(id_, text, seq=1, section_id="policy.statements"):
    return Statement(id=id_, text=text, seq=seq, section_id=section_id)


def _html(items: list[tuple[str | None, str]]) -> str:
    """Build an HTML <ul> from (statement_id_or_None, text) pairs."""
    lis = []
    for sid, text in items:
        attr = f' data-statement-id="{sid}"' if sid else ""
        lis.append(f"<li{attr}>{text}</li>")
    return "<ul>" + "".join(lis) + "</ul>"


OLD = [
    _stmt("stmt-001", "All users must authenticate with MFA.", seq=1),
    _stmt("stmt-002", "Privileged access must be reviewed quarterly.", seq=2),
    _stmt("stmt-003", "Shared accounts are prohibited.", seq=3),
]


class TestLlmPreservesIds:
    """LLM kept data-statement-id attributes unchanged."""

    def test_all_ids_preserved(self):
        new_html = _html(
            [
                ("stmt-001", "All users must authenticate with MFA."),
                ("stmt-002", "Privileged access must be reviewed quarterly."),
                ("stmt-003", "Shared accounts are prohibited."),
            ]
        )
        active, superseded = reconcile_statement_ids(OLD, new_html, "policy.statements")
        active_ids = {s.id for s in active}
        assert active_ids == {"stmt-001", "stmt-002", "stmt-003"}
        assert superseded == []

    def test_updated_text_with_preserved_id(self):
        new_html = _html(
            [
                ("stmt-001", "All users must authenticate with MFA or hardware key."),
                ("stmt-002", "Privileged access must be reviewed quarterly."),
                ("stmt-003", "Shared accounts are prohibited."),
            ]
        )
        active, superseded = reconcile_statement_ids(OLD, new_html, "policy.statements")
        mfa_stmt = next(s for s in active if s.id == "stmt-001")
        assert "hardware key" in mfa_stmt.text
        assert superseded == []


class TestLlmDropsAttributes:
    """LLM removed data-statement-id; recovered via Jaccard similarity."""

    def test_identical_text_recovered(self):
        new_html = _html(
            [
                (None, "All users must authenticate with MFA."),
                (None, "Privileged access must be reviewed quarterly."),
                (None, "Shared accounts are prohibited."),
            ]
        )
        # With high similarity (identical text), all should be recovered
        active, superseded = reconcile_statement_ids(
            OLD, new_html, "policy.statements", similarity_threshold=0.7
        )
        active_ids = {s.id for s in active}
        assert "stmt-001" in active_ids
        assert "stmt-002" in active_ids
        assert "stmt-003" in active_ids

    def test_minor_rewording_recovered(self):
        new_html = _html(
            [
                (None, "All users must authenticate using MFA."),  # "with" → "using"
                ("stmt-002", "Privileged access must be reviewed quarterly."),
                ("stmt-003", "Shared accounts are prohibited."),
            ]
        )
        active, _ = reconcile_statement_ids(
            OLD, new_html, "policy.statements", similarity_threshold=0.5
        )
        assert "stmt-001" in {s.id for s in active}

    def test_completely_different_text_gets_new_id(self):
        new_html = _html(
            [
                ("stmt-001", "All users must authenticate with MFA."),
                ("stmt-002", "Privileged access must be reviewed quarterly."),
                (None, "Encryption must be applied to all data at rest."),
            ]
        )
        active, superseded = reconcile_statement_ids(
            OLD, new_html, "policy.statements", similarity_threshold=0.85
        )
        # stmt-003 (shared accounts) is gone — superseded
        assert "stmt-003" in {s.id for s in superseded}
        # The new encryption statement should get a new UUID
        new_stmt = next(
            s for s in active if "Encryption" in s.text
        )
        assert new_stmt.id not in {"stmt-001", "stmt-002", "stmt-003"}
        assert len(new_stmt.id) > 0


class TestLlmSplitsStatement:
    """LLM splits one statement into two."""

    def test_first_part_keeps_original_id_second_gets_new(self):
        # stmt-001 split into two; LLM keeps the id on the first
        new_html = _html(
            [
                ("stmt-001", "All users must authenticate with MFA."),
                (None, "Service accounts must use certificate-based authentication."),
                ("stmt-002", "Privileged access must be reviewed quarterly."),
                ("stmt-003", "Shared accounts are prohibited."),
            ]
        )
        active, superseded = reconcile_statement_ids(OLD, new_html, "policy.statements")
        active_ids = {s.id for s in active}
        assert "stmt-001" in active_ids
        # The new part got a fresh UUID
        assert len(active) == 4
        new_ids = active_ids - {"stmt-001", "stmt-002", "stmt-003"}
        assert len(new_ids) == 1
        assert superseded == []


class TestLlmMergesStatements:
    """LLM merges two statements into one."""

    def test_merged_statement_keeps_one_id_other_superseded(self):
        # stmt-001 and stmt-003 merged; LLM keeps stmt-001's id
        new_html = _html(
            [
                (
                    "stmt-001",
                    "All users must authenticate with MFA; shared accounts are prohibited.",
                ),
                ("stmt-002", "Privileged access must be reviewed quarterly."),
            ]
        )
        active, superseded = reconcile_statement_ids(OLD, new_html, "policy.statements")
        active_ids = {s.id for s in active}
        assert "stmt-001" in active_ids
        assert "stmt-002" in active_ids
        # stmt-003 not in new output → superseded
        assert "stmt-003" in {s.id for s in superseded}


class TestLlmDeletesStatement:
    """LLM removes a statement entirely."""

    def test_deleted_statement_is_superseded(self):
        new_html = _html(
            [
                ("stmt-001", "All users must authenticate with MFA."),
                # stmt-002 removed
                ("stmt-003", "Shared accounts are prohibited."),
            ]
        )
        active, superseded = reconcile_statement_ids(OLD, new_html, "policy.statements")
        assert "stmt-002" in {s.id for s in superseded}
        assert "stmt-002" not in {s.id for s in active}

    def test_superseded_status_is_set(self):
        new_html = _html(
            [
                ("stmt-001", "All users must authenticate with MFA."),
                ("stmt-003", "Shared accounts are prohibited."),
            ]
        )
        _, superseded = reconcile_statement_ids(OLD, new_html, "policy.statements")
        assert all(s.status == "superseded" for s in superseded)

    def test_all_statements_deleted(self):
        active, superseded = reconcile_statement_ids(OLD, "<ul></ul>", "policy.statements")
        assert active == []
        assert len(superseded) == 3
        assert {s.id for s in superseded} == {"stmt-001", "stmt-002", "stmt-003"}


class TestEdgeCases:
    def test_empty_old_statements_all_new(self):
        new_html = _html([(None, "New statement.")])
        active, superseded = reconcile_statement_ids([], new_html, "policy.statements")
        assert len(active) == 1
        assert superseded == []

    def test_empty_new_html_supersedes_all(self):
        active, superseded = reconcile_statement_ids(
            OLD, "<ul></ul>", "policy.statements"
        )
        assert active == []
        assert len(superseded) == len(OLD)

    def test_seq_reflects_new_order(self):
        new_html = _html(
            [
                ("stmt-003", "Shared accounts are prohibited."),
                ("stmt-001", "All users must authenticate with MFA."),
                ("stmt-002", "Privileged access must be reviewed quarterly."),
            ]
        )
        active, _ = reconcile_statement_ids(OLD, new_html, "policy.statements")
        by_id = {s.id: s.seq for s in active}
        assert by_id["stmt-003"] == 1
        assert by_id["stmt-001"] == 2
        assert by_id["stmt-002"] == 3

    def test_section_id_propagated(self):
        new_html = _html([("stmt-001", "All users must authenticate with MFA.")])
        active, _ = reconcile_statement_ids(
            [OLD[0]], new_html, "policy.statements"
        )
        assert all(s.section_id == "policy.statements" for s in active)
