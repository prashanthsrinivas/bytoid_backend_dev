"""VRA shared vocabulary + constants.

Dependency-free so both the Flask app and the Lambda collector import it. This
is the single source of truth for intelligence categories, severities, evidence
types, and the two mandatory default questions.
"""

# --- Intelligence categories (mirror the requirements doc) -------------------
CAT_CORPORATE = "corporate"
CAT_DOMAIN = "domain"
CAT_SECURITY = "security"
CAT_VULNERABILITY = "vulnerability"
CAT_BREACH = "breach"
CAT_COMPLIANCE = "compliance"
CAT_REPUTATION = "reputation"
CAT_OPEN_SOURCE = "open_source"

CATEGORIES = (
    CAT_CORPORATE,
    CAT_DOMAIN,
    CAT_SECURITY,
    CAT_VULNERABILITY,
    CAT_BREACH,
    CAT_COMPLIANCE,
    CAT_REPUTATION,
    CAT_OPEN_SOURCE,
)

# Human labels for dashboard/report rendering.
CATEGORY_LABELS = {
    CAT_CORPORATE: "Corporate Intelligence",
    CAT_DOMAIN: "Domain Intelligence",
    CAT_SECURITY: "Security Intelligence",
    CAT_VULNERABILITY: "Vulnerability Intelligence",
    CAT_BREACH: "Breach Intelligence",
    CAT_COMPLIANCE: "Compliance Intelligence",
    CAT_REPUTATION: "Reputation Intelligence",
    CAT_OPEN_SOURCE: "Open Source Intelligence",
}

# --- Severity scale ----------------------------------------------------------
SEV_INFO = "info"
SEV_LOW = "low"
SEV_MEDIUM = "medium"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"

# Ordered low->high; index used for comparisons + severity-weighted scoring.
SEVERITY_ORDER = (SEV_INFO, SEV_LOW, SEV_MEDIUM, SEV_HIGH, SEV_CRITICAL)

# Weights used to derive a 0-100 snapshot risk score for the dashboard trend
# line. This is intentionally distinct from the runbook's Impact x Likelihood
# risk_engine score (which remains the authoritative assessment rating).
SEVERITY_WEIGHTS = {
    SEV_INFO: 0,
    SEV_LOW: 10,
    SEV_MEDIUM: 30,
    SEV_HIGH: 65,
    SEV_CRITICAL: 100,
}

# --- Scan lifecycle states (vra_assessments.scan_state) ----------------------
SCAN_PENDING = "pending"
SCAN_IN_FLIGHT = "in_flight"
SCAN_COMPLETE = "complete"
SCAN_FAILED = "failed"
SCAN_STATES = (SCAN_PENDING, SCAN_IN_FLIGHT, SCAN_COMPLETE, SCAN_FAILED)

# --- Assessment type (playbook config + vra_assessments) ---------------------
ASSESSMENT_STANDARD = "standard"
ASSESSMENT_VRA = "vra"

# --- Mandatory default questions auto-inserted into a VRA questionnaire ------
# ``vra_role`` ties a question to its OSINT meaning; ``locked`` blocks deletion
# and role-changing edits. Order matters: these are the first two questions.
VRA_ROLE_VENDOR_NAME = "vendor_name"
VRA_ROLE_VENDOR_DOMAIN = "vendor_domain"

DEFAULT_VRA_QUESTIONS = (
    {
        "vra_role": VRA_ROLE_VENDOR_NAME,
        "question": "Vendor Name",
        "help_text": "Legal/commercial name of the vendor being assessed "
        "(e.g. Microsoft, Amazon Web Services, Sigmoid).",
        "required": True,
        "locked": True,
    },
    {
        "vra_role": VRA_ROLE_VENDOR_DOMAIN,
        "question": "Vendor Website / Primary Domain",
        "help_text": "Primary website or domain used for OSINT collection "
        "(e.g. microsoft.com, aws.amazon.com, sigmoid.com).",
        "required": True,
        "locked": True,
    },
)

# Report title template kept in sync with the vendor name throughout the
# assessment lifecycle.
REPORT_TITLE_TEMPLATE = "Vendor Risk Assessment – {vendor_name}"  # noqa: RUF001 (en dash per spec)


def report_title_for(vendor_name: str) -> str:
    """Deterministic VRA report/runbook title from the vendor name."""
    name = (vendor_name or "").strip() or "Vendor"
    return REPORT_TITLE_TEMPLATE.format(vendor_name=name)
