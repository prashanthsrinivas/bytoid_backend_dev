"""Central rule metadata — the single source of truth for every rule_id.

Dependency-free (stdlib only) so it vendors into the Lambda bundle. For each
rule across ALL domains (Security Groups + IAM/Network/Data/Compute/Logging) it
carries: domain, category, default severity, the priority-queue inputs
(exploitability / blast_radius / ease-of-fix effort), the remediation guidance,
and the CIS control codes it maps to. The deterministic engines still set each
finding's actual severity; this is the canonical per-rule reference used by the
priority queue, the compliance layer, and the AI/deterministic remediation text.
"""

from __future__ import annotations

from sg_audit.schema import (
    CAT_ACCESS_CONTROL,
    CAT_DATA_EXPOSURE,
    CAT_EGRESS,
    CAT_ENCRYPTION,
    CAT_HYGIENE,
    CAT_IDENTITY,
    CAT_LOGGING,
    CAT_MONITORING,
    CAT_NETWORK_EXPOSURE,
    CAT_PATCH_MANAGEMENT,
    CAT_PUBLIC_ACCESS,
    DOMAIN_COMPUTE,
    DOMAIN_CONTAINERS,
    DOMAIN_DATA,
    DOMAIN_DEVOPS,
    DOMAIN_EXTERNAL,
    DOMAIN_IAM,
    DOMAIN_K8S,
    DOMAIN_LOGGING,
    DOMAIN_NETWORK,
    DOMAIN_SECURITY_GROUPS,
    DOMAIN_VCS,
    SEV_CRITICAL,
    SEV_HIGH,
    SEV_INFO,
    SEV_LOW,
    SEV_MEDIUM,
)

