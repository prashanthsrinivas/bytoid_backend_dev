# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run the development server:**
```bash
python app.py --host 0.0.0.0 --port 3000
```

**Run the production server (gunicorn):**
```bash
gunicorn app:app --bind 0.0.0.0:3000
```

**Run a Celery worker:**
```bash
celery -A utils.celery_base worker --loglevel=info
```

**Lint with ruff:**
```bash
ruff check .
ruff check --fix .
```

**There is no test suite.** The `tests/` and `testing/` directories contain SQL schema snippets, not runnable tests.

**Environment:** The primary `.env` is loaded from a hardcoded path outside the repo:
```
/home/ec2-user/bytoid_python/.env
```
A local `.env` at the project root is also present (gitignored). The `DEV=true` env var toggles dev vs. prod behavior throughout the app.

**DB credentials** come from AWS Secrets Manager (not `.env`). Dev DB: `bytoiddb.c9ek8228ux41.ca-central-1.rds.amazonaws.com` / `bytoid_support_agent`. Prod DB: `bytoidprod.c9ek8228ux41.ca-central-1.rds.amazonaws.com` / `bytoid`.

## Architecture

### Structure

Every feature is a self-contained package with a `routes.py` that exports a Flask Blueprint. There are ~40 such packages (e.g., `users_routes/`, `invited_users/`, `gmail_route/`, `ai_reporting/`). Shared infrastructure lives in three directories:

- `db/` — MySQL connection pool (`rds_db.py`), DB helper functions (`db_checkers.py`)
- `utils/` — Decorators, loggers, config, permissions, Celery setup, S3 helpers
- `services/` — Feature service classes (Gmail, Outlook, Redis, Stripe, audit logging, etc.)

### Blueprint Registration (`app.py`)

All blueprints are collected in a flat `blueprints` list. Two loops run: the first applies `CORS(bp, supports_credentials=True, origins=ALLOWED_ORIGINS)` to each, then the second calls `app.register_blueprint(bp)`. No URL prefixes are set at registration — each blueprint defines its own full paths internally.

`app.py` has a `before_request` that stamps `g.start_time` and `g.request_id`, and an `after_request` that logs every request (method, path, status, duration, IP) via `get_logger(blueprint_name)`.

**Note:** There are two `@app.after_request` functions defined in `app.py`. Python registers only the second one (lines 235–248, the request logger). The first one (CORS header injection) is shadowed — CORS is handled instead by the per-blueprint `CORS(bp, ...)` calls.

### Database Access

All DB access uses raw PyMySQL — no ORM. The connection pool is initialized once at module import of `db/rds_db.py`.

Standard pattern for route handlers:
```python
conn = connect_to_rds()
with conn.cursor(pymysql.cursors.DictCursor) as cursor:
    cursor.execute("SELECT ... FROM users WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
conn.commit()
conn.close()
```

Use `safe_execute(cursor, query, params)` for write queries that may hit deadlocks (retries 3× with backoff).

`db/db_checkers.py` contains reusable DB helper functions — check there before writing new queries for common patterns like `check_userid_valid()`, `get_userinfo()`, `make_api_key()`, etc.

### Authentication & Session

**The session middleware (`session_middleware.py`) is currently commented out in `app.py`.** Routes that require authentication handle it independently. When it is active, `register_session_check(app)` attaches a `before_request` that:

1. Skips `EXEMPT_PATHS` (login, OAuth callbacks, etc.)
2. Reads `session_id` cookie (SHA-256 hash of the Redis session UUID) and `access_token` cookie (or `Authorization: Bearer`)
3. Validates against Redis, auto-refreshes expired access tokens using the refresh token
4. On success, populates `g.user_id`, `g.user` (full user dict with parsed permissions), `g.session_data`, `g.current_access_token`

**Token lifetimes:** session 30 min, access token 15 min, refresh token 1 hr.

**`g.user_id`** is the canonical identity carrier used by `@permission_required`. When the middleware is inactive, routes fall back to `session.get("user_id")` (Flask session) or `data.get("user_id")` (request body). This inconsistency is a known issue.

### Authorization (`utils/permission_required.py`)

`@permission_required("some_permission")` is a route decorator that:

- **Admins:** self-access always allowed; cross-user (normal user) always allowed within the same org; cross-admin access requires a row in the `special_access` table (`grantor_admin_id = target, target_admin_id = current_user`).
- **Normal users:** checks `shared_users` table for an active sharing relationship, then validates the required permission exists in the owner's `roles_creation` JSON column.

### Admin Special Access (`special_access` table)

Schema: `(grantor_admin_id, target_admin_id)` — "grantor allows target to access their data."

Managed by four endpoints in `invited_users/routes.py`:
- `POST /admin/grant_special_access` — direct grant (both admins same org)
- `POST /admin/request_special_access` — sends email invite via Outlook
- `GET|POST /admin/accept_special_access` — target admin accepts request
- `POST /admin/revoke_special_access` — removes the grant row

### Logging

**Application logging:** `logger = get_logger(__name__)` in every module. Writes text-formatted logs to `logs/app.log` (5 MB rotating, 3 backups). Format: `HH:MM:SS | LEVEL | name | message`.

**Audit logging:** `services/audit_log_service.py`. Writes structured JSON (one line per event) to `logs/audit.log` (10 MB rotating, 5 backups). Use `log_audit_event(action, endpoint, ip, status, ...)` — it never raises. Action constants (`LOGIN_SUCCESS`, `LOGIN_FAILED`, `SPECIAL_ACCESS_GRANTED`, `SPECIAL_ACCESS_REVOKED`) are defined in the same module. The `_upload_to_s3()` stub is the designated hook for future S3 archival.

### Celery

The Celery app is in `utils/celery_base.py`, exported as `celery`. Broker and backend use `CELERY_BROKER_URL` (ElastiCache Redis with TLS). Dev mode uses `ssl_cert_reqs=none`; prod requires a PEM cert at `/home/ec2-user/bytoid_python/awsredis.pem`.

### Key Config Values (`utils/app_configs.py`)

- `IS_DEV` — set from `DEV=true` env var
- `ALLOWED_ORIGINS` — CORS allowlist; includes `DEV_ORIGINS` only when `IS_DEV=True`
- `BACKURL` — backend API base URL (dev: AWS API Gateway; prod: `https://api.bytoid.ai`)
