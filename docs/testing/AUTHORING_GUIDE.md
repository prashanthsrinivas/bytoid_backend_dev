# Test Authoring Guide

How to write tests for the Bytoid backend. Follow this rubric so every feature
ships with adequate coverage before it merges.

## Directory layout

```
tests/
  unit/          ← pure-function tests; dependencies mocked
    services/
    utils/
    categories/
    db/
  integration/   ← two or more real components talking
    api_to_db/
    retry_logic/
    file_upload/
  regression/    ← one file per closed bug, test_regression_<ticket>.py
  fuzz/          ← Hypothesis property-based tests
  concurrency/   ← threading / race-condition tests
  fault_injection/  ← mocked failures: DB timeout, S3 503, OpenAI 500
  recovery/      ← graceful-degradation and fallback validation
testing/         ← legacy test tree (do not add new files here)
```

## Rubric for every blueprint / service

For each Flask blueprint registered in `app.py` and each service in `services/`:

| Test type | File | Minimum |
|---|---|---|
| Unit — happy path | `tests/unit/<area>/test_<module>.py` | 1 per public function |
| Unit — unhappy path | same file | 1 per error branch (4xx, 5xx) |
| Integration — happy path | `tests/integration/...` | 1 end-to-end call |
| Integration — unhappy path | same file | 1 upstream failure scenario |
| Regression | `tests/regression/test_regression_<ticket>.py` | 1 per closed bug |
| Fuzz | `tests/fuzz/test_<module>.py` | 1 per `request.get_json()` consumer |
| Authz | `tests/unit/...` or `tests/integration/...` | 1 per `@permission_required` |

## The critical import constraint

`db/rds_db.py` calls `boto3.client("secretsmanager")` **at module import time**
and will crash in any environment without AWS credentials (including CI and local
machines without an `.env` pointing to a live Secrets Manager). This is a hard
constraint you must work around in every test file.

**Pattern — stub before importing:**
```python
import sys, types
from unittest.mock import MagicMock

# Must happen BEFORE any import that transitively touches db.rds_db.
for mod in ("pymysql", "pymysql.cursors", "db", "db.rds_db", "db.db_checkers",
            "utils.s3_utils", "utils.celery_base"):
    sys.modules.setdefault(mod, MagicMock())

# Now safe to import anything that uses these.
from my_module import MyClass
```

The `db_stubs` fixture (available everywhere via `tests/conftest.py`) does this
for you inside a test; use it for test-level isolation. For module-level imports,
stub manually at the top of the file.

## Flask test client pattern

```python
import pytest
from flask import Flask
from tests_routes.routes import tests_bp

@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(tests_bp)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    return app

@pytest.fixture
def client(app):
    return app.test_client()

def test_something(client):
    resp = client.get("/tests/categories")
    assert resp.status_code == 200
```

Patch `tests_routes.routes.ACCESSIBLE_IDS` to include a test user ID
when testing authenticated endpoints.

## Result store isolation

`tests_routes/result_store.py` writes to `testing/results/` by default. Redirect
it in tests using the global-mutation fixture pattern:

```python
import tests_routes.result_store as rs

@pytest.fixture(autouse=True)
def isolate_store(tmp_path):
    orig_root, orig_summary = rs.RESULTS_ROOT, rs.SUMMARY_PATH
    rs.RESULTS_ROOT = str(tmp_path)
    rs.SUMMARY_PATH = str(tmp_path / "summary.json")
    yield
    rs.RESULTS_ROOT = orig_root
    rs.SUMMARY_PATH = orig_summary
```

## Property-based / fuzz tests

Use Hypothesis for any function that accepts user-controlled string input:

```python
from hypothesis import given, settings, strategies as st
import pytest

@pytest.mark.fuzz
@settings(max_examples=200)
@given(st.text())
def test_my_function_never_raises(s):
    my_function(s)  # assert no exception
```

Always gate on `pytest.importorskip("hypothesis")` or add `hypothesis` to
`requirements.txt` (Phase 3 already did this).

## Markers

| Marker | When to use |
|---|---|
| `@pytest.mark.unit` | Pure unit test with mocked deps |
| `@pytest.mark.integration` | Multi-component, real I/O |
| `@pytest.mark.regression` | Locking in a fixed bug |
| `@pytest.mark.fuzz` | Hypothesis / property-based |
| `@pytest.mark.concurrency` | Threading or async race tests |
| `@pytest.mark.chaos` | Fault injection (crash/timeout simulation) |
| `@pytest.mark.security` | Auth, RBAC, injection, HMAC |
| `@pytest.mark.slow` | Takes >1s; skipped in `make test-fast` |
| `@pytest.mark.live_llm` | Hits real LLM; gated by `RUN_LIVE_LLM=1` |

## AI/RAG-specific requirements (Phase 4)

Every LangChain chain or agent tool in `agent_routes/`, `ai_assistant_chat/`,
`workflow_service.py` must have:

1. A prompt-injection test — known jailbreak strings must not cause the model to
   execute system tools or exfil credentials.
2. A vector-poisoning test — an adversarial document in LanceDB must not crowd
   out legitimate results.
3. An output-sanitization test — SSN/credit-card/API-key-shaped output must be
   redacted before reaching the client.

These live in `tests/security/llm/` (Phase 4).

## Running subsets

```bash
# Only new Phase 3 tests
pytest tests/unit/ tests/integration/ tests/regression/ tests/fuzz/ \
       tests/concurrency/ tests/fault_injection/ tests/recovery/ -v

# All tests (fast — skip slow/chaos/live_llm)
make test-fast

# Full suite
make test

# Coverage
make coverage
```

## What NOT to do

- Do not import `db.rds_db` directly. Use stubs.
- Do not write tests that hit real AWS, RDS, Redis, or OpenAI endpoints without
  the `@pytest.mark.live_llm` / `@pytest.mark.slow` marker and an env-var gate.
- Do not use `unittest.TestCase`. Use pytest fixtures and plain `def test_*`.
- Do not add new files to `testing/` (legacy). Use `tests/` sub-directories.
- Do not write trivial "assert True" or "assert 1 == 1" tests — every test must
  make a falsifiable assertion about the production code's behavior.
