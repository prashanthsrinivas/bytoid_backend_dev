"""Assessment chat — DB schema bootstrap.

Idempotent ``CREATE TABLE IF NOT EXISTS`` + best-effort ``ALTER``s, mirroring
``workflow_route.state_machine.bootstrap_schema()``. Called once at blueprint
import time (see ``assessment_chat/routes.py``).

Visibility model (baked into the schema, not left to a default):
  - ``chat_message.visibility`` ∈ {'all','internal'}. 'internal' messages are
    never shown to, pushed to, or emailed to assessee-side participants.
  - ``chat_participant.side`` ∈ {'internal','assessee'}. Reviewers/approvers and
    the admin/owner seed as 'internal'; the assessee/vendor and any externally
    added email participants are 'assessee'. The column is explicit and
    overridable at add-time so a wrong default is never silently baked in.
"""

from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)

# Supported reading languages. English is the canonical/default authoring lang.
SUPPORTED_LANGS = ("en", "fr", "es", "ja", "zh")
DEFAULT_LANG = "en"

# Message visibility.
VISIBILITY_ALL = "all"
VISIBILITY_INTERNAL = "internal"

# Participant side.
SIDE_INTERNAL = "internal"
SIDE_ASSESSEE = "assessee"


_DDL = [
    # One chat thread per assessment context. ``context_type``/``context_id``
    # is the stable anchor (e.g. ('workflow', <workflow_id>)); workflow_id /
    # doc_type / doc_id are denormalized for convenience. email_* columns hold
    # the bridged mail thread (populated in Phase 2).
    """CREATE TABLE IF NOT EXISTS chat_thread (
      thread_id        CHAR(36)     NOT NULL,
      org_id           VARCHAR(64)  NOT NULL,
      context_type     VARCHAR(32)  NOT NULL DEFAULT 'workflow',
      context_id       VARCHAR(128) NOT NULL,
      workflow_id      CHAR(36)     NULL,
      doc_type         VARCHAR(32)  NULL,
      doc_id           VARCHAR(64)  NULL,
      created_by       VARCHAR(64)  NOT NULL,
      email_provider   VARCHAR(16)  NULL,
      email_subject    VARCHAR(512) NULL,
      email_thread_id  VARCHAR(255) NULL,
      email_last_msgid VARCHAR(255) NULL,
      created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (thread_id),
      UNIQUE KEY uq_ctx (context_type, context_id),
      INDEX idx_org (org_id),
      INDEX idx_email_thread (email_thread_id)
    )""",
    # Thread membership. ``side`` and ``preferred_lang`` are the compliance- and
    # translation-critical columns. user_id is NULL for external (email-only)
    # participants; those are deduped on (thread_id, email) in code.
    """CREATE TABLE IF NOT EXISTS chat_participant (
      participant_id   CHAR(36)     NOT NULL,
      thread_id        CHAR(36)     NOT NULL,
      user_id          VARCHAR(64)  NULL,
      email            VARCHAR(255) NULL,
      role             VARCHAR(32)  NOT NULL DEFAULT 'added',
      side             VARCHAR(16)  NOT NULL DEFAULT 'internal',
      preferred_lang   VARCHAR(8)   NOT NULL DEFAULT 'en',
      is_external      TINYINT(1)   NOT NULL DEFAULT 0,
      email_bridge     TINYINT(1)   NOT NULL DEFAULT 0,
      added_by         VARCHAR(64)  NULL,
      active           TINYINT(1)   NOT NULL DEFAULT 1,
      added_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (participant_id),
      UNIQUE KEY uq_thread_user (thread_id, user_id),
      INDEX idx_thread (thread_id, active),
      INDEX idx_user (user_id)
    )""",
    # Messages. ``original_text``/``original_lang`` are always preserved; the UI
    # shows the original alongside any AI translation. ``visibility`` gates who
    # may read. ``source`` distinguishes chat vs email-ingested vs call-summary
    # vs system rows.
    """CREATE TABLE IF NOT EXISTS chat_message (
      message_id       CHAR(36)     NOT NULL,
      thread_id        CHAR(36)     NOT NULL,
      sender_user_id   VARCHAR(64)  NULL,
      sender_email     VARCHAR(255) NULL,
      original_text    TEXT         NOT NULL,
      original_lang    VARCHAR(8)   NOT NULL DEFAULT 'en',
      visibility       VARCHAR(16)  NOT NULL DEFAULT 'all',
      source           VARCHAR(16)  NOT NULL DEFAULT 'chat',
      email_message_id VARCHAR(255) NULL,
      call_id          CHAR(36)     NULL,
      created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (message_id),
      INDEX idx_thread (thread_id, created_at),
      INDEX idx_email_msg (email_message_id)
    )""",
    # Lazy per-(message, language) translation cache. Populated on first read in
    # a given language; reused thereafter. message+lang is the PK so a message
    # is translated at most once per language.
    """CREATE TABLE IF NOT EXISTS chat_message_translation (
      message_id       CHAR(36)     NOT NULL,
      lang             VARCHAR(8)   NOT NULL,
      translated_text  MEDIUMTEXT   NOT NULL,
      model            VARCHAR(64)  NULL,
      created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (message_id, lang)
    )""",
    # Audio-conferencing sessions spun up from a chat thread (Phase 3). The
    # call links to a Google Meet; after the call a recording is transcribed
    # (Whisper) and summarized into meeting notes that can be filed back into
    # the chat and/or the workflow notes feed.
    """CREATE TABLE IF NOT EXISTS chat_call_session (
      call_id          CHAR(36)     NOT NULL,
      thread_id        CHAR(36)     NOT NULL,
      started_by       VARCHAR(64)  NOT NULL,
      provider         VARCHAR(16)  NOT NULL DEFAULT 'google_meet',
      join_url         VARCHAR(512) NULL,
      event_id         VARCHAR(128) NULL,
      status           VARCHAR(16)  NOT NULL DEFAULT 'active',
      recording_ref    VARCHAR(512) NULL,
      transcript       MEDIUMTEXT   NULL,
      summary          MEDIUMTEXT   NULL,
      notes_filed      TINYINT(1)   NOT NULL DEFAULT 0,
      started_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
      ended_at         TIMESTAMP    NULL,
      PRIMARY KEY (call_id),
      INDEX idx_thread (thread_id, started_at)
    )""",
]


# Best-effort migrations for tables created before a column existed. Each is
# wrapped in its own try/except since MySQL versions differ on IF NOT EXISTS
# for ADD COLUMN; a duplicate-column error is expected and ignored.
_ALTERS = [
    "ALTER TABLE chat_thread ADD COLUMN email_last_msgid VARCHAR(255) NULL",
]


def bootstrap_schema() -> None:
    """Create chat tables if they don't exist. Idempotent — safe on every boot."""
    conn = connect_to_rds()
    if not conn:
        logger.warning("assessment_chat bootstrap_schema: no DB connection available")
        return
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
            for stmt in _ALTERS:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass  # column already exists; ignore
        conn.commit()
        logger.info("assessment_chat schema bootstrap complete")
    except Exception as exc:
        logger.error("assessment_chat bootstrap_schema failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
