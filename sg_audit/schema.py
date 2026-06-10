"""SG-audit shared vocabulary + constants.

Dependency-free (stdlib only) so both the Flask app and the Lambda collector
import it. Single source of truth for severities, finding categories, the
deterministic rule ids, the sensitive-port map, and the audit lifecycle states.
"""

# --- Severity scale ----------------------------------------------------------
SEV_INFO = "info"
SEV_LOW = "low"
SEV_MEDIUM = "medium"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"

# Ordered low->high; index used for comparisons + severity-weighted scoring.
SEVERITY_ORDER = (SEV_INFO, SEV_LOW, SEV_MEDIUM, SEV_HIGH, SEV_CRITICAL)

# Weights used to derive a 0-100 risk score. Mirrors the VRA scheme so the
# dashboard trend/score code is directly comparable.
SEVERITY_WEIGHTS = {
    SEV_INFO: 0,
    SEV_LOW: 10,
    SEV_MEDIUM: 30,
    SEV_HIGH: 65,
    SEV_CRITICAL: 100,
}

# Effort ranking weight (lower effort surfaces first when severity ties).
EFFORT_ORDER = ("low", "medium", "high")

# --- Security domains --------------------------------------------------------
# The Cloud Security Posture engine groups findings by domain. Security Groups
# is one domain (its rule engine in analysis/rules.py is unchanged); the others
# are collected by modules under sg_audit/domains/.
DOMAIN_SECURITY_GROUPS = "security_groups"
DOMAIN_IAM = "iam"
DOMAIN_NETWORK = "network"
DOMAIN_DATA = "data"
DOMAIN_COMPUTE = "compute"
DOMAIN_LOGGING = "logging"
DOMAIN_DEVOPS = "devops"          # Phase 2: CI/CD & DevOps (AWS-native)
DOMAIN_CONTAINERS = "containers"  # Phase 2: Kubernetes / EKS (control plane)
DOMAIN_EXTERNAL = "external"      # Phase 2: external attack surface
DOMAIN_VCS = "vcs"                # Phase 3: source control / CI (GitHub)
DOMAIN_K8S = "k8s"                # Phase 3: in-cluster Kubernetes (RBAC/pods)

DOMAINS = (
    DOMAIN_IAM,
    DOMAIN_NETWORK,
    DOMAIN_SECURITY_GROUPS,
    DOMAIN_DATA,
    DOMAIN_COMPUTE,
    DOMAIN_LOGGING,
    DOMAIN_DEVOPS,
    DOMAIN_CONTAINERS,
    DOMAIN_EXTERNAL,
    DOMAIN_VCS,
    DOMAIN_K8S,
)

DOMAIN_LABELS = {
    DOMAIN_IAM: "Identity & Access",
    DOMAIN_NETWORK: "Network Architecture",
    DOMAIN_SECURITY_GROUPS: "Security Groups",
    DOMAIN_DATA: "Data & Storage",
    DOMAIN_COMPUTE: "Compute",
    DOMAIN_LOGGING: "Logging & Monitoring",
    DOMAIN_DEVOPS: "CI/CD & DevOps",
    DOMAIN_CONTAINERS: "Containers (EKS)",
    DOMAIN_EXTERNAL: "External Attack Surface",
    DOMAIN_VCS: "Source Control (GitHub)",
    DOMAIN_K8S: "Kubernetes Workloads",
}

# --- Finding categories ------------------------------------------------------
CAT_NETWORK_EXPOSURE = "network_exposure"   # admin/service ports open to the internet
CAT_DATA_EXPOSURE = "data_exposure"         # databases/caches open to the internet
CAT_ACCESS_CONTROL = "access_control"       # overly broad ingress (internal or wide public)
CAT_EGRESS = "egress"                       # overly broad egress
CAT_HYGIENE = "hygiene"                     # unused SGs, default SG misuse, missing descriptions
CAT_IDENTITY = "identity"                   # IAM users/roles/policies/MFA/credentials
CAT_PUBLIC_ACCESS = "public_access"         # publicly reachable resources (S3/RDS/EC2)
CAT_ENCRYPTION = "encryption"               # data-at-rest encryption gaps
CAT_LOGGING = "logging"                     # audit/trail/flow-log gaps
CAT_MONITORING = "monitoring"               # config recorder / retention gaps
CAT_PATCH_MANAGEMENT = "patch_management"   # unmanaged/unpatched compute

