-- AI Governance Scan — result storage
--
-- Backs the platform-wide Giskard sweep (see ai_governance/scan_results_store.py).
-- Mirrors the ai_guardrail_rules / ai_guardrail_violations conventions.
--
-- PRIVACY: the `result` / `summary` JSON columns must hold ONLY redacted
-- excerpts, issue metadata, and counts — never raw decrypted user text.

CREATE TABLE IF NOT EXISTS ai_governance_scan_runs (
    run_id        VARCHAR(36)  PRIMARY KEY,
    scope         VARCHAR(16)  NOT NULL,            -- 'platform' | 'org' | 'user'
    status        VARCHAR(16)  NOT NULL DEFAULT 'queued', -- queued|running|completed|failed
    modes         JSON,                              -- ["raget","prompt","tabular","guardrail"]
    user_count    INT          DEFAULT 0,
    started_by    VARCHAR(64),                       -- actor user_id, or 'system' (beat)
    summary       JSON,                              -- aggregate rollup (set on finalize)
    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    completed_at  TIMESTAMP    NULL,
    INDEX idx_status (status),
    INDEX idx_created (created_at)
);

CREATE TABLE IF NOT EXISTS ai_governance_scan_user_results (
    result_id     VARCHAR(36)  PRIMARY KEY,
    run_id        VARCHAR(36)  NOT NULL,
    user_id       VARCHAR(64)  NOT NULL,
    org_admin_id  VARCHAR(64),
    status        VARCHAR(16)  NOT NULL,             -- ok|error|skipped|degraded
    result        JSON,                              -- per-mode findings (redacted only)
    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_run (run_id),
    INDEX idx_org (org_admin_id),
    INDEX idx_run_user (run_id, user_id)
);
