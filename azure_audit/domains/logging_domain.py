"""Logging & Monitoring domain — Activity Log diagnostics + Defender plans.

``analyze_diagnostic_settings`` flags a subscription with no Activity Log
diagnostic setting; ``analyze_defender_pricings`` flags Defender for Cloud plans
not on the Standard tier. Named ``logging_domain`` to avoid shadowing stdlib
``logging``.
"""

from __future__ import annotations

from azure_audit.metadata import RULE_META
from cspm_core.normalize import make_domain_finding

_DIAG_API = "2021-05-01-preview"
_PRICING_API = "2023-01-01"


def analyze_diagnostic_settings(settings, subscription_id, subscription_name="") -> list:
    if settings:
        return []
    return [make_domain_finding(
        rule_meta=RULE_META, rule_id="AZ_NO_DIAGNOSTIC_SETTINGS", severity="medium",
        finding_summary=f"Subscription {subscription_name or subscription_id} has no Activity Log diagnostic setting",
        scope_id=subscription_id, scope_name=subscription_name,
        entity_type="subscription", entity_id=subscription_id, entity_name=subscription_name or subscription_id,
        source="azure")]


def analyze_defender_pricings(pricings, subscription_id, subscription_name="") -> list:
    findings = []
    for plan in pricings or []:
        props = plan.get("properties", {}) or {}
        if props.get("pricingTier") and props["pricingTier"] != "Standard":
            name = plan.get("name", "")
            findings.append(make_domain_finding(
                rule_meta=RULE_META, rule_id="AZ_DEFENDER_PLAN_OFF", severity="medium",
                finding_summary=f"Defender for Cloud plan '{name}' is on the {props['pricingTier']} tier (not Standard)",
                scope_id=subscription_id, scope_name=subscription_name,
                entity_type="defender_plan", entity_id=plan.get("id") or name, entity_name=name,
                source="azure", details={"pricing_tier": props.get("pricingTier")}))
    return findings


def collect(creds, subscription_id, subscription_name="") -> list:
    from azure_audit.rest import arm_list
    findings = []
    diag = arm_list(creds, f"/subscriptions/{subscription_id}/providers/Microsoft.Insights/diagnosticSettings", _DIAG_API)
    findings += analyze_diagnostic_settings(diag, subscription_id, subscription_name)
    pricings = arm_list(creds, f"/subscriptions/{subscription_id}/providers/Microsoft.Security/pricings", _PRICING_API)
    findings += analyze_defender_pricings(pricings, subscription_id, subscription_name)
    return findings
