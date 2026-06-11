"""Azure domain analyzer tests on synthetic ARM payloads (no network).

Run with::

    python -m azure_audit.tests.test_domains

Exits non-zero on the first failure. Also exercises the AZURE_PROVIDER through the
generic engine (build_snapshot/score/compliance) to prove the wiring.
"""

from __future__ import annotations

import sys

from azure_audit.domains import compute, data, identity, logging_domain, network
from azure_audit.metadata import RULE_META
from azure_audit.provider import AZURE_PROVIDER
from cspm_core import score
from cspm_core.compliance import coverage_for
from cspm_core.normalize import build_snapshot, validate_finding

SUB = "11111111-1111-1111-1111-111111111111"
_checks = 0


def ok(cond, label):
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)


def rules(findings):
    return {f["rule_id"] for f in findings}


def test_metadata_integrity():
    for rid, m in RULE_META.items():
        ok(m["domain"] in AZURE_PROVIDER.domains, f"{rid} domain valid")
        ok(m["severity"] in ("info", "low", "medium", "high", "critical"), f"{rid} severity valid")
        ok(m["effort"] in ("low", "medium", "high"), f"{rid} effort valid")


def test_network():
    nsgs = [{"id": "/sub/nsg-1", "name": "web-nsg", "location": "eastus", "properties": {"securityRules": [
        {"name": "ssh", "properties": {"direction": "Inbound", "access": "Allow", "protocol": "Tcp",
                                       "destinationPortRange": "22", "sourceAddressPrefix": "*"}},
        {"name": "sql", "properties": {"direction": "Inbound", "access": "Allow", "protocol": "Tcp",
                                       "destinationPortRange": "1433", "sourceAddressPrefix": "Internet"}},
        {"name": "all", "properties": {"direction": "Inbound", "access": "Allow", "protocol": "*",
                                       "destinationPortRange": "*", "sourceAddressPrefixes": ["0.0.0.0/0"]}},
        {"name": "ok", "properties": {"direction": "Inbound", "access": "Allow", "protocol": "Tcp",
                                      "destinationPortRange": "443", "sourceAddressPrefix": "10.0.0.0/8"}},
    ]}}]
    f = network.analyze_nsgs(nsgs, SUB, "Prod")
    r = rules(f)
    ok("AZ_NSG_ADMIN_WORLD_OPEN" in r, "NSG admin world open (22)")
    ok("AZ_NSG_DB_WORLD_OPEN" in r, "NSG db world open (1433, Internet)")
    ok("AZ_NSG_ALL_PORTS_WORLD" in r, "NSG all ports world open")
    ok(len(f) == 3, "internal 10.0.0.0/8 → 443 not flagged")
    ok(f[0]["domain"] == "network", "network domain tag")
    ok(f[0]["supporting_details"]["scope_id"] == SUB, "scope_id propagated")
    # IPv6 ::/0 treated like 0.0.0.0/0
    nsg6 = [{"id": "n2", "name": "n2", "properties": {"securityRules": [
        {"name": "rdp6", "properties": {"direction": "Inbound", "access": "Allow",
                                        "destinationPortRange": "3389", "sourceAddressPrefix": "::/0"}}]}}]
    ok("AZ_NSG_ADMIN_WORLD_OPEN" in rules(network.analyze_nsgs(nsg6, SUB)), "NSG ::/0 admin open")
    # range covering an admin port
    nsgr = [{"id": "n3", "name": "n3", "properties": {"securityRules": [
        {"name": "r", "properties": {"direction": "Inbound", "access": "Allow",
                                     "destinationPortRange": "20-30", "sourceAddressPrefix": "*"}}]}}]
    ok("AZ_NSG_ADMIN_WORLD_OPEN" in rules(network.analyze_nsgs(nsgr, SUB)), "NSG port range 20-30 covers 22")


def test_identity():
    ra = [{"name": "ra1", "properties": {"roleDefinitionId": "/x/8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
                                         "principalId": "p1", "principalType": "User",
                                         "scope": f"/subscriptions/{SUB}"}}]
    ok("AZ_RBAC_SUBSCRIPTION_OWNER" in rules(identity.analyze_role_assignments(ra, SUB)), "subscription Owner")
    rd = [{"name": "custom", "properties": {"type": "CustomRole", "roleName": "godmode",
                                            "permissions": [{"actions": ["*"]}]}}]
    ok("AZ_RBAC_CUSTOM_WILDCARD_ROLE" in rules(identity.analyze_role_definitions(rd, SUB)), "custom wildcard role")
    ca = [{"name": "old@x.com", "properties": {"emailAddress": "old@x.com", "role": "ServiceAdministrator"}}]
    ok("AZ_CLASSIC_ADMIN_PRESENT" in rules(identity.analyze_classic_admins(ca, SUB)), "classic admin present")


