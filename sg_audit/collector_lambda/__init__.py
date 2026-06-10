"""SG-audit collector Lambda (separate deployable).

This subpackage is bundled and deployed to AWS Lambda by ``deploy.py``. It is a
*stateless collector*: it receives an audit scope + the caller's short-lived base
STS credentials, assumes the read-only audit role in each member account (with the
per-tenant ExternalId), fetches every Security Group (+ rules + ENI attachments)
per region, runs the deterministic rule engine (``sg_audit.analysis.rules``),
HMAC-signs the snapshot (``sg_audit.signing``), and POSTs it to the app callback.

It holds NO KMS keys and never touches the database — the app owns all encryption
and persistence. Its own execution role needs only CloudWatch Logs; the
cross-account reach comes from the passed-in base credentials, authorized by each
member account's role trust policy (ExternalId condition).

Named ``collector_lambda`` (not ``lambda``) because ``lambda`` is a Python
reserved word and could not be imported as a package.
"""
