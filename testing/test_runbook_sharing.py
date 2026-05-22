"""
Tests for the runbook share-to-display flow.

Covers:
- runbook_get_result_by_id logic  (status filter, duplicate handling)
- _keep filter logic in result_list
- Security guard: known-entries must not leak un-shared results
- Regression guard: non-completed shared results must be visible

Tests are fully isolated — no live AWS, no LanceDB, no DB imports.
We test the pure logic extracted from the production functions so that
the testing/conftest.py sys.modules stubs don't interfere.

Run with:  python -m pytest testing/test_runbook_sharing.py -v
"""

# ── Constants ─────────────────────────────────────────────────────────────────

ADMIN_ID = "admin-001"
DEMO_ID = "demo-user"
RB_ID = "runbook_abc"
RES_ID = "result_xyz"
RES_ID_2 = "result_unshared"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_row(status, risk_score=46.0, runbook_id=RB_ID, result_id=RES_ID, ended_at=1_700_000_000):
    return {
        "result_id": result_id,
        "runbook_id": runbook_id,
        "user_id": ADMIN_ID,
        "status": status,
        "risk_score": float(risk_score),
        "ended_at": ended_at,
        "result": "{}",
    }


def _shared_reports(has_runbook_id=True, result_id=RES_ID):
    entry = {
        "type": "runbook",
        "mainuser_id": ADMIN_ID,
        "reportid": result_id,
        "name": "Test Report",
        "dateofaccess": "2026-01-01T00:00:00",
    }
    if has_runbook_id:
        entry["runbook_id"] = RB_ID
    return {result_id: entry}


# ── Pure logic mirror of runbook_get_result_by_id ────────────────────────────
# This replicates the exact logic from db/lance_db_service.py so we can test
# it without importing the class (conftest stubs out the db package).


FINAL_STATUSES = {"completed", "success", "done", "draft"}


def _runbook_get_result_by_id_logic(rows, result_id):
    """Pure logic equivalent to LanceDBServer.runbook_get_result_by_id.

    Takes the list of rows that the LanceDB query would return and applies
    the same selection rules as the production function.
    """
    if not rows:
        return None
    valid_rows = [r for r in rows if r.get("status") in FINAL_STATUSES]
    if not valid_rows:
        return None
    row = max(valid_rows, key=lambda r: r.get("ended_at") or 0)
    return row


# ── Unit tests: runbook_get_result_by_id logic ────────────────────────────────


def test_get_by_id_returns_completed_row():
    rows = [_make_row("completed", risk_score=46)]
    result = _runbook_get_result_by_id_logic(rows, RES_ID)
    assert result is not None
    assert result["status"] == "completed"
    assert result["result_id"] == RES_ID


def test_get_by_id_returns_draft_row():
    """Must surface draft results — regression for old status=='completed' filter."""
    rows = [_make_row("draft", risk_score=0)]
    result = _runbook_get_result_by_id_logic(rows, RES_ID)
    assert result is not None
    assert result["status"] == "draft"


def test_get_by_id_returns_success_row():
    rows = [_make_row("success", risk_score=30)]
    result = _runbook_get_result_by_id_logic(rows, RES_ID)
    assert result is not None
    assert result["status"] == "success"


def test_get_by_id_returns_done_row():
    rows = [_make_row("done", risk_score=20)]
    result = _runbook_get_result_by_id_logic(rows, RES_ID)
    assert result is not None
    assert result["status"] == "done"


def test_get_by_id_returns_none_for_running():
    """In-progress results must not be surfaced to shared users."""
    rows = [_make_row("running")]
    result = _runbook_get_result_by_id_logic(rows, RES_ID)
    assert result is None


def test_get_by_id_returns_none_for_failed():
    rows = [_make_row("failed")]
    result = _runbook_get_result_by_id_logic(rows, RES_ID)
    assert result is None


def test_get_by_id_returns_none_when_not_found():
    result = _runbook_get_result_by_id_logic([], RES_ID)
    assert result is None


def test_get_by_id_picks_most_recent_when_duplicates_exist():
    """LanceDB is append-only: result_id can have a running then completed row.
    The function must return the most recently finalized row."""
    older = _make_row("completed", risk_score=10, ended_at=1_000_000)
    newer = _make_row("completed", risk_score=46, ended_at=2_000_000)
    result = _runbook_get_result_by_id_logic([older, newer], RES_ID)
    assert result is not None
    assert result["ended_at"] == 2_000_000
    assert result["risk_score"] == 46.0


def test_get_by_id_skips_running_row_and_returns_completed_duplicate():
    """When both running and completed rows exist, return the completed one."""
    running_row = _make_row("running", ended_at=2_000_000)
    completed_row = _make_row("completed", risk_score=46, ended_at=1_000_000)
    result = _runbook_get_result_by_id_logic([running_row, completed_row], RES_ID)
    assert result is not None
    assert result["status"] == "completed"


# ── Unit tests: _keep filter ──────────────────────────────────────────────────
# Mirror the exact _keep logic from result_list so we can test edge cases.


