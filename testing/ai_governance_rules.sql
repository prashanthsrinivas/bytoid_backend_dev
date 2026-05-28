-- AI Governance: structured guardrail rules and violation log.
--
-- Rules are authored from the frontend (Guardrails tab) and stored per org.
-- Every LLM call routed through utils/fireworkzz.py or utils/chatopenzz.py
-- is checked against the active rule set via ai_governance/enforcer.py.

CREATE TABLE IF NOT EXISTS ai_guardrail_rules (
    rule_id      VARCHAR(36)  PRIMARY KEY,
    org_admin_id VARCHAR(64)  NOT NULL,
    name         VARCHAR(255) NOT NULL,
    description  TEXT,
    rule_type    VARCHAR(32)  NOT NULL,
        -- blocked_phrase | regex | pii | topic | max_tokens | model_allowlist
    config       JSON         NOT NULL,
    applies_to   VARCHAR(16)  NOT NULL DEFAULT 'both',
        -- input | output | both
    action       VARCHAR(16)  NOT NULL DEFAULT 'audit',
        -- block | redact | warn | audit
    scope        JSON,
        -- optional: {"features":["ai_reporting"],"models":["moonshotai.kimi-k2.5"]}
    enabled      TINYINT(1)   NOT NULL DEFAULT 1,
    created_by   VARCHAR(64),
    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_org_enabled (org_admin_id, enabled),
    INDEX idx_rule_type (rule_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS ai_guardrail_violations (
    violation_id VARCHAR(36) PRIMARY KEY,
    rule_id      VARCHAR(36) NOT NULL,
    rule_name    VARCHAR(255),
    org_admin_id VARCHAR(64),
    user_id      VARCHAR(64),
    feature      VARCHAR(64),
    model        VARCHAR(128),
    direction    VARCHAR(8),
        -- input | output
    action_taken VARCHAR(16),
        -- block | redact | warn | audit
    excerpt      TEXT,
    trace_id     VARCHAR(64),
    request_id   VARCHAR(64),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_org_time (org_admin_id, created_at),
    INDEX idx_rule (rule_id),
    INDEX idx_feature_time (feature, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
