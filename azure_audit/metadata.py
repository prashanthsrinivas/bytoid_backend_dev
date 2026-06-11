"""Azure rule metadata + CIS Microsoft Azure Foundations mapping.

``RULE_META`` is the single source the engine reads: each rule_id maps to its
domain, finding category, default severity, priority inputs (exploitability /
blast_radius / effort), human label, remediation text, and CIS controls. SOC 2 /
ISO are derived from the category by ``cspm_core.compliance`` (shared maps), so
they stay empty here.
"""

from __future__ import annotations

CIS_LABEL = "CIS Microsoft Azure Foundations"

CIS_FAMILIES = {
    "1": "Identity & Access Management",
    "2": "Microsoft Defender for Cloud",
    "3": "Storage Accounts",
    "4": "Database Services",
    "5": "Logging & Monitoring",
    "6": "Networking",
    "7": "Virtual Machines",
    "8": "Key Vault",
}

DOMAINS = ("network", "identity", "data", "compute", "logging")
DOMAIN_LABELS = {
    "network": "Network Security Groups",
    "identity": "Entra ID & RBAC",
    "data": "Data Services",
    "compute": "Virtual Machines",
    "logging": "Logging & Monitoring",
}


def _m(domain, category, severity, expl, blast, effort, label, remediation, cis):
    return {"domain": domain, "category": category, "severity": severity,
            "exploitability": expl, "blast_radius": blast, "effort": effort,
            "label": label, "remediation": remediation, "cis": cis, "soc2": [], "iso": []}


RULE_META = {
    # ── network (NSG) ──────────────────────────────────────────────────────
    "AZ_NSG_ADMIN_WORLD_OPEN": _m(
        "network", "network_exposure", "critical", 3, 3, "low",
        "NSG allows admin port from the Internet",
        "Restrict the NSG inbound rule to known administrative CIDRs or use Azure Bastion / Just-in-Time access.",
        ["6.1", "6.2"]),
    "AZ_NSG_DB_WORLD_OPEN": _m(
        "network", "network_exposure", "critical", 3, 3, "low",
        "NSG allows database port from the Internet",
        "Remove the 0.0.0.0/0 / Internet source; expose databases only via private endpoints or peered VNets.",
        ["6.1", "6.2"]),
    "AZ_NSG_ALL_PORTS_WORLD": _m(
        "network", "network_exposure", "critical", 3, 3, "low",
        "NSG allows all ports from the Internet",
        "Replace the allow-all inbound rule with least-privilege port/source rules.",
        ["6.1"]),
    "AZ_NSG_SENSITIVE_WORLD_OPEN": _m(
        "network", "network_exposure", "high", 2, 2, "low",
        "NSG allows a sensitive port from the Internet",
        "Restrict the inbound rule source to trusted CIDRs.",
        ["6.1"]),
    # ── identity (Entra / RBAC) ────────────────────────────────────────────
    "AZ_RBAC_SUBSCRIPTION_OWNER": _m(
        "identity", "identity", "high", 2, 3, "medium",
        "Owner role assigned at subscription scope",
        "Grant least-privilege built-in roles; reserve Owner for break-glass identities and use PIM for just-in-time elevation.",
        ["1.23"]),
    "AZ_RBAC_CUSTOM_WILDCARD_ROLE": _m(
        "identity", "access_control", "high", 2, 3, "medium",
        "Custom RBAC role grants wildcard (*) actions",
        "Scope custom role definitions to the specific actions required; avoid '*' in Actions.",
        ["1.23"]),
    "AZ_CLASSIC_ADMIN_PRESENT": _m(
        "identity", "identity", "medium", 1, 2, "medium",
        "Classic (co-)administrator still assigned",
        "Remove classic administrators and manage access exclusively through Azure RBAC.",
        ["1.3"]),
    # ── data ───────────────────────────────────────────────────────────────
    "AZ_STORAGE_PUBLIC_BLOB": _m(
        "data", "public_access", "high", 3, 2, "low",
        "Storage account allows public blob access",
        "Set allowBlobPublicAccess=false on the storage account and audit container ACLs.",
        ["3.7"]),
    "AZ_STORAGE_NO_HTTPS": _m(
        "data", "encryption", "medium", 2, 2, "low",
        "Storage account does not enforce HTTPS-only",
        "Enable 'Secure transfer required' (supportsHttpsTrafficOnly=true).",
        ["3.1"]),
    "AZ_STORAGE_SHARED_KEY": _m(
        "data", "access_control", "medium", 2, 2, "medium",
        "Storage account permits shared-key access",
        "Set allowSharedKeyAccess=false and use Entra ID (Azure AD) authorization.",
        ["3.8"]),
    "AZ_STORAGE_PUBLIC_NETWORK": _m(
        "data", "network_exposure", "medium", 2, 2, "low",
        "Storage account network default action is Allow",
        "Set networkAcls.defaultAction=Deny and allow only trusted VNets / private endpoints.",
        ["3.7"]),
    "AZ_SQL_PUBLIC_NETWORK": _m(
        "data", "network_exposure", "high", 3, 2, "low",
        "SQL server allows public network access",
        "Set publicNetworkAccess=Disabled and use private endpoints.",
        ["4.1"]),
    "AZ_KEYVAULT_NO_PURGE_PROTECTION": _m(
        "data", "data_exposure", "medium", 1, 2, "low",
        "Key Vault has purge protection disabled",
        "Enable purge protection (enablePurgeProtection=true) and soft-delete.",
        ["8.5"]),
    "AZ_KEYVAULT_PUBLIC_NETWORK": _m(
        "data", "network_exposure", "medium", 2, 2, "low",
        "Key Vault allows public network access",
        "Set publicNetworkAccess=Disabled / networkAcls.defaultAction=Deny and use private endpoints.",
        ["8.6"]),
    # ── compute ────────────────────────────────────────────────────────────
    "AZ_PUBLIC_IP_IN_USE": _m(
        "compute", "network_exposure", "medium", 2, 2, "medium",
        "Public IP address attached to a resource",
        "Front workloads with a load balancer / Application Gateway and remove direct public IPs where possible.",
        []),
    "AZ_VM_NO_DISK_ENCRYPTION": _m(
        "compute", "encryption", "medium", 1, 2, "medium",
        "VM disk encryption / encryption-at-host not enabled",
        "Enable Azure Disk Encryption or encryption-at-host on the virtual machine.",
        ["7.2", "7.3"]),
    "AZ_VM_UNMANAGED_DISK": _m(
        "compute", "hygiene", "low", 1, 1, "high",
        "VM uses unmanaged disks",
        "Migrate the VM to managed disks for built-in encryption and lifecycle management.",
        ["7.1"]),
    # ── logging ────────────────────────────────────────────────────────────
    "AZ_NO_DIAGNOSTIC_SETTINGS": _m(
        "logging", "logging", "medium", 1, 2, "medium",
        "Subscription has no Activity Log diagnostic setting",
        "Create a diagnostic setting that ships the Activity Log to a Log Analytics workspace / storage / Event Hub.",
        ["5.1.1"]),
    "AZ_DEFENDER_PLAN_OFF": _m(
        "logging", "monitoring", "medium", 1, 2, "low",
        "Microsoft Defender for Cloud plan not on Standard tier",
        "Enable the Standard Defender for Cloud plan for the affected resource type.",
        ["2.1.1"]),
}
