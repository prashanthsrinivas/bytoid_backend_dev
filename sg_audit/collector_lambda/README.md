# SG-audit collector Lambda

Stateless cross-account Security Group collector. The app invokes it
(`InvocationType="Event"`) with an audit scope + the caller's short-lived base
STS credentials; it assumes the read-only audit role in each member account,
fetches Security Groups + rules + ENI attachments per region, runs the
deterministic rule engine (`sg_audit/analysis/rules.py`), HMAC-signs the
snapshot, and POSTs it to `/sg-audit/callback`.

It holds **no KMS keys** and never touches the database — the app owns all
encryption/persistence.

## 1. Member-account audit role (deploy once per account, org-wide via StackSet)

Create a role named `BytoidSecurityAuditRole` (or whatever you pass as
`role_name`) in **every member account** you want audited.

**Permissions policy** (read-only; `ec2:Describe*` does not support
resource-level scoping, so `Resource: "*"` is expected). The AWS-managed
`SecurityAudit` policy is an acceptable single-ARN alternative.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "SGAuditReadOnly",
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSecurityGroupRules",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeRegions",
      "ec2:DescribeInstances",
      "ec2:DescribeVpcs"
    ],
    "Resource": "*"
  }]
}
```

**Trust policy** — the load-bearing security control. Principal is the tenant's
own management/security account; the `ExternalId` is the per-tenant value shown
on the audit record (the confused-deputy defense):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<MANAGEMENT_OR_SECURITY_ACCOUNT_ID>:root" },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "<PER-TENANT-EXTERNALID>" } }
  }]
}
```

If you use org auto-discovery, the management/delegated-admin account's SAML
role must also allow `organizations:ListAccounts`; otherwise register an explicit
`account_ids` list on the audit instead.

## 2. Deploy the Lambda

Run with an admin/CI credential (NOT the app runtime role):

```bash
python -m sg_audit.collector_lambda.deploy \
  --function bytoid-sg-audit-collector \
  --callback-url https://api.bytoid.ai/sg-audit/callback \
  --hmac-secret "$SG_HMAC_SECRET" \
  --region ca-central-1
```

The execution role is **CloudWatch Logs only** — cross-account reach comes from
the passed-in base credentials, authorized by each member role's trust policy.

## 3. Enable collection in the app

Set in the app environment:

- `SG_LAMBDA_ARN` — the deployed function ARN (or name)
- `SG_CALLBACK_BASE_URL` — public HTTPS base (e.g. `https://api.bytoid.ai`)
- `SG_HMAC_SECRET` — same secret passed to `--hmac-secret` above

Until both `SG_LAMBDA_ARN` and `SG_HMAC_SECRET` are set, the app runs in
collection-disabled mode (audits register, but `collect` returns
`{"status": "disabled"}` — a safe no-op).
