-- Workflow configuration per org per artifact type
CREATE TABLE IF NOT EXISTS workflow_config (
  org_id            VARCHAR(64)  NOT NULL,
  doc_type          VARCHAR(32)  NOT NULL,       -- 'policy'|'procedure'|'runbook'|'report'
  assignment_mode   VARCHAR(32)  NOT NULL DEFAULT 'per_document',  -- 'per_document'|'role_based'
  reviewer_role_id  VARCHAR(64)  NULL,
  approver_role_id  VARCHAR(64)  NULL,
  states_json       JSON         NOT NULL,
  updated_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (org_id, doc_type)
);

-- Org-wide document review cadence. One row per org; every document follows
-- the same review cycle. frequency enum: '3_months'|'6_months'|'9_months'|'12_months'.
CREATE TABLE IF NOT EXISTS org_review_config (
  org_id            VARCHAR(64)  NOT NULL,
  review_frequency  VARCHAR(32)  NOT NULL DEFAULT '12_months',
  updated_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (org_id)
);

-- Per-document workflow state
CREATE TABLE IF NOT EXISTS document_workflow (
  workflow_id       CHAR(36)     NOT NULL,
  org_id            VARCHAR(64)  NOT NULL,
  doc_type          VARCHAR(32)  NOT NULL,
  doc_id            VARCHAR(64)  NOT NULL,
  doc_version       VARCHAR(32)  NOT NULL,
  owner_user_id     VARCHAR(64)  NOT NULL,
  state             VARCHAR(32)  NOT NULL DEFAULT 'draft',
  current_reviewer  VARCHAR(64)  NULL,
  current_approver  VARCHAR(64)  NULL,
  state_version     INT          NOT NULL DEFAULT 1,
  submitted_at      TIMESTAMP    NULL,
  approved_at       TIMESTAMP    NULL,
  published_at      TIMESTAMP    NULL,
  created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (workflow_id),
  UNIQUE KEY uq_doc (doc_type, doc_id, doc_version),
  INDEX idx_reviewer (current_reviewer, state),
  INDEX idx_approver (current_approver, state),
  INDEX idx_org (org_id, doc_type, state)
);

-- Immutable audit trail of every state transition
CREATE TABLE IF NOT EXISTS document_workflow_events (
  event_id          CHAR(36)     NOT NULL,
  workflow_id       CHAR(36)     NOT NULL,
  from_state        VARCHAR(32)  NULL,
  to_state          VARCHAR(32)  NOT NULL,
  actor_user_id     VARCHAR(64)  NOT NULL,
  comment           TEXT         NULL,
  created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (event_id),
  INDEX idx_wf (workflow_id, created_at)
);

-- Dead-letter queue for failed workflow email notifications
CREATE TABLE IF NOT EXISTS workflow_email_dlq (
  dlq_id            CHAR(36)     NOT NULL,
  workflow_id       CHAR(36)     NULL,
  event_id          CHAR(36)     NULL,
  org_id            VARCHAR(64)  NOT NULL,
  recipient         VARCHAR(255) NOT NULL,
  template_name     VARCHAR(64)  NOT NULL,
  context_json      TEXT         NOT NULL,
  last_error        TEXT         NULL,
  retry_count       INT          NOT NULL DEFAULT 0,
  last_retry_at     TIMESTAMP    NULL,
  status            VARCHAR(32)  NOT NULL DEFAULT 'pending',  -- 'pending'|'succeeded'|'permanent_failure'
  created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (dlq_id),
  INDEX idx_pending (status, created_at),
  INDEX idx_org (org_id, status)
);

-- Additive columns on notifications table for workflow context
ALTER TABLE notifications
  ADD COLUMN IF NOT EXISTS doc_type       VARCHAR(32) NULL,
  ADD COLUMN IF NOT EXISTS doc_id         VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS workflow_id    CHAR(36)    NULL,
  ADD COLUMN IF NOT EXISTS workflow_state VARCHAR(32) NULL,
  ADD COLUMN IF NOT EXISTS action_required TINYINT(1) DEFAULT 0;

-- Feature flags per org (used by policy_hub_v2_enabled, etc.)
CREATE TABLE IF NOT EXISTS org_feature_flags (
  org_id      VARCHAR(64)  NOT NULL,
  flag_name   VARCHAR(64)  NOT NULL,
  flag_value  VARCHAR(255) NOT NULL DEFAULT 'false',
  updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (org_id, flag_name)
);