def _make_keep_fn(shared_result_ids, user_id, runbook_ids):
    valid_statuses = {"completed", "success", "done", "draft"}

    def _keep(r):
        if not isinstance(r, dict):
            return False
        if r.get("status") not in valid_statuses:
            return False
        is_shared = r.get("result_id") in shared_result_ids
        is_owned = r.get("user_id") == user_id
        if not (is_shared or is_owned):
            return False
        # Owned-but-not-shared: require non-zero risk score (hide SU drafts)
        if is_owned and not is_shared and (r.get("risk_score") or 0) == 0:
            return False
        if is_owned and not is_shared and r.get("runbook_id") not in runbook_ids:
            return False
        return True

    return _keep


def test_keep_explicitly_shared_result_passes_regardless_of_risk_score():
    """Shared results must show even when risk_score=0 (draft sent for review)."""
    keep = _make_keep_fn(
        shared_result_ids={RES_ID},
        user_id=DEMO_ID,
        runbook_ids={RB_ID},
    )
    row = _make_row("completed", risk_score=0)
    row["user_id"] = ADMIN_ID  # not owned by DEMO_ID
    assert keep(row) is True


def test_keep_explicitly_shared_draft_passes():
    """A shared result with status=draft must also pass."""
    keep = _make_keep_fn(
        shared_result_ids={RES_ID},
        user_id=DEMO_ID,
        runbook_ids={RB_ID},
    )
    row = _make_row("draft", risk_score=0)
    row["user_id"] = ADMIN_ID
    assert keep(row) is True


def test_keep_owned_result_with_risk_score_zero_is_hidden():
    """SU-mode test/draft runs (risk_score=0, owned but never shared) must stay
    hidden so only explicitly assigned reports surface."""
    keep = _make_keep_fn(
        shared_result_ids=set(),  # NOT in share list
        user_id=DEMO_ID,
        runbook_ids={RB_ID},
    )
    row = _make_row("draft", risk_score=0)
    row["user_id"] = DEMO_ID  # owned
    assert keep(row) is False


def test_keep_owned_result_with_positive_risk_score_passes():
    keep = _make_keep_fn(
        shared_result_ids=set(),
        user_id=DEMO_ID,
        runbook_ids={RB_ID},
    )
    row = _make_row("completed", risk_score=42)
    row["user_id"] = DEMO_ID
    assert keep(row) is True


def test_keep_result_not_owned_and_not_shared_is_hidden():
    keep = _make_keep_fn(
        shared_result_ids=set(),
        user_id=DEMO_ID,
        runbook_ids={RB_ID},
    )
    row = _make_row("completed", risk_score=46)
    row["user_id"] = ADMIN_ID  # foreign, not shared
    assert keep(row) is False


def test_keep_running_status_is_hidden():
    keep = _make_keep_fn(
        shared_result_ids={RES_ID},
        user_id=DEMO_ID,
        runbook_ids={RB_ID},
    )
    row = _make_row("running")
    row["user_id"] = ADMIN_ID
    assert keep(row) is False


# ── Security tests: known-entries guard logic ─────────────────────────────────
# Mirror the guard logic from the detail endpoint's known-entries block.


def _apply_known_entries_guard(rows_from_admin, authorized_shared_ids, valid_statuses, owned_result_ids):
    """Pure logic mirror of the known-entries block in get_runbook_results."""
    visible = []
    for row in rows_from_admin:
        if not isinstance(row, dict):
            continue
        if row.get("status") not in valid_statuses:
            continue
        if row.get("result_id") in owned_result_ids:
            continue
        if row.get("result_id") not in authorized_shared_ids:
            continue  # security guard: skip un-shared results
        visible.append(row)
    return visible


def test_security_guard_hides_unshared_result():
    """Admin has 2 results for the runbook; only 1 was explicitly shared.
    The security guard must return exactly 1 result."""
    shared_row = _make_row("completed", result_id=RES_ID)
    unshared_row = _make_row("completed", result_id=RES_ID_2)
    admin_rows = [shared_row, unshared_row]

    visible = _apply_known_entries_guard(
        rows_from_admin=admin_rows,
        authorized_shared_ids={RES_ID},  # only RES_ID was shared
        valid_statuses={"completed", "success", "done", "draft"},
        owned_result_ids=set(),
    )
    assert len(visible) == 1
    assert visible[0]["result_id"] == RES_ID


def test_security_guard_shows_shared_draft_result():
    """The explicitly shared result has status='draft'. Must be visible."""
    draft_row = _make_row("draft", risk_score=0, result_id=RES_ID)
    visible = _apply_known_entries_guard(
        rows_from_admin=[draft_row],
        authorized_shared_ids={RES_ID},
        valid_statuses={"completed", "success", "done", "draft"},
        owned_result_ids=set(),
    )
    assert len(visible) == 1
    assert visible[0]["status"] == "draft"


def test_security_guard_empty_when_no_results_shared():
    """User has no share entries for this runbook → nothing visible."""
    row = _make_row("completed", result_id=RES_ID)
    visible = _apply_known_entries_guard(
        rows_from_admin=[row],
        authorized_shared_ids=set(),  # empty
        valid_statuses={"completed", "success", "done", "draft"},
        owned_result_ids=set(),
    )
    assert visible == []


def test_security_guard_excludes_running_regardless_of_share():
    """Even if a result_id is in the share list, running rows must be hidden."""
    running_row = _make_row("running", result_id=RES_ID)
    visible = _apply_known_entries_guard(
        rows_from_admin=[running_row],
        authorized_shared_ids={RES_ID},  # shared, but running
        valid_statuses={"completed", "success", "done", "draft"},
        owned_result_ids=set(),
    )
    assert visible == []
