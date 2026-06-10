"""Deterministic-engine tests for the Cloud Security Posture module.

Covers the pure analyzers (no boto3/DB), the finding contract, scoring/priority,
multi-framework compliance, and the report builder. Run with:

    python -m sg_audit.tests.test_analyzers

Exits non-zero on the first failure. Mirrors (and persists) the checks used
during development so the engines have regression protection without a pytest
dependency or AWS access.
"""

from __future__ import annotations

import sys

from sg_audit.analysis.normalize import build_snapshot, make_domain_finding
from sg_audit.analysis.rules import analyze_account_region
from sg_audit.analysis import score
from sg_audit.compliance import FRAMEWORKS, all_frameworks, coverage_for
from sg_audit.domains import compute, containers, data, devops, external, iam, k8s_domain, network, vcs
from sg_audit.domains import DOMAIN_COLLECTORS
from sg_audit.report_inputs import build_report
from sg_audit.schema import DOMAINS

A = "111122223333"
_checks = 0


def ok(cond, label):
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)


def rules(findings):
    return {f["rule_id"] for f in findings}


def test_security_groups():
    sgs = [{"GroupId": "sg-1", "GroupName": "web", "VpcId": "v",
            "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                               "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
            "IpPermissionsEgress": []}]
    f = analyze_account_region(account_id=A, account_name="p", region="us-east-1",
                               security_groups=sgs, eni_sg_usage={"sg-1": 1})
    ok("SG_ADMIN_WORLD_INGRESS" in rules(f), "SG admin-world ingress")
    ok(f[0]["domain"] == "security_groups", "SG finding tagged security_groups domain")
    ok(f[0]["supporting_details"]["entity_id"] == "sg-1", "SG entity backfilled")
    # IPv6 ::/0 treated like 0.0.0.0/0
    sgs6 = [{"GroupId": "sg-2", "GroupName": "db", "VpcId": "v",
             "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                                "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}], "IpPermissionsEgress": []}]
    ok("SG_DB_WORLD_INGRESS" in rules(analyze_account_region(
        account_id=A, account_name="", region="us-east-1", security_groups=sgs6, eni_sg_usage=None)),
        "SG IPv6 ::/0 db ingress")


def test_iam():
    rows = [{"user": "<root_account>", "mfa_active": "false", "access_key_1_active": "true",
             "password_enabled": "false", "access_key_2_active": "false"},
            {"user": "alice", "password_enabled": "true", "mfa_active": "false",
             "password_last_used": "2020-01-01T00:00:00+00:00", "access_key_1_active": "true",
             "access_key_1_last_rotated": "2019-01-01T00:00:00+00:00", "access_key_2_active": "false"}]
    fr = rules(iam.analyze_credential_report(rows, A))
    ok({"IAM_ROOT_NO_MFA", "IAM_ROOT_HAS_KEYS", "IAM_USER_NO_MFA", "IAM_STALE_ACCESS_KEY"} <= fr, "IAM credential report")
    aad = {"RoleDetailList": [{"RoleName": "r1", "AttachedManagedPolicies": [{"PolicyName": "AdministratorAccess"}],
            "RolePolicyList": [], "AssumeRolePolicyDocument": {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}}]}}],
            "Policies": [{"PolicyName": "p", "DefaultVersionId": "v", "PolicyVersionList": [
                {"VersionId": "v", "IsDefaultVersion": True, "Document": {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}}]}]}
    fa = rules(iam.analyze_authorization_details(aad, A))
    ok({"IAM_ADMIN_ACCESS", "IAM_CROSS_ACCOUNT_TRUST_WILDCARD", "IAM_WILDCARD_POLICY"} <= fa, "IAM auth details")
    # GitHub OIDC trust without sub condition
    oidc = {"RoleDetailList": [{"RoleName": "gha", "AttachedManagedPolicies": [], "RolePolicyList": [],
            "AssumeRolePolicyDocument": {"Statement": [{"Effect": "Allow",
                "Principal": {"Federated": "arn:aws:iam::1:oidc-provider/token.actions.githubusercontent.com"}}]}}], "Policies": []}
    ok("IAM_GITHUB_OIDC_TRUST_WILDCARD" in rules(iam.analyze_authorization_details(oidc, A)), "IAM GitHub OIDC wildcard")
    restricted = {"RoleDetailList": [{"RoleName": "gha2", "AttachedManagedPolicies": [], "RolePolicyList": [],
            "AssumeRolePolicyDocument": {"Statement": [{"Effect": "Allow",
                "Principal": {"Federated": "arn:aws:iam::1:oidc-provider/token.actions.githubusercontent.com"},
                "Condition": {"StringLike": {"token.actions.githubusercontent.com:sub": "repo:o/r:*"}}}]}}], "Policies": []}
    ok("IAM_GITHUB_OIDC_TRUST_WILDCARD" not in rules(iam.analyze_authorization_details(restricted, A)),
       "IAM GitHub OIDC restricted -> no finding")
    ok(iam.analyze_password_policy(None, A)[0]["rule_id"] == "IAM_NO_PASSWORD_POLICY", "IAM password policy")


