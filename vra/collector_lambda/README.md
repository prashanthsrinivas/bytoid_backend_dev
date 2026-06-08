# VRA OSINT Collector Lambda

A **separate, stateless** AWS Lambda that performs vendor OSINT collection out
of the request path. It cannot touch app code or the database; it holds no KMS
keys. It receives a scan request, runs free/open-source collectors, normalizes
findings, HMAC-signs the snapshot, and POSTs it to the app callback.

## Why a separate Lambda
- **Additive / no-regression:** collection runs in a different process, so it
  physically cannot modify existing modules.
- **Isolation:** any collector failure is contained in AWS and never breaks
  assessment creation or report generation.
- **Security:** the app owns all encryption/persistence (single source of
  truth); the Lambda is a pure collector.

## Region
Deployed to `AWS_REGION` (default `ca-central-1`, matching RDS/KMS/Secrets).

## Networking
Runs **outside the VPC** so it has public internet egress for OSINT and can
reach the app's public HTTPS callback (`VRA_CALLBACK_BASE_URL` /
`api.bytoid.ai`). No NAT gateway required.

## Contents
- `handler.py` — entrypoint (`lambda_handler`). Phase 0 = skeleton that emits a
  valid empty snapshot so the invoke→collect→callback path is testable.
- `deploy.py` — boto3 create-or-update provisioner (Phase 2). Idempotent;
  creates a least-privilege IAM role (CloudWatch Logs only — no KMS).
- `requirements.txt` — Lambda-only deps (Phase 2): `dnspython`, `requests`,
  `tldextract`, `cryptography`, `beautifulsoup4`.

## Shared code
The handler imports `vra.osint.{safe_fetch,normalize,signing}` and `vra.schema`
— all stdlib/requests-only by design so they vendor cleanly into the bundle.

## Deploy (Phase 2)
```bash
make deploy-vra-lambda            # packages + create-or-update in AWS_REGION
# uses an explicit admin/CI credential (NOT the app's scoped runtime role)
```

> The app never deploys this automatically. Provisioning cloud infra is a
> deliberate, authorized action run by an operator with create permissions
> (`iam:CreateRole`, `lambda:CreateFunction`, ...).
