"""GCP rule metadata + CIS Google Cloud Platform Foundation mapping.

``RULE_META`` is the single source the engine reads. SOC 2 / ISO derive from the
finding category in ``cspm_core.compliance``, so they stay empty here.
"""

from __future__ import annotations

CIS_LABEL = "CIS Google Cloud Platform Foundation"

CIS_FAMILIES = {
    "1": "Identity & Access Management",
    "2": "Logging & Monitoring",
    "3": "Networking",
    "4": "Virtual Machines",
    "5": "Cloud Storage",
    "6": "Cloud SQL",
    "7": "BigQuery",
}

DOMAINS = ("network", "iam", "data", "compute", "logging")
DOMAIN_LABELS = {
    "network": "VPC Firewall & Network",
    "iam": "Cloud IAM",
    "data": "Storage & Cloud SQL",
    "compute": "Compute Engine",
    "logging": "Logging & Monitoring",
}


def _m(domain, category, severity, expl, blast, effort, label, remediation, cis):
    return {"domain": domain, "category": category, "severity": severity,
            "exploitability": expl, "blast_radius": blast, "effort": effort,
            "label": label, "remediation": remediation, "cis": cis, "soc2": [], "iso": []}


RULE_META = {
    # ── network (VPC firewall) ─────────────────────────────────────────────
    "GCP_FW_ADMIN_WORLD_OPEN": _m(
        "network", "network_exposure", "critical", 3, 3, "low",
        "Firewall allows admin port from 0.0.0.0/0",
        "Restrict the ingress firewall rule sourceRanges to known CIDRs; use IAP for SSH/RDP.",
        ["3.6", "3.7"]),
    "GCP_FW_DB_WORLD_OPEN": _m(
        "network", "network_exposure", "critical", 3, 3, "low",
        "Firewall allows a database port from 0.0.0.0/0",
        "Remove the 0.0.0.0/0 source; expose databases only via private IP / peered VPCs.",
        ["3.6", "3.7"]),
    "GCP_FW_ALL_PORTS_WORLD": _m(
        "network", "network_exposure", "critical", 3, 3, "low",
        "Firewall allows all ports/protocols from 0.0.0.0/0",
        "Replace the allow-all ingress rule with least-privilege protocol/port rules.",
        ["3.1"]),
    "GCP_FW_SENSITIVE_WORLD_OPEN": _m(
        "network", "network_exposure", "high", 2, 2, "low",
        "Firewall allows a sensitive port from 0.0.0.0/0",
        "Restrict the ingress firewall rule sourceRanges to trusted CIDRs.",
        ["3.6"]),
    "GCP_DEFAULT_NETWORK": _m(
        "network", "hygiene", "medium", 1, 2, "high",
        "Default network present in project",
        "Delete the default network and create custom VPCs with explicit firewall rules.",
        ["3.1"]),
    # ── iam ────────────────────────────────────────────────────────────────
    "GCP_IAM_PUBLIC_MEMBER": _m(
        "iam", "access_control", "critical", 3, 3, "low",
        "IAM binding grants access to allUsers / allAuthenticatedUsers",
        "Remove allUsers / allAuthenticatedUsers from the project IAM policy bindings.",
        ["1.1"]),
    "GCP_IAM_PRIMITIVE_ROLE": _m(
        "iam", "access_control", "medium", 2, 3, "medium",
        "Primitive role (Owner/Editor) assigned to a user",
        "Replace primitive roles with predefined or custom least-privilege roles.",
        ["1.5"]),
    "GCP_SA_USER_MANAGED_KEY_STALE": _m(
        "iam", "identity", "medium", 2, 2, "medium",
        "User-managed service-account key not rotated",
        "Rotate user-managed service-account keys at least every 90 days; prefer workload identity.",
        ["1.4"]),
    # ── data ───────────────────────────────────────────────────────────────
    "GCP_BUCKET_PUBLIC": _m(
        "data", "public_access", "high", 3, 2, "low",
        "Cloud Storage bucket is publicly accessible",
        "Remove allUsers / allAuthenticatedUsers from the bucket IAM policy.",
        ["5.1", "5.2"]),
    "GCP_BUCKET_NO_UNIFORM_ACCESS": _m(
        "data", "access_control", "low", 1, 1, "low",
        "Bucket uniform bucket-level access is disabled",
        "Enable uniform bucket-level access to centralize permissions on IAM.",
        ["5.2"]),
    "GCP_SQL_PUBLIC_IP": _m(
        "data", "network_exposure", "high", 3, 2, "low",
        "Cloud SQL instance has a public IPv4 address",
        "Disable the public IP and connect via private IP / the Cloud SQL Auth proxy.",
        ["6.5"]),
    "GCP_SQL_NO_SSL": _m(
        "data", "encryption", "medium", 2, 2, "low",
        "Cloud SQL instance does not require SSL",
        "Set the instance to require SSL/TLS for all connections.",
        ["6.4"]),
    # ── compute ────────────────────────────────────────────────────────────
    "GCP_INSTANCE_PUBLIC_IP": _m(
        "compute", "network_exposure", "medium", 2, 2, "medium",
        "Compute instance has an external IP",
        "Remove the external IP and use Cloud NAT / IAP for egress and access.",
        ["4.9"]),
    "GCP_OS_LOGIN_DISABLED": _m(
        "compute", "access_control", "medium", 1, 2, "medium",
        "OS Login is not enabled on the instance",
        "Enable OS Login (enable-oslogin=TRUE) project- or instance-wide.",
        ["4.4"]),
    "GCP_DEFAULT_SA_FULL_SCOPE": _m(
        "compute", "identity", "high", 2, 3, "medium",
        "Instance uses the default service account with full cloud-platform scope",
        "Use a dedicated least-privilege service account and restrict access scopes.",
        ["4.1", "4.2"]),
    "GCP_SHIELDED_VM_OFF": _m(
        "compute", "hygiene", "low", 1, 1, "low",
        "Shielded VM is not enabled on the instance",
        "Enable Shielded VM (secure boot, vTPM, integrity monitoring).",
        ["4.8"]),
    # ── logging ────────────────────────────────────────────────────────────
    "GCP_AUDIT_LOGGING_INCOMPLETE": _m(
        "logging", "logging", "medium", 1, 2, "medium",
        "Audit logging is not enabled for all services",
        "Configure auditConfigs with allServices and DATA_READ/DATA_WRITE/ADMIN_READ log types.",
        ["2.1"]),
    "GCP_NO_LOG_SINK": _m(
        "logging", "logging", "low", 1, 2, "medium",
        "Project has no logging sink configured",
        "Create an aggregated log sink to a secured bucket / BigQuery dataset for retention.",
        ["2.2"]),
}