def test_network():
    vpcs = [{"VpcId": "vpc-1", "IsDefault": True, "Tags": []}]
    rts = [{"RouteTableId": "rt-1", "VpcId": "vpc-1", "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}],
            "Associations": [{"Main": True}]}]
    subs = [{"SubnetId": "sub-1", "VpcId": "vpc-1", "MapPublicIpOnLaunch": True}]
    peers = [{"VpcPeeringConnectionId": "pcx-1", "Status": {"Code": "active"},
              "RequesterVpcInfo": {"OwnerId": A, "VpcId": "vpc-1"}, "AccepterVpcInfo": {"OwnerId": "999", "VpcId": "vpc-9"}}]
    fn = network.analyze_network(A, "us-east-1", vpcs, subs, rts, peers)
    ok({"NET_DEFAULT_VPC_PRESENT", "NET_ROUTE_OPEN_TO_IGW", "NET_PUBLIC_SUBNET", "NET_SUBNET_AUTO_PUBLIC_IP",
        "NET_PEERING_CROSS_ACCOUNT"} <= rules(fn), "Network analyzers")
    ok(fn[0]["supporting_details"]["entity_type"] == "vpc", "Network entity=vpc")


def test_data():
    acl = {"Grants": [{"Grantee": {"URI": "http://acs.amazonaws.com/groups/global/AllUsers"}, "Permission": "READ"}]}
    fb = data.analyze_bucket(A, "b", "us-east-1", acl, None, {"PolicyStatus": {"IsPublic": True}}, None)
    ok({"S3_PUBLIC_ACL", "S3_PUBLIC_POLICY", "S3_NO_PUBLIC_ACCESS_BLOCK", "S3_NO_ENCRYPTION"} <= rules(fb), "S3 analyzers")
    fr = data.analyze_rds_instance(A, "us-east-1", {"DBInstanceIdentifier": "db", "PubliclyAccessible": True, "StorageEncrypted": False})
    ok({"RDS_PUBLIC", "RDS_UNENCRYPTED"} <= rules(fr), "RDS analyzers")


def test_compute():
    inst = [{"InstanceId": "i-1", "State": {"Name": "running"}, "PublicIpAddress": "1.2.3.4",
             "MetadataOptions": {"HttpTokens": "optional"}, "ImageId": "ami-1", "Tags": []}]
    fc = compute.analyze_instances(A, "us-east-1", inst, {"ami-1"}, set())
    ok({"EC2_PUBLIC_IP", "EC2_IMDSV1_ENABLED", "EC2_PUBLIC_AMI", "EC2_NOT_SSM_MANAGED"} <= rules(fc), "Compute analyzers")


def test_logging():
    from sg_audit.domains import logging_domain as lg
    ok(lg.analyze_trails(A, [])[0]["rule_id"] == "LOG_NO_CLOUDTRAIL", "Logging no-trail")
    ft = rules(lg.analyze_trails(A, [{"Name": "t", "TrailARN": "arn", "IsMultiRegionTrail": False,
                                      "IsLogging": True, "LogFileValidationEnabled": False}]))
    ok({"LOG_TRAIL_NOT_MULTIREGION", "LOG_NO_LOG_VALIDATION"} <= ft, "Logging trail issues")
    ok(lg.analyze_flow_logs(A, "us-east-1", ["vpc-1"], set())[0]["rule_id"] == "LOG_FLOW_LOGS_MISSING", "Logging flow logs")
    ok(lg.analyze_config(A, False)[0]["rule_id"] == "LOG_NO_CONFIG_RECORDER", "Logging config recorder")


def test_external_containers_devops():
    fe = external.analyze_load_balancers(A, "us-east-1", [{"name": "alb", "scheme": "internet-facing", "type": "application"}])
    fe += external.analyze_apis(A, "us-east-1", [{"id": "a", "name": "api", "public": True, "no_auth": True}])
    ok({"EXT_INTERNET_FACING_LB", "EXT_PUBLIC_API", "EXT_PUBLIC_API_NO_AUTH"} <= rules(fe), "External analyzers")
    fk = containers.analyze_cluster(A, "us-east-1", {"name": "c", "resourcesVpcConfig": {"endpointPublicAccess": True,
            "publicAccessCidrs": ["0.0.0.0/0"]}, "encryptionConfig": [], "logging": {"clusterLogging": [{"enabled": False, "types": ["api"]}]}})
    ok({"EKS_PUBLIC_ENDPOINT_WORLD", "EKS_NO_SECRETS_ENCRYPTION", "EKS_NO_CONTROL_PLANE_LOGGING"} <= rules(fk), "EKS analyzers")
    fd = devops.analyze_codebuild_project(A, "us-east-1", {"name": "b", "environment": {"privilegedMode": True,
            "environmentVariables": [{"name": "AWS_SECRET_ACCESS_KEY", "value": "x", "type": "PLAINTEXT"}]}})
    fd += devops.analyze_tfstate_bucket(A, "my-tfstate", True)
    ok({"CICD_CODEBUILD_PLAINTEXT_SECRET", "CICD_CODEBUILD_PRIVILEGED", "CICD_TFSTATE_PUBLIC"} <= rules(fd), "DevOps analyzers")