CATEGORIES = (
    CAT_NETWORK_EXPOSURE,
    CAT_DATA_EXPOSURE,
    CAT_ACCESS_CONTROL,
    CAT_EGRESS,
    CAT_HYGIENE,
    CAT_IDENTITY,
    CAT_PUBLIC_ACCESS,
    CAT_ENCRYPTION,
    CAT_LOGGING,
    CAT_MONITORING,
    CAT_PATCH_MANAGEMENT,
)

CATEGORY_LABELS = {
    CAT_NETWORK_EXPOSURE: "Network Exposure",
    CAT_DATA_EXPOSURE: "Data Exposure",
    CAT_ACCESS_CONTROL: "Access Control",
    CAT_EGRESS: "Egress",
    CAT_HYGIENE: "Hygiene",
    CAT_IDENTITY: "Identity & Access",
    CAT_PUBLIC_ACCESS: "Public Access",
    CAT_ENCRYPTION: "Encryption",
    CAT_LOGGING: "Logging",
    CAT_MONITORING: "Monitoring",
    CAT_PATCH_MANAGEMENT: "Patch Management",
}

# --- Audit lifecycle states (audit record `scan_state`) ----------------------
SCAN_PENDING = "pending"
SCAN_IN_FLIGHT = "in_flight"
SCAN_COMPLETE = "complete"
SCAN_FAILED = "failed"
SCAN_STATES = (SCAN_PENDING, SCAN_IN_FLIGHT, SCAN_COMPLETE, SCAN_FAILED)

# --- Deterministic rule ids --------------------------------------------------
# Each is a stable identifier emitted in a finding's risk_indicators and used by
# the AI faithfulness validator. Keep in sync with sg_audit/analysis/rules.py.
RULE_ADMIN_WORLD_INGRESS = "SG_ADMIN_WORLD_INGRESS"
RULE_DB_WORLD_INGRESS = "SG_DB_WORLD_INGRESS"
RULE_CACHE_WORLD_INGRESS = "SG_CACHE_WORLD_INGRESS"
RULE_ALL_PORTS_WORLD = "SG_ALL_PORTS_WORLD"
RULE_WIDE_RANGE_WORLD = "SG_WIDE_RANGE_WORLD"
RULE_SENSITIVE_NON_ADMIN_WORLD = "SG_SENSITIVE_NON_ADMIN_WORLD"
RULE_NON_WORLD_ADMIN_OPEN_WIDE = "SG_NON_WORLD_ADMIN_OPEN_WIDE"
RULE_DEFAULT_SG_HAS_RULES = "SG_DEFAULT_SG_HAS_RULES"
RULE_DEFAULT_SG_IN_USE = "SG_DEFAULT_SG_IN_USE"
RULE_BROAD_INTERNAL_CIDR = "SG_BROAD_INTERNAL_CIDR"
RULE_BROAD_EGRESS_ALL = "SG_BROAD_EGRESS_ALL"
RULE_UNUSED = "SG_UNUSED"
RULE_ICMP_WORLD = "SG_ICMP_WORLD"
RULE_MISSING_RULE_DESCRIPTION = "SG_MISSING_RULE_DESCRIPTION"

RULE_IDS = (
    RULE_ADMIN_WORLD_INGRESS,
    RULE_DB_WORLD_INGRESS,
    RULE_CACHE_WORLD_INGRESS,
    RULE_ALL_PORTS_WORLD,
    RULE_WIDE_RANGE_WORLD,
    RULE_SENSITIVE_NON_ADMIN_WORLD,
    RULE_NON_WORLD_ADMIN_OPEN_WIDE,
    RULE_DEFAULT_SG_HAS_RULES,
    RULE_DEFAULT_SG_IN_USE,
    RULE_BROAD_INTERNAL_CIDR,
    RULE_BROAD_EGRESS_ALL,
    RULE_UNUSED,
    RULE_ICMP_WORLD,
    RULE_MISSING_RULE_DESCRIPTION,
)