# --- New-domain rule id constants (SG ids live in schema.py) -----------------
# IAM
IAM_ADMIN_ACCESS = "IAM_ADMIN_ACCESS"
IAM_WILDCARD_POLICY = "IAM_WILDCARD_POLICY"
IAM_USER_NO_MFA = "IAM_USER_NO_MFA"
IAM_ROOT_NO_MFA = "IAM_ROOT_NO_MFA"
IAM_ROOT_HAS_KEYS = "IAM_ROOT_HAS_KEYS"
IAM_UNUSED_CREDENTIAL = "IAM_UNUSED_CREDENTIAL"
IAM_STALE_ACCESS_KEY = "IAM_STALE_ACCESS_KEY"
IAM_CROSS_ACCOUNT_TRUST_WILDCARD = "IAM_CROSS_ACCOUNT_TRUST_WILDCARD"
IAM_NO_PASSWORD_POLICY = "IAM_NO_PASSWORD_POLICY"  # noqa: S105 (rule id, not a secret)
IAM_GITHUB_OIDC_TRUST_WILDCARD = "IAM_GITHUB_OIDC_TRUST_WILDCARD"
# Source control (GitHub) + in-cluster Kubernetes (Phase 3)
VCS_PUBLIC_REPO = "VCS_PUBLIC_REPO"
VCS_NO_BRANCH_PROTECTION = "VCS_NO_BRANCH_PROTECTION"
VCS_ACTIONS_WRITE_DEFAULT = "VCS_ACTIONS_WRITE_DEFAULT"
VCS_SECRET_SCANNING_ALERT = "VCS_SECRET_SCANNING_ALERT"  # noqa: S105 (rule id)
K8S_CLUSTER_ADMIN_BINDING = "K8S_CLUSTER_ADMIN_BINDING"
K8S_PRIVILEGED_POD = "K8S_PRIVILEGED_POD"
K8S_HOST_NAMESPACE_POD = "K8S_HOST_NAMESPACE_POD"
K8S_DASHBOARD_EXPOSED = "K8S_DASHBOARD_EXPOSED"
# Network
NET_ROUTE_OPEN_TO_IGW = "NET_ROUTE_OPEN_TO_IGW"
NET_PUBLIC_SUBNET = "NET_PUBLIC_SUBNET"
NET_SUBNET_AUTO_PUBLIC_IP = "NET_SUBNET_AUTO_PUBLIC_IP"
NET_DEFAULT_VPC_PRESENT = "NET_DEFAULT_VPC_PRESENT"
NET_PEERING_CROSS_ACCOUNT = "NET_PEERING_CROSS_ACCOUNT"
# Data
S3_PUBLIC_ACL = "S3_PUBLIC_ACL"
S3_PUBLIC_POLICY = "S3_PUBLIC_POLICY"
S3_NO_PUBLIC_ACCESS_BLOCK = "S3_NO_PUBLIC_ACCESS_BLOCK"
S3_NO_ENCRYPTION = "S3_NO_ENCRYPTION"
RDS_PUBLIC = "RDS_PUBLIC"
RDS_UNENCRYPTED = "RDS_UNENCRYPTED"
RDS_SNAPSHOT_PUBLIC = "RDS_SNAPSHOT_PUBLIC"
SECRET_NO_ROTATION = "SECRET_NO_ROTATION"  # noqa: S105 (rule id, not a secret)
# Compute
EC2_PUBLIC_IP = "EC2_PUBLIC_IP"
EC2_IMDSV1_ENABLED = "EC2_IMDSV1_ENABLED"
EC2_PUBLIC_AMI = "EC2_PUBLIC_AMI"
EC2_NOT_SSM_MANAGED = "EC2_NOT_SSM_MANAGED"
EC2_OPEN_MGMT_PORT = "EC2_OPEN_MGMT_PORT"
# Logging
LOG_NO_CLOUDTRAIL = "LOG_NO_CLOUDTRAIL"
LOG_TRAIL_NOT_MULTIREGION = "LOG_TRAIL_NOT_MULTIREGION"
LOG_NO_LOG_VALIDATION = "LOG_NO_LOG_VALIDATION"
LOG_FLOW_LOGS_MISSING = "LOG_FLOW_LOGS_MISSING"
LOG_SHORT_RETENTION = "LOG_SHORT_RETENTION"
LOG_NO_CONFIG_RECORDER = "LOG_NO_CONFIG_RECORDER"
# External attack surface
EXT_INTERNET_FACING_LB = "EXT_INTERNET_FACING_LB"
EXT_PUBLIC_API = "EXT_PUBLIC_API"
EXT_PUBLIC_API_NO_AUTH = "EXT_PUBLIC_API_NO_AUTH"
# Containers / EKS
EKS_PUBLIC_ENDPOINT = "EKS_PUBLIC_ENDPOINT"
EKS_PUBLIC_ENDPOINT_WORLD = "EKS_PUBLIC_ENDPOINT_WORLD"
EKS_NO_SECRETS_ENCRYPTION = "EKS_NO_SECRETS_ENCRYPTION"
EKS_NO_CONTROL_PLANE_LOGGING = "EKS_NO_CONTROL_PLANE_LOGGING"
# CI/CD & DevOps
CICD_CODEBUILD_PLAINTEXT_SECRET = "CICD_CODEBUILD_PLAINTEXT_SECRET"  # noqa: S105 (rule id)
CICD_CODEBUILD_PRIVILEGED = "CICD_CODEBUILD_PRIVILEGED"
CICD_TFSTATE_PUBLIC = "CICD_TFSTATE_PUBLIC"


def _m(domain, category, severity, exploitability, blast_radius, effort, label, remediation, cis):
    return {
        "domain": domain,
        "category": category,
        "severity": severity,
        "exploitability": exploitability,  # 0-3
        "blast_radius": blast_radius,      # 0-3
        "effort": effort,                  # low | medium | high (ease of fix)
        "label": label,
        "remediation": remediation,
        "cis": list(cis),                  # CIS AWS Foundations control codes
        "soc2": [],                        # Phase 2
        "iso": [],                         # Phase 2
    }


