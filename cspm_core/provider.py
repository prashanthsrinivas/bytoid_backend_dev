"""The Provider descriptor — what each cloud plugs into the engine.

A provider is a plain object carrying its rule metadata + a few callables. The
engine never imports a cloud SDK; it only calls these. Construct one module-level
``Provider`` per cloud (e.g. ``azure_audit.provider.AZURE_PROVIDER``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Provider:
    key: str                       # "azure" | "gcp"
    label: str                     # "Azure" | "GCP"
    route_prefix: str              # "azure-audit" (paths become /azure-audit/...)
    s3_namespace: str              # "azure_audit" (S3 prefix {user}/azure_audit/...)
    redis_namespace: str           # "azure_audit" (Redis key prefix)

    domains: tuple                 # ("network", "identity", ...)
    domain_labels: dict            # domain -> human label
    rule_meta: dict                # rule_id -> {domain, category, severity, exploitability,
                                   #             blast_radius, effort, label, remediation, cis, soc2, iso}
    cis_label: str                 # "CIS Microsoft Azure Foundations"
    cis_families: dict             # control-prefix -> family label

    # Permission keys (must be registered in utils/permission_metadata.py).
    perms: dict                    # {create, findings_read, dashboard_read, recommend, remediation}

    # Credential + collection (engine calls these; clouds implement them).
    resolve_credentials: Callable  # (user_id) -> creds dict | None
    enumerate_scopes: Callable     # (creds, scope_filter) -> [ {id, name, ...}, ... ]
    collect: Callable              # (creds, scope, domains) -> (findings, status_dict)

    fixers: dict = field(default_factory=dict)   # rule_id -> fix(session_or_creds, finding, dry_run)
    auto_remediate_enabled: Callable | None = None  # () -> bool

    scope_label: str = "scope"     # "subscription" | "project"
    default_audit_name: str = "Cloud Security Posture Audit"
    default_role_hint: str = ""    # ops note shown after audit creation

    # accessors -------------------------------------------------------------
    def meta(self, rule_id: str) -> dict:
        return self.rule_meta.get(rule_id, {})

    def rule_label(self, rule_id: str) -> str:
        return self.rule_meta.get(rule_id, {}).get("label", rule_id)

    def remediation_for(self, rule_id: str) -> str:
        return self.rule_meta.get(rule_id, {}).get("remediation", "")

    def effort_for(self, rule_id: str, default: str = "medium") -> str:
        return self.rule_meta.get(rule_id, {}).get("effort", default)

    def cis_for(self, rule_id: str) -> list:
        return self.rule_meta.get(rule_id, {}).get("cis", [])

    def priority_inputs(self, rule_id: str):
        m = self.rule_meta.get(rule_id, {})
        return int(m.get("exploitability", 1)), int(m.get("blast_radius", 1))
