"""Shared CSPM vocabulary — severities, categories, scan states, rating bands.

Cloud-agnostic (stdlib only). Mirrors ``sg_audit/schema.py``'s generic parts;
provider-specific domains/rules live in each provider's ``metadata.py``.
"""

# --- Severity scale ----------------------------------------------------------
SEV_INFO = "info"
SEV_LOW = "low"
SEV_MEDIUM = "medium"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"

SEVERITY_ORDER = (SEV_INFO, SEV_LOW, SEV_MEDIUM, SEV_HIGH, SEV_CRITICAL)

SEVERITY_WEIGHTS = {
    SEV_INFO: 0, SEV_LOW: 10, SEV_MEDIUM: 30, SEV_HIGH: 65, SEV_CRITICAL: 100,
}

EFFORT_ORDER = ("low", "medium", "high")

# --- Finding categories (shared across clouds) -------------------------------
CAT_NETWORK_EXPOSURE = "network_exposure"
CAT_DATA_EXPOSURE = "data_exposure"
CAT_ACCESS_CONTROL = "access_control"
CAT_EGRESS = "egress"
CAT_HYGIENE = "hygiene"
CAT_IDENTITY = "identity"
CAT_PUBLIC_ACCESS = "public_access"
CAT_ENCRYPTION = "encryption"
CAT_LOGGING = "logging"
CAT_MONITORING = "monitoring"
CAT_PATCH_MANAGEMENT = "patch_management"

CATEGORIES = (
    CAT_NETWORK_EXPOSURE, CAT_DATA_EXPOSURE, CAT_ACCESS_CONTROL, CAT_EGRESS, CAT_HYGIENE,
    CAT_IDENTITY, CAT_PUBLIC_ACCESS, CAT_ENCRYPTION, CAT_LOGGING, CAT_MONITORING, CAT_PATCH_MANAGEMENT,
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

# --- Audit lifecycle states --------------------------------------------------
SCAN_PENDING = "pending"
SCAN_IN_FLIGHT = "in_flight"
SCAN_COMPLETE = "complete"
SCAN_FAILED = "failed"
SCAN_STATES = (SCAN_PENDING, SCAN_IN_FLIGHT, SCAN_COMPLETE, SCAN_FAILED)

# --- Posture rating bands (0-100 risk_score -> label) ------------------------
RATING_BANDS = ((75, "Critical"), (50, "High"), (20, "Medium"), (0, "Low"))


def rating_for(risk_score: float, has_critical: bool = False) -> str:
    if has_critical:
        return "Critical"
    for threshold, label in RATING_BANDS:
        if (risk_score or 0.0) >= threshold:
            return label
    return "Low"