# Human label + remediation hint per rule (used by report/recommend grounding).
RULE_LABELS = {
    RULE_ADMIN_WORLD_INGRESS: "Administrative port open to the internet",
    RULE_DB_WORLD_INGRESS: "Database port open to the internet",
    RULE_CACHE_WORLD_INGRESS: "Cache port open to the internet",
    RULE_ALL_PORTS_WORLD: "All ports/protocols open to the internet",
    RULE_WIDE_RANGE_WORLD: "Wide port range open to the internet",
    RULE_SENSITIVE_NON_ADMIN_WORLD: "Sensitive service port open to the internet",
    RULE_NON_WORLD_ADMIN_OPEN_WIDE: "Administrative port open to a wide public range",
    RULE_DEFAULT_SG_HAS_RULES: "Default security group has non-default rules",
    RULE_DEFAULT_SG_IN_USE: "Default security group is attached to resources",
    RULE_BROAD_INTERNAL_CIDR: "Sensitive port open to a broad internal range",
    RULE_BROAD_EGRESS_ALL: "Unrestricted egress to all destinations",
    RULE_UNUSED: "Unused security group (no attachments)",
    RULE_ICMP_WORLD: "ICMP open to the internet",
    RULE_MISSING_RULE_DESCRIPTION: "Security group rule missing a description",
}

# --- Sensitive port map ------------------------------------------------------
# port -> (service label, class). Classes drive severity banding in rules.py.
PORT_CLASS_ADMIN = "admin"
PORT_CLASS_DATABASE = "database"
PORT_CLASS_CACHE = "cache"
PORT_CLASS_SENSITIVE = "sensitive"

PORT_SERVICE_MAP = {
    22: ("SSH", PORT_CLASS_ADMIN),
    23: ("Telnet", PORT_CLASS_ADMIN),
    3389: ("RDP", PORT_CLASS_ADMIN),
    5985: ("WinRM-HTTP", PORT_CLASS_ADMIN),
    5986: ("WinRM-HTTPS", PORT_CLASS_ADMIN),
    3306: ("MySQL/Aurora", PORT_CLASS_DATABASE),
    5432: ("PostgreSQL", PORT_CLASS_DATABASE),
    1433: ("MS SQL Server", PORT_CLASS_DATABASE),
    1521: ("Oracle", PORT_CLASS_DATABASE),
    27017: ("MongoDB", PORT_CLASS_DATABASE),
    27018: ("MongoDB-shard", PORT_CLASS_DATABASE),
    5984: ("CouchDB", PORT_CLASS_DATABASE),
    9042: ("Cassandra", PORT_CLASS_DATABASE),
    6379: ("Redis", PORT_CLASS_CACHE),
    11211: ("Memcached", PORT_CLASS_CACHE),
    9200: ("Elasticsearch", PORT_CLASS_SENSITIVE),
    9300: ("Elasticsearch-transport", PORT_CLASS_SENSITIVE),
    5601: ("Kibana", PORT_CLASS_SENSITIVE),
    2379: ("etcd-client", PORT_CLASS_SENSITIVE),
    2380: ("etcd-peer", PORT_CLASS_SENSITIVE),
    9092: ("Kafka", PORT_CLASS_SENSITIVE),
    2181: ("ZooKeeper", PORT_CLASS_SENSITIVE),
    5672: ("RabbitMQ", PORT_CLASS_SENSITIVE),
    15672: ("RabbitMQ-mgmt", PORT_CLASS_SENSITIVE),
    8020: ("Hadoop-NN", PORT_CLASS_SENSITIVE),
    9000: ("Hadoop-NN-alt", PORT_CLASS_SENSITIVE),
    50070: ("HDFS-WebUI", PORT_CLASS_SENSITIVE),
    9090: ("Prometheus", PORT_CLASS_SENSITIVE),
    3000: ("Grafana", PORT_CLASS_SENSITIVE),
    445: ("SMB", PORT_CLASS_SENSITIVE),
    139: ("NetBIOS", PORT_CLASS_SENSITIVE),
    161: ("SNMP", PORT_CLASS_SENSITIVE),
}

SENSITIVE_PORTS = frozenset(PORT_SERVICE_MAP.keys())

# World-open CIDRs (IPv4 + IPv6) treated identically.
WORLD_CIDRS = frozenset({"0.0.0.0/0", "::/0"})

# A range wider than this many ports (but not literally all) is "wide".
WIDE_RANGE_PORT_THRESHOLD = 100

# --- Posture rating bands (0-100 risk_score -> label) ------------------------
# A single critical floors the rating at Critical regardless of mean.
RATING_BANDS = ((75, "Critical"), (50, "High"), (20, "Medium"), (0, "Low"))


def rating_for(risk_score: float, has_critical: bool = False) -> str:
    """Map a 0-100 risk score to a posture rating label."""
    if has_critical:
        return "Critical"
    for threshold, label in RATING_BANDS:
        if (risk_score or 0.0) >= threshold:
            return label
    return "Low"