def test_vcs_k8s():
    fv = vcs.analyze_repo("org", {"name": "app", "full_name": "org/app", "private": False})
    fv += vcs.analyze_repo_controls("org", "org/app", protected=False, default_wf_permission="write",
                                    secret_alerts=[{"number": 1, "secret_type": "aws_key"}])
    ok({"VCS_PUBLIC_REPO", "VCS_NO_BRANCH_PROTECTION", "VCS_ACTIONS_WRITE_DEFAULT", "VCS_SECRET_SCANNING_ALERT"} <= rules(fv), "VCS analyzers")
    crb = [{"metadata": {"name": "b"}, "roleRef": {"name": "cluster-admin"}, "subjects": [{"kind": "Group", "name": "system:authenticated"}]}]
    pods = [{"metadata": {"name": "p", "namespace": "app"}, "spec": {"hostNetwork": True, "containers": [{"securityContext": {"privileged": True}}]}}]
    svcs = [{"metadata": {"name": "kubernetes-dashboard", "namespace": "kubernetes-dashboard"}, "spec": {"type": "LoadBalancer"}}]
    fkk = (k8s_domain.analyze_rolebindings(A, "us-east-1", "c", crb)
           + k8s_domain.analyze_pods(A, "us-east-1", "c", pods)
           + k8s_domain.analyze_services(A, "us-east-1", "c", svcs))
    ok({"K8S_CLUSTER_ADMIN_BINDING", "K8S_PRIVILEGED_POD", "K8S_HOST_NAMESPACE_POD", "K8S_DASHBOARD_EXPOSED"} <= rules(fkk), "K8s analyzers")
    ok(not k8s_domain.analyze_pods(A, "us-east-1", "c", [{"metadata": {"name": "x", "namespace": "kube-system"},
       "spec": {"hostNetwork": True, "containers": []}}]), "K8s kube-system skipped")


def test_scoring_compliance_report():
    findings = (iam.analyze_credential_report([{"user": "<root_account>", "mfa_active": "false",
                "access_key_1_active": "false", "password_enabled": "false", "access_key_2_active": "false"}], A)
                + data.analyze_bucket(A, "b", "us-east-1",
                    {"Grants": [{"Grantee": {"URI": "http://acs.amazonaws.com/groups/global/AllUsers"}, "Permission": "READ"}]},
                    None, None, None))
    snap = build_snapshot(scan_id="s", audit_id="a", findings=findings, accounts_scanned=[A])
    gp = score.global_posture(snap)
    ok(gp["rating"] in ("Critical", "High"), "global rating")
    ok(len(gp["remediation_priority_queue"]) >= 1, "priority queue populated")
    ok(gp["remediation_priority_queue"][0]["priority"] >= gp["remediation_priority_queue"][-1]["priority"], "priority sorted desc")
    covs = all_frameworks(snap)
    ok({c["framework"] for c in covs} == set(FRAMEWORKS), "all frameworks present")
    ok(all(c["evaluated"] > 0 for c in covs), "frameworks evaluate controls")
    ok(coverage_for(snap, "CIS")["framework"] == "CIS", "coverage_for CIS")
    md = build_report(snap, {"name": "Test"}, [])
    ok("Cloud Security Posture Report" in md and "Risk by Domain" in md and "Compliance Coverage" in md, "report markdown")


def test_registry():
    ok(set(DOMAIN_COLLECTORS) == {"iam", "network", "data", "compute", "logging", "devops", "containers", "external", "vcs", "k8s"},
       "10 domain collectors registered")
    ok(len(DOMAINS) == 11, "11 domains incl security_groups")
    # domain default-finding helper tags domain from metadata
    f = make_domain_finding(rule_id="IAM_USER_NO_MFA", severity="high", finding_summary="x",
                            account_id=A, entity_type="user", entity_id="u")
    ok(f["domain"] == "iam", "make_domain_finding tags domain")


def main():
    for fn in (test_security_groups, test_iam, test_network, test_data, test_compute, test_logging,
               test_external_containers_devops, test_vcs_k8s, test_scoring_compliance_report, test_registry):
        fn()
    print(f"OK — {_checks} assertions passed across 10 test groups")


if __name__ == "__main__":
    main()