RULE_META: dict[str, dict] = {
    # ---------------- Security Groups (severity matches analysis/rules.py) ----
    "SG_ADMIN_WORLD_INGRESS": _m(DOMAIN_SECURITY_GROUPS, CAT_NETWORK_EXPOSURE, SEV_CRITICAL, 3, 2, "low",
        "Administrative port open to the internet",
        "Remove the 0.0.0.0/0 (and ::/0) rule; restrict the admin port to a bastion/VPN/corporate CIDR, or use SSM Session Manager instead of public SSH/RDP.", ["5.2"]),
    "SG_DB_WORLD_INGRESS": _m(DOMAIN_SECURITY_GROUPS, CAT_DATA_EXPOSURE, SEV_CRITICAL, 3, 3, "low",
        "Database port open to the internet",
        "Remove internet exposure of the database port; allow access only from the application tier's security group or a private CIDR.", ["5.2"]),
    "SG_CACHE_WORLD_INGRESS": _m(DOMAIN_SECURITY_GROUPS, CAT_DATA_EXPOSURE, SEV_CRITICAL, 3, 3, "low",
        "Cache port open to the internet",
        "Remove internet exposure of the cache port; restrict to the application security group within the VPC.", ["5.2"]),
    "SG_ALL_PORTS_WORLD": _m(DOMAIN_SECURITY_GROUPS, CAT_NETWORK_EXPOSURE, SEV_CRITICAL, 3, 3, "medium",
        "All ports/protocols open to the internet",
        "Replace the all-ports/all-protocols rule with explicit least-privilege rules for only the ports the workload needs.", ["5.2"]),
    "SG_WIDE_RANGE_WORLD": _m(DOMAIN_SECURITY_GROUPS, CAT_NETWORK_EXPOSURE, SEV_HIGH, 2, 2, "medium",
        "Wide port range open to the internet",
        "Narrow the port range to the specific ports required and scope the source away from 0.0.0.0/0.", ["5.2"]),
    "SG_SENSITIVE_NON_ADMIN_WORLD": _m(DOMAIN_SECURITY_GROUPS, CAT_NETWORK_EXPOSURE, SEV_HIGH, 2, 2, "low",
        "Sensitive service port open to the internet",
        "Restrict the sensitive service port to known internal sources; put the service behind a load balancer or VPN.", ["5.2"]),
    "SG_NON_WORLD_ADMIN_OPEN_WIDE": _m(DOMAIN_SECURITY_GROUPS, CAT_ACCESS_CONTROL, SEV_HIGH, 2, 2, "low",
        "Administrative port open to a wide public range",
        "Tighten the source CIDR for the admin port to specific known addresses (a /32 or small range).", ["5.2"]),
    "SG_DEFAULT_SG_HAS_RULES": _m(DOMAIN_SECURITY_GROUPS, CAT_HYGIENE, SEV_MEDIUM, 1, 1, "low",
        "Default security group has non-default rules",
        "Remove all CIDR-based rules from the default security group (CIS 5.4); it should deny all and never be used directly.", ["5.4"]),
    "SG_DEFAULT_SG_IN_USE": _m(DOMAIN_SECURITY_GROUPS, CAT_HYGIENE, SEV_MEDIUM, 1, 2, "high",
        "Default security group is attached to resources",
        "Move resources onto purpose-built least-privilege groups, then strip the default SG's rules.", ["5.4"]),
    "SG_BROAD_INTERNAL_CIDR": _m(DOMAIN_SECURITY_GROUPS, CAT_ACCESS_CONTROL, SEV_MEDIUM, 1, 2, "medium",
        "Sensitive port open to a broad internal range",
        "Narrow the internal source range to the specific subnet/security group that needs access.", []),
    "SG_BROAD_EGRESS_ALL": _m(DOMAIN_SECURITY_GROUPS, CAT_EGRESS, SEV_MEDIUM, 1, 2, "medium",
        "Unrestricted egress to all destinations",
        "Replace the allow-all egress rule with explicit destinations/ports the workload requires.", []),
    "SG_UNUSED": _m(DOMAIN_SECURITY_GROUPS, CAT_HYGIENE, SEV_LOW, 0, 0, "low",
        "Unused security group (no attachments)",
        "Delete the unused security group to reduce sprawl.", []),
    "SG_ICMP_WORLD": _m(DOMAIN_SECURITY_GROUPS, CAT_NETWORK_EXPOSURE, SEV_LOW, 1, 0, "low",
        "ICMP open to the internet",
        "Restrict ICMP to internal ranges or remove it; allow only required ICMP types.", []),
    "SG_MISSING_RULE_DESCRIPTION": _m(DOMAIN_SECURITY_GROUPS, CAT_HYGIENE, SEV_INFO, 0, 0, "low",
        "Security group rule missing a description",
        "Add a description to each rule documenting its purpose and owner.", []),

    # ---------------- IAM -----------------------------------------------------
    IAM_ADMIN_ACCESS: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_HIGH, 2, 3, "medium",
        "Principal has administrator (full) access",
        "Replace AdministratorAccess with least-privilege policies scoped to the principal's actual needs; reserve admin for break-glass roles.", ["1.16"]),
    IAM_WILDCARD_POLICY: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_HIGH, 2, 3, "medium",
        "Policy grants Action:* on Resource:*",
        "Scope the policy to specific actions and resource ARNs; remove the \"*\":\"*\" statement.", ["1.16"]),
    IAM_USER_NO_MFA: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_HIGH, 3, 2, "low",
        "Console user without MFA",
        "Enforce MFA for every IAM user with console access (and via an IAM policy condition aws:MultiFactorAuthPresent).", ["1.10"]),
    IAM_ROOT_NO_MFA: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_CRITICAL, 3, 3, "low",
        "Root account has no MFA",
        "Enable hardware/virtual MFA on the root account immediately.", ["1.5"]),
    IAM_ROOT_HAS_KEYS: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_CRITICAL, 3, 3, "low",
        "Root account has active access keys",
        "Delete the root account access keys; use IAM roles/users for programmatic access.", ["1.4"]),
    IAM_UNUSED_CREDENTIAL: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_MEDIUM, 1, 1, "low",
        "Credential unused for an extended period",
        "Disable or delete credentials with no activity (>90 days) to shrink the attack surface.", ["1.12"]),
    IAM_STALE_ACCESS_KEY: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_MEDIUM, 1, 1, "low",
        "Access key not rotated",
        "Rotate access keys at least every 90 days; remove unused keys.", ["1.14"]),
    IAM_CROSS_ACCOUNT_TRUST_WILDCARD: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_HIGH, 3, 3, "medium",
        "Role trusts a wildcard or external principal without conditions",
        "Restrict the role trust policy Principal to specific account IDs/roles and require an ExternalId/condition for third-party access.", []),
    IAM_NO_PASSWORD_POLICY: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_MEDIUM, 1, 1, "low",
        "Weak or missing account password policy",
        "Set an account password policy (length >=14, complexity, reuse prevention, expiry).", ["1.8", "1.9"]),
    IAM_GITHUB_OIDC_TRUST_WILDCARD: _m(DOMAIN_IAM, CAT_IDENTITY, SEV_HIGH, 3, 3, "low",
        "IAM role trusts GitHub Actions OIDC without a repo/sub condition",
        "Add a StringLike condition on token.actions.githubusercontent.com:sub restricting the role to specific repos/branches; a missing or '*' sub lets any GitHub repo assume the role.", []),

    # ---------------- Network -------------------------------------------------
    NET_ROUTE_OPEN_TO_IGW: _m(DOMAIN_NETWORK, CAT_NETWORK_EXPOSURE, SEV_LOW, 1, 1, "medium",
        "Route table sends 0.0.0.0/0 to an internet gateway",
        "Confirm the subnet is intended to be public; private subnets should route egress via a NAT gateway, not an IGW.", []),
    NET_PUBLIC_SUBNET: _m(DOMAIN_NETWORK, CAT_PUBLIC_ACCESS, SEV_MEDIUM, 1, 2, "medium",
        "Subnet is internet-facing (routes to IGW)",
        "Place application/database tiers in private subnets; keep only load balancers / bastions in public subnets.", []),
    NET_SUBNET_AUTO_PUBLIC_IP: _m(DOMAIN_NETWORK, CAT_PUBLIC_ACCESS, SEV_MEDIUM, 1, 2, "low",
        "Subnet auto-assigns public IPs on launch",
        "Disable MapPublicIpOnLaunch unless the subnet is explicitly a public tier.", []),
    NET_DEFAULT_VPC_PRESENT: _m(DOMAIN_NETWORK, CAT_HYGIENE, SEV_LOW, 0, 1, "medium",
        "Default VPC is present/in use",
        "Avoid using the default VPC; deploy into purpose-built VPCs with controlled routing.", []),
    NET_PEERING_CROSS_ACCOUNT: _m(DOMAIN_NETWORK, CAT_ACCESS_CONTROL, SEV_MEDIUM, 1, 2, "medium",
        "VPC peering connection to an external account",
        "Confirm the peered account is trusted and scope route tables/SGs so peering only exposes intended subnets/ports.", []),

    # ---------------- Data & Storage -----------------------------------------
    S3_PUBLIC_ACL: _m(DOMAIN_DATA, CAT_PUBLIC_ACCESS, SEV_CRITICAL, 3, 3, "low",
        "S3 bucket ACL grants public access",
        "Remove AllUsers/AuthenticatedUsers grants and enable S3 Block Public Access on the bucket and account.", ["2.1.5"]),
    S3_PUBLIC_POLICY: _m(DOMAIN_DATA, CAT_PUBLIC_ACCESS, SEV_CRITICAL, 3, 3, "low",
        "S3 bucket policy allows public access",
        "Remove the public Principal:* statement from the bucket policy and enable Block Public Access.", ["2.1.5"]),
    S3_NO_PUBLIC_ACCESS_BLOCK: _m(DOMAIN_DATA, CAT_PUBLIC_ACCESS, SEV_HIGH, 2, 3, "low",
        "S3 Block Public Access not fully enabled",
        "Enable all four Block Public Access settings at the account level and per bucket.", ["2.1.5"]),
    S3_NO_ENCRYPTION: _m(DOMAIN_DATA, CAT_ENCRYPTION, SEV_MEDIUM, 1, 2, "low",
        "S3 bucket has no default encryption",
        "Enable default SSE-S3 or SSE-KMS encryption on the bucket.", ["2.1.1"]),
    RDS_PUBLIC: _m(DOMAIN_DATA, CAT_PUBLIC_ACCESS, SEV_CRITICAL, 3, 3, "low",
        "RDS instance is publicly accessible",
        "Set PubliclyAccessible=false and place the instance in private subnets reachable only from the app tier.", ["2.3.3"]),
    RDS_UNENCRYPTED: _m(DOMAIN_DATA, CAT_ENCRYPTION, SEV_HIGH, 1, 3, "high",
        "RDS storage is not encrypted",
        "Enable storage encryption (requires recreating/restoring the instance from an encrypted snapshot).", ["2.3.1"]),
    RDS_SNAPSHOT_PUBLIC: _m(DOMAIN_DATA, CAT_PUBLIC_ACCESS, SEV_CRITICAL, 3, 3, "low",
        "RDS snapshot is shared publicly",
        "Remove the 'all' restore attribute from the snapshot so it is private.", []),
    SECRET_NO_ROTATION: _m(DOMAIN_DATA, CAT_HYGIENE, SEV_LOW, 1, 1, "medium",
        "Secret does not have rotation enabled",
        "Enable automatic rotation on the secret with an appropriate rotation Lambda/schedule.", []),

    # ---------------- Compute -------------------------------------------------
    EC2_PUBLIC_IP: _m(DOMAIN_COMPUTE, CAT_PUBLIC_ACCESS, SEV_MEDIUM, 1, 2, "medium",
        "EC2 instance has a public IP",
        "Remove the public IP where not required; front the instance with a load balancer and keep it in a private subnet.", []),
    EC2_IMDSV1_ENABLED: _m(DOMAIN_COMPUTE, CAT_ACCESS_CONTROL, SEV_HIGH, 2, 2, "low",
        "Instance metadata service v1 (IMDSv1) allowed",
        "Require IMDSv2 (set MetadataOptions HttpTokens=required) to mitigate SSRF credential theft.", ["5.6"]),
    EC2_PUBLIC_AMI: _m(DOMAIN_COMPUTE, CAT_HYGIENE, SEV_MEDIUM, 1, 1, "medium",
        "Instance launched from a public AMI",
        "Use vetted, private golden AMIs; verify the AMI source and patch level.", []),
    EC2_NOT_SSM_MANAGED: _m(DOMAIN_COMPUTE, CAT_PATCH_MANAGEMENT, SEV_LOW, 0, 1, "medium",
        "Instance is not managed by SSM (patch posture unknown)",
        "Install/enable the SSM agent and enroll in Patch Manager so patch compliance is visible and enforced.", []),
    EC2_OPEN_MGMT_PORT: _m(DOMAIN_COMPUTE, CAT_NETWORK_EXPOSURE, SEV_CRITICAL, 3, 2, "low",
        "Public instance with an administrative port open to the internet",
        "Close the public admin port at the security group; use SSM Session Manager or a bastion for access.", ["5.2"]),

    # ---------------- Logging & Monitoring ------------------------------------
    LOG_NO_CLOUDTRAIL: _m(DOMAIN_LOGGING, CAT_LOGGING, SEV_HIGH, 1, 3, "low",
        "No enabled CloudTrail trail",
        "Create a multi-region CloudTrail trail capturing management events, delivering to a protected S3 bucket.", ["3.1"]),
    LOG_TRAIL_NOT_MULTIREGION: _m(DOMAIN_LOGGING, CAT_LOGGING, SEV_MEDIUM, 1, 2, "low",
        "CloudTrail trail is not multi-region",
        "Enable IsMultiRegionTrail so activity in every region is captured.", ["3.1"]),
    LOG_NO_LOG_VALIDATION: _m(DOMAIN_LOGGING, CAT_LOGGING, SEV_LOW, 0, 1, "low",
        "CloudTrail log file validation disabled",
        "Enable log file validation to detect tampering of delivered logs.", ["3.2"]),
    LOG_FLOW_LOGS_MISSING: _m(DOMAIN_LOGGING, CAT_LOGGING, SEV_MEDIUM, 1, 2, "low",
        "VPC has no flow logs",
        "Enable VPC Flow Logs (ALL traffic) to CloudWatch Logs or S3 for network forensics.", ["3.9"]),
    LOG_SHORT_RETENTION: _m(DOMAIN_LOGGING, CAT_MONITORING, SEV_LOW, 0, 1, "low",
        "Log group has short/no retention",
        "Set a retention period (e.g. >=365 days) on the log group to preserve audit history.", []),
    LOG_NO_CONFIG_RECORDER: _m(DOMAIN_LOGGING, CAT_MONITORING, SEV_MEDIUM, 1, 2, "medium",
        "AWS Config recorder is not enabled",
        "Enable AWS Config in all regions to record resource configuration changes.", ["3.5"]),

    # ---------------- External attack surface ---------------------------------
    EXT_INTERNET_FACING_LB: _m(DOMAIN_EXTERNAL, CAT_NETWORK_EXPOSURE, SEV_LOW, 1, 1, "medium",
        "Internet-facing load balancer",
        "Confirm the load balancer must be public; restrict its listeners/security groups and put a WAF in front of public HTTP(S).", []),
    EXT_PUBLIC_API: _m(DOMAIN_EXTERNAL, CAT_PUBLIC_ACCESS, SEV_LOW, 1, 1, "medium",
        "Public API Gateway endpoint",
        "Confirm the API must be public; consider a private API or restrict via resource policy / WAF.", []),
    EXT_PUBLIC_API_NO_AUTH: _m(DOMAIN_EXTERNAL, CAT_ACCESS_CONTROL, SEV_HIGH, 3, 2, "low",
        "Public API method has no authorization",
        "Attach an authorizer (IAM/Cognito/Lambda) or API key + usage plan to the public API method.", []),

    # ---------------- Containers / EKS ----------------------------------------
    EKS_PUBLIC_ENDPOINT: _m(DOMAIN_CONTAINERS, CAT_NETWORK_EXPOSURE, SEV_MEDIUM, 1, 2, "medium",
        "EKS cluster API endpoint is publicly accessible",
        "Disable public endpoint access (use private + VPN/PrivateLink) or restrict publicAccessCidrs to known ranges.", []),
    EKS_PUBLIC_ENDPOINT_WORLD: _m(DOMAIN_CONTAINERS, CAT_NETWORK_EXPOSURE, SEV_HIGH, 2, 3, "low",
        "EKS public API endpoint is open to 0.0.0.0/0",
        "Set publicAccessCidrs to specific admin ranges, or disable public endpoint access entirely.", []),
    EKS_NO_SECRETS_ENCRYPTION: _m(DOMAIN_CONTAINERS, CAT_ENCRYPTION, SEV_MEDIUM, 1, 2, "high",
        "EKS secrets envelope encryption (KMS) not configured",
        "Enable envelope encryption of Kubernetes secrets with a KMS key on the cluster.", []),
    EKS_NO_CONTROL_PLANE_LOGGING: _m(DOMAIN_CONTAINERS, CAT_LOGGING, SEV_LOW, 0, 1, "low",
        "EKS control-plane logging is disabled",
        "Enable api/audit/authenticator control-plane log types to CloudWatch Logs.", []),

    # ---------------- CI/CD & DevOps ------------------------------------------
    CICD_CODEBUILD_PLAINTEXT_SECRET: _m(DOMAIN_DEVOPS, CAT_DATA_EXPOSURE, SEV_HIGH, 2, 2, "low",
        "CodeBuild project stores a secret in a plaintext environment variable",
        "Move the value to Secrets Manager / SSM Parameter Store (SecureString) and reference it as a SECRETS_MANAGER/PARAMETER_STORE env var type.", []),
    CICD_CODEBUILD_PRIVILEGED: _m(DOMAIN_DEVOPS, CAT_ACCESS_CONTROL, SEV_MEDIUM, 1, 2, "low",
        "CodeBuild project runs in privileged mode",
        "Disable privilegedMode unless Docker-in-Docker is required; if needed, isolate the project and scope its role tightly.", []),
    CICD_TFSTATE_PUBLIC: _m(DOMAIN_DEVOPS, CAT_DATA_EXPOSURE, SEV_CRITICAL, 3, 3, "low",
        "Terraform state bucket is publicly accessible",
        "Terraform state contains secrets/resource detail — make the bucket private, enable Block Public Access, and encrypt + version it.", ["2.1.5"]),

    # ---------------- Source control (GitHub) ---------------------------------
    VCS_PUBLIC_REPO: _m(DOMAIN_VCS, CAT_PUBLIC_ACCESS, SEV_MEDIUM, 1, 2, "low",
        "Repository is public",
        "Confirm the repository is intended to be public; make it private if it holds proprietary code or config.", []),
    VCS_NO_BRANCH_PROTECTION: _m(DOMAIN_VCS, CAT_ACCESS_CONTROL, SEV_MEDIUM, 1, 2, "low",
        "Default branch has no protection rule",
        "Enable branch protection on the default branch (required reviews, status checks, no force-push).", []),
    VCS_ACTIONS_WRITE_DEFAULT: _m(DOMAIN_VCS, CAT_ACCESS_CONTROL, SEV_MEDIUM, 2, 2, "low",
        "GitHub Actions default token has write permissions",
        "Set default workflow permissions to read-only and grant write per-workflow/job only where needed.", []),
    VCS_SECRET_SCANNING_ALERT: _m(DOMAIN_VCS, CAT_DATA_EXPOSURE, SEV_HIGH, 3, 3, "low",
        "Open secret-scanning alert in a repository",
        "Revoke/rotate the leaked secret immediately and remove it from history; resolve the secret-scanning alert.", []),

    # ---------------- Kubernetes workloads (in-cluster) -----------------------
    K8S_CLUSTER_ADMIN_BINDING: _m(DOMAIN_K8S, CAT_ACCESS_CONTROL, SEV_HIGH, 2, 3, "medium",
        "cluster-admin bound to a broad subject",
        "Replace cluster-admin bindings to groups like system:authenticated/anonymous (or wide users) with least-privilege roles.", []),
    K8S_PRIVILEGED_POD: _m(DOMAIN_K8S, CAT_ACCESS_CONTROL, SEV_HIGH, 2, 3, "medium",
        "Privileged container running in the cluster",
        "Remove privileged: true; use specific capabilities and a restrictive Pod Security Standard.", []),
    K8S_HOST_NAMESPACE_POD: _m(DOMAIN_K8S, CAT_ACCESS_CONTROL, SEV_MEDIUM, 2, 2, "medium",
        "Pod uses host network/PID/IPC namespace",
        "Disable hostNetwork/hostPID/hostIPC unless strictly required; they break container isolation.", []),
    K8S_DASHBOARD_EXPOSED: _m(DOMAIN_K8S, CAT_PUBLIC_ACCESS, SEV_HIGH, 3, 3, "low",
        "Kubernetes dashboard exposed via a LoadBalancer service",
        "Remove the public LoadBalancer; access the dashboard via kubectl proxy or an authenticated ingress.", []),
}


# --- accessors (safe defaults so unknown ids never crash) --------------------
def meta(rule_id: str) -> dict:
    return RULE_META.get(rule_id, {})


def rule_label(rule_id: str) -> str:
    return RULE_META.get(rule_id, {}).get("label", rule_id)


def remediation_for(rule_id: str) -> str:
    return RULE_META.get(rule_id, {}).get("remediation", "")


def domain_for(rule_id: str, default: str = DOMAIN_SECURITY_GROUPS) -> str:
    return RULE_META.get(rule_id, {}).get("domain", default)


def cis_for(rule_id: str) -> list[str]:
    return RULE_META.get(rule_id, {}).get("cis", [])


def effort_for(rule_id: str, default: str = "medium") -> str:
    return RULE_META.get(rule_id, {}).get("effort", default)


def priority_inputs(rule_id: str) -> tuple[int, int]:
    """(exploitability, blast_radius), each 0-3; defaults to (1, 1)."""
    m = RULE_META.get(rule_id, {})
    return int(m.get("exploitability", 1)), int(m.get("blast_radius", 1))