def test_data():
    sa = [{"id": "s1", "name": "stor1", "properties": {"allowBlobPublicAccess": True,
           "supportsHttpsTrafficOnly": False, "allowSharedKeyAccess": True,
           "networkAcls": {"defaultAction": "Allow"}}}]
    r = rules(data.analyze_storage_accounts(sa, SUB))
    ok({"AZ_STORAGE_PUBLIC_BLOB", "AZ_STORAGE_NO_HTTPS", "AZ_STORAGE_SHARED_KEY",
        "AZ_STORAGE_PUBLIC_NETWORK"} <= r, "storage account all four rules")
    sql = [{"id": "q1", "name": "sql1", "properties": {"publicNetworkAccess": "Enabled"}}]
    ok("AZ_SQL_PUBLIC_NETWORK" in rules(data.analyze_sql_servers(sql, SUB)), "sql public network")
    kv = [{"id": "k1", "name": "kv1", "properties": {"enablePurgeProtection": False,
           "networkAcls": {"defaultAction": "Allow"}}}]
    ok({"AZ_KEYVAULT_NO_PURGE_PROTECTION", "AZ_KEYVAULT_PUBLIC_NETWORK"} <= rules(data.analyze_key_vaults(kv, SUB)),
       "key vault rules")
    # a hardened storage account → no findings
    ok(data.analyze_storage_accounts([{"id": "s2", "name": "ok", "properties": {
        "allowBlobPublicAccess": False, "supportsHttpsTrafficOnly": True, "allowSharedKeyAccess": False,
        "networkAcls": {"defaultAction": "Deny"}}}], SUB) == [], "hardened storage clean")


def test_compute():
    vms = [{"id": "v1", "name": "vm1", "properties": {"storageProfile": {"osDisk": {"vhd": {"uri": "x"}}}}}]
    ok({"AZ_VM_UNMANAGED_DISK", "AZ_VM_NO_DISK_ENCRYPTION"} <= rules(compute.analyze_vms(vms, SUB)),
       "vm unmanaged + no encryption")
    enc = [{"id": "v2", "name": "vm2", "properties": {"storageProfile": {"osDisk": {"managedDisk": {"id": "d"}}},
            "securityProfile": {"encryptionAtHost": True}}}]
    ok(compute.analyze_vms(enc, SUB) == [], "encrypted managed VM clean")
    pip = [{"id": "p1", "name": "pip1", "properties": {"ipConfiguration": {"id": "nic"}, "ipAddress": "1.2.3.4"}},
           {"id": "p2", "name": "pip2", "properties": {"ipAddress": "5.6.7.8"}}]
    pf = compute.analyze_public_ips(pip, SUB)
    ok("AZ_PUBLIC_IP_IN_USE" in rules(pf) and len(pf) == 1, "only attached public IP flagged")


def test_logging():
    ok("AZ_NO_DIAGNOSTIC_SETTINGS" in rules(logging_domain.analyze_diagnostic_settings([], SUB)),
       "no diagnostic settings")
    ok(logging_domain.analyze_diagnostic_settings([{"id": "d"}], SUB) == [], "diagnostic setting present → clean")
    pr = [{"name": "VirtualMachines", "properties": {"pricingTier": "Free"}},
          {"name": "StorageAccounts", "properties": {"pricingTier": "Standard"}}]
    df = logging_domain.analyze_defender_pricings(pr, SUB)
    ok("AZ_DEFENDER_PLAN_OFF" in rules(df) and len(df) == 1, "only Free Defender plan flagged")


def test_provider_engine_wiring():
    f = []
    f += network.analyze_nsgs([{"id": "n", "name": "n", "properties": {"securityRules": [
        {"name": "s", "properties": {"direction": "Inbound", "access": "Allow",
                                     "destinationPortRange": "22", "sourceAddressPrefix": "*"}}]}}], SUB, "Prod")
    f += data.analyze_storage_accounts([{"id": "s1", "name": "stor1",
                                         "properties": {"allowBlobPublicAccess": True}}], SUB, "Prod")
    ok(all(validate_finding(x) for x in f), "all azure findings validate")
    snap = build_snapshot(scan_id="s", audit_id="a", findings=f, scopes_scanned=[SUB])
    g = score.global_posture(snap, AZURE_PROVIDER)
    ok(g["rating"] == "Critical", "azure snapshot rating Critical (admin world open)")
    cis = coverage_for(snap, AZURE_PROVIDER, "CIS")
    ok(cis["framework_label"] == "CIS Microsoft Azure Foundations", "azure CIS label")
    ok(any(c["control"] == "6.1" for c in cis["controls"]), "azure CIS includes control 6.1")
    q = score.remediation_priority_queue(f, AZURE_PROVIDER)
    ok(q[0]["severity"] == "critical", "azure priority queue critical first")


def main():
    test_metadata_integrity()
    test_network()
    test_identity()
    test_data()
    test_compute()
    test_logging()
    test_provider_engine_wiring()
    print(f"AZURE_AUDIT OK — {_checks} assertions passed")


if __name__ == "__main__":
    main()
