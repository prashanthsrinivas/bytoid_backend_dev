"""Multi-account AWS Security Group auditing + AI tightening recommendations.

An *audit* fans out across every account in an AWS Organization, fetches all
Security Groups (+ their rules and ENI attachments) per region, runs a
deterministic rule engine over them, scores posture, surfaces the findings on a
Security Posture Dashboard, and — on demand — produces grounded AI remediation
("how to tighten").

Design constraints (do not violate):
  * **Additive only.** This package never modifies existing modules. The only
    edits to existing files are append-only registrations (blueprint list, one
    EXEMPT_PATHS entry, permission metadata, permission labels).
  * **Fail-safe.** Cross-account collection is isolated per account/region; any
    account/region failure lands in ``collector_status`` and degrades to a
    partial snapshot — it never fails the whole audit.
  * **Security-first.** Cross-account reach is read-only (a standard audit role
    assumed with a per-tenant ExternalId). The inbound Lambda callback is
    HMAC-signed, timestamp-bounded, replay-protected, and idempotent. Base STS
    credentials and the HMAC secret are passed at invoke time and never logged.
  * **Faithful AI.** The AI recommender only explains/prioritizes/suggests
    remediation for findings the deterministic engine produced; it can never
    invent a security group, rule, or finding (enforced by post-validation).

The heavy cross-account fetch + deterministic analysis run in a separate AWS
Lambda (``collector_lambda/``) which holds no KMS keys and never touches the DB;
it HMAC-signs the snapshot and POSTs it to the app callback. The app owns all
encryption/persistence (single source of truth in S3), mirroring the ``vra``
module's collector pattern.
"""
