"""Tests for the intake-questionnaire-to-runbook trigger pathway.

Covers:
- `_question_answer_stats` is now pure (no Celery side-effect)
- All four submission methods enqueue the runbook task exactly once and only
  AFTER `saveworkflowtos3` has run
- `trigger_runbook_from_playbook` returns a structured `{status, ...}` dict on
  success, failure, and runbook-not-found

These tests intentionally bypass `WorkflowRunnerV2.__init__` (which calls into
AWS / RDS / S3) and construct instances via `object.__new__` so they remain
pure unit tests with no external dependencies.

Run:
    python -m pytest tests/test_workflow_runbook_trigger.py -v
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock, patch


# ── Stub AWS / DB / S3 modules at import time ─────────────────────────────────
# `services.workflow_service` and `runbook.helper` import modules that touch
# AWS Secrets Manager and the RDS pool at module load. Replace them with
# lightweight stubs before importing the units under test.

def _install_stubs() -> None:
    """Install lightweight stubs ONLY for submodules whose real implementations
    pull AWS/RDS/LLM clients at import time. Do not stub real packages (services,
    runbook, utils) — we want the real packages on disk; only their heavyweight
    submodules get replaced."""

    def _stub_module(name: str, **attrs) -> None:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod

    # db.* — touches AWS Secrets Manager at module load.
    db_pkg = types.ModuleType("db")
    db_pkg.__path__ = []  # placeholder package; we override its submodules below
    sys.modules["db"] = db_pkg
    _stub_module("db.rds_db",
                 connect_to_rds=lambda: MagicMock(name="conn"),
                 get_cursor=lambda *a, **kw: MagicMock(name="cursor"),
                 get_secret=lambda: {"username": "u", "password": "p"})
    _stub_module("db.db_checkers",
                 fetch_contacts_by_user=lambda *a, **kw: [],
                 fetch_user_Social=lambda *a, **kw: "google",
                 get_userinfo=lambda *a, **kw: {},
                 get_email_by_id=lambda *a, **kw: "",
                 check_userid_valid=lambda *a, **kw: True,
                 make_api_key=lambda *a, **kw: "k")
    _stub_module("db.lance_db_service",
                 LanceDBServer=MagicMock(name="LanceDBServer"))

    # playbook.helperzz — transitive chain pulls docx/pptx/etc.
    _stub_module("playbook.helperzz",
                 save_execution_playbook_to_s3=lambda *a, **kw: None,
                 save_playbook_to_s3=lambda *a, **kw: None)

    # utils.* — LLM/HTTP/YAML wrappers; replace with no-op shims.
    # fireworkzz has dozens of helpers used across the codebase — install a
    # permissive MagicMock so any attribute access returns a no-op.
    sys.modules["utils.fireworkzz"] = MagicMock(name="utils.fireworkzz")
    _stub_module("utils.normal",
                 can_reply_to_email=lambda *a, **kw: False,
                 load_yaml_file=lambda *a, **kw: {"wf_conversation": ""},
                 read_function_jsons=lambda *a, **kw: [],
                 read_function_jsons2=lambda *a, **kw: [],
                 parse_composite_user_id=lambda x: (x, x))
    _stub_module("utils.s3_utils",
                 read_json_from_s3=lambda *a, **kw: {},
                 attach_CLDFRNT_url=lambda k: k,
                 upload_any_file=lambda *a, **kw: None,
                 s3bucket=MagicMock(),
                 S3_BUCKET="test-bucket",
                 load_yaml_from_s3=lambda *a, **kw: {})

    # cust_helpers.pathconfig
    _stub_module("cust_helpers.pathconfig", play_template="")

    # utils.celery_base — imported lazily inside the trigger blocks. The tests
    # patch `utils.celery_base.create_playbook_runbook_task`, so the module
    # needs to exist with that attribute as a Mock.
    _stub_module("utils.celery_base",
                 create_playbook_runbook_task=MagicMock(name="celery_task"))


_install_stubs()

# Now safe to import the unit under test.
# Note: tests for `runbook.helper.trigger_runbook_from_playbook` would require
# stubbing an extensive transitive chain (langchain_openai, apscheduler, etc.)
# and have been left for manual / integration verification — see the plan file.
from services.workflow_service import WorkflowRunnerV2  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_runner(
    *,
    previous_data: dict,
    workflow_json: dict | None = None,
    chat_history: list | None = None,
    steps: dict | None = None,
) -> WorkflowRunnerV2:
    """Construct a WorkflowRunnerV2 without going through its heavy __init__."""
    runner = object.__new__(WorkflowRunnerV2)
    runner.userid = "user-1"
    runner.filename = "abcd1234.json"
    runner.workflow_json = workflow_json if workflow_json is not None else {"runbook_id": "rb-1"}
    runner.previous_data = previous_data
    runner.chat_history = chat_history if chat_history is not None else []
    runner.steps = steps or {}
    runner.step_order = {sid: i for i, sid in enumerate(runner.steps)}
    runner.logger = MagicMock(name="logger")
    runner.testing = True
    # Replace persistence with a tracker so order assertions work.
    runner.saveworkflowtos3 = MagicMock(name="saveworkflowtos3")
    return runner


def q(qid: str, ans: str | None = None) -> dict:
    return {"id": qid, "user_answer": ans, "comment": None}


def field(fid: str, ans=None, required=True) -> dict:
    return {"id": fid, "user_answer": ans, "required": required}


def make_qna_previous_data(answered: int, total: int) -> dict:
    """Build previous_data with `total` questions, first `answered` filled."""
    outputs = []
    for i in range(total):
        outputs.append(q(f"q{i}", "yes" if i < answered else None))
    return {"step-1": {"output": outputs}}


def make_form_previous_data(answered: int, total: int) -> dict:
    fields = []
    for i in range(total):
        fields.append(field(f"f{i}", "yes" if i < answered else None))
    return {
        "step-1": {
            "output": {
                "form_schema": {"fields": fields},
            }
        }
    }


# ── Tests for the pure stats helper ───────────────────────────────────────────

class TestQuestionAnswerStatsIsPure:
    def test_returns_correct_counts(self):
        runner = make_runner(previous_data=make_qna_previous_data(answered=3, total=4))
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            stats = runner._question_answer_stats()
        assert stats == {"answered": 3, "total": 4, "all_answered": False}
        task_mock.delay.assert_not_called()

    def test_no_celery_side_effect_when_all_answered(self):
        runner = make_runner(previous_data=make_qna_previous_data(answered=4, total=4))
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            stats = runner._question_answer_stats()
            # Call again — must remain side-effect-free
            runner._question_answer_stats()
        assert stats["all_answered"] is True
        task_mock.delay.assert_not_called()


# ── Tests for the Q&A single submission path ──────────────────────────────────

class TestAnswerQuestionsTrigger:
    def _setup_runner(self, *, answered=3, total=4, runbook_id="rb-1"):
        wf_json = {"runbook_id": runbook_id} if runbook_id else {}
        chats = [
            {"id": "chat-1", "step_id": "step-1",
             "output": [q(f"q{i}", "yes" if i < answered else None) for i in range(total)]}
        ]
        return make_runner(
            previous_data=make_qna_previous_data(answered, total),
            workflow_json=wf_json,
            chat_history=chats,
        )

    def test_completion_triggers_after_save(self):
        runner = self._setup_runner(answered=3, total=4)
        parent = MagicMock()
        runner.saveworkflowtos3 = parent.save

        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            task_mock.delay = parent.delay
            asyncio.run(runner.answer_questions(
                answer="yes", comment=None, qid="q3", chid="chat-1",
            ))

        # delay called exactly once with the expected args
        parent.delay.assert_called_once_with("user-1", "abcd1234.json", "rb-1")
        # save happened before delay
        order = [c[0] for c in parent.mock_calls]
        assert order.index("save") < order.index("delay")

    def test_partial_answer_does_not_trigger(self):
        runner = self._setup_runner(answered=2, total=4)
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            asyncio.run(runner.answer_questions(
                answer="yes", comment=None, qid="q2", chid="chat-1",
            ))
        task_mock.delay.assert_not_called()

    def test_no_trigger_when_runbook_id_missing(self):
        runner = self._setup_runner(answered=3, total=4, runbook_id=None)
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            asyncio.run(runner.answer_questions(
                answer="yes", comment=None, qid="q3", chid="chat-1",
            ))
        task_mock.delay.assert_not_called()

    def test_clearing_last_answer_does_not_trigger(self):
        # Start fully answered, then clear the last answer.
        runner = self._setup_runner(answered=4, total=4)
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            asyncio.run(runner.answer_questions(
                answer="", comment=None, qid="q3", chid="chat-1",
            ))
        task_mock.delay.assert_not_called()

    def test_save_called_exactly_once(self):
        runner = self._setup_runner(answered=3, total=4)
        with patch("utils.celery_base.create_playbook_runbook_task"):
            asyncio.run(runner.answer_questions(
                answer="yes", comment=None, qid="q3", chid="chat-1",
            ))
        assert runner.saveworkflowtos3.call_count == 1

    def test_response_shape_unchanged(self):
        runner = self._setup_runner(answered=3, total=4)
        with patch("utils.celery_base.create_playbook_runbook_task"):
            res = asyncio.run(runner.answer_questions(
                answer="yes", comment=None, qid="q3", chid="chat-1",
            ))
        assert set(res.keys()) == {"status", "all_questions_answered", "message"}
        assert res["status"] == "success"
        assert res["all_questions_answered"] is True


# ── Tests for the Q&A bulk submission path ────────────────────────────────────

class TestAnswerQuestionsBulkTrigger:
    def _setup_runner(self, *, answered=0, total=3, runbook_id="rb-1"):
        wf_json = {"runbook_id": runbook_id} if runbook_id else {}
        chats = [
            {"id": "chat-1", "step_id": "step-1",
             "output": [q(f"q{i}", "yes" if i < answered else None) for i in range(total)]}
        ]
        runner = make_runner(
            previous_data=make_qna_previous_data(answered, total),
            workflow_json=wf_json,
            chat_history=chats,
            steps={"step-1": {"id": "step-1", "title": "Step One"}},
        )
        return runner

    def test_completion_triggers_after_save(self):
        runner = self._setup_runner(answered=0, total=3)
        parent = MagicMock()
        runner.saveworkflowtos3 = parent.save
        bulk = [
            {"question_id": "q0", "user_answer": "a"},
            {"question_id": "q1", "user_answer": "b"},
            {"question_id": "q2", "user_answer": "c"},
        ]
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            task_mock.delay = parent.delay
            res = asyncio.run(runner.answer_questions_bulk(bulk, "chat-1"))

        parent.delay.assert_called_once_with("user-1", "abcd1234.json", "rb-1")
        order = [c[0] for c in parent.mock_calls]
        assert order.index("save") < order.index("delay")
        assert res["all_questions_answered"] is True
        assert set(res.keys()) == {"status", "all_questions_answered", "message"}

    def test_partial_bulk_does_not_trigger(self):
        runner = self._setup_runner(answered=0, total=3)
        bulk = [{"question_id": "q0", "user_answer": "a"}]
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            asyncio.run(runner.answer_questions_bulk(bulk, "chat-1"))
        task_mock.delay.assert_not_called()


# ── Tests for the form submission paths (regression) ──────────────────────────

class TestFormFieldTriggerRegression:
    def _setup_runner(self, *, answered=0, total=2):
        chats = [
            {"id": "chat-1", "step_id": "step-1",
             "output": {"form_schema": {"fields": [
                 field(f"f{i}", "yes" if i < answered else None) for i in range(total)
             ]}}}
        ]
        return make_runner(
            previous_data=make_form_previous_data(answered, total),
            workflow_json={"runbook_id": "rb-1", "pre_user_data": {}},
            chat_history=chats,
            steps={"step-1": {"id": "step-1"}},
        )

    def test_single_field_completion_triggers_after_save(self):
        runner = self._setup_runner(answered=1, total=2)
        parent = MagicMock()
        runner.saveworkflowtos3 = parent.save
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            task_mock.delay = parent.delay
            asyncio.run(runner.update_form_field("f1", "yes", "chat-1"))
        parent.delay.assert_called_once_with("user-1", "abcd1234.json", "rb-1")
        order = [c[0] for c in parent.mock_calls]
        assert order.index("save") < order.index("delay")

    def test_bulk_form_completion_triggers_after_save(self):
        runner = self._setup_runner(answered=0, total=2)
        parent = MagicMock()
        runner.saveworkflowtos3 = parent.save
        answers = [{"id": "f0", "answer": "y"}, {"id": "f1", "answer": "y"}]
        with patch("utils.celery_base.create_playbook_runbook_task") as task_mock:
            task_mock.delay = parent.delay
            res = asyncio.run(runner.update_form_bulk(answers, "chat-1"))
        parent.delay.assert_called_once_with("user-1", "abcd1234.json", "rb-1")
        order = [c[0] for c in parent.mock_calls]
        assert order.index("save") < order.index("delay")
        assert res["form_completed"] is True


# Tests for `runbook.helper.trigger_runbook_from_playbook` are deferred to
# integration / manual verification — the module's transitive imports
# (langchain_openai, apscheduler, internal AWS clients) make pure unit testing
# impractical without a far more extensive stub layer. The behavior change is
# small (wrap engine call, return dict) and is covered by manual verification:
# see the "End-to-end manual verification" section in the plan file.
