"""Risk-relevance scoring + risk-category classification for OSINT findings.

Turns raw collected artifacts into *vendor-risk intelligence*: every finding gets
a 0-100 relevance score and a risk category (Security / Compliance / Legal /
Financial / Operational / Reputation). Low-relevance noise (industry-wide news,
keyword-only matches, generic DNS) is suppressed below a configurable threshold
so the report favors fewer, high-value findings over many generic ones.

Deterministic + dependency-free (stdlib only) so it runs in the Lambda and is
fully testable. No LLM — scores are explainable (``relevance_reasons``).
"""

from __future__ import annotations

import contextlib
import json

# --- Vendor-risk intelligence categories -------------------------------------
RISK_SECURITY = "security"
RISK_COMPLIANCE = "compliance"
RISK_LEGAL = "legal"
RISK_FINANCIAL = "financial"
RISK_OPERATIONAL = "operational"
RISK_REPUTATION = "reputation"

RISK_CATEGORIES = (
    RISK_SECURITY,
    RISK_COMPLIANCE,
    RISK_LEGAL,
    RISK_FINANCIAL,
    RISK_OPERATIONAL,
    RISK_REPUTATION,
)

RISK_CATEGORY_LABELS = {
    RISK_SECURITY: "Security Intelligence",
    RISK_COMPLIANCE: "Compliance Intelligence",
    RISK_LEGAL: "Legal Intelligence",
    RISK_FINANCIAL: "Financial Intelligence",
    RISK_OPERATIONAL: "Operational Intelligence",
    RISK_REPUTATION: "Reputation Intelligence",
}

# evidence_type -> risk category (for the deterministic collectors).
_EVIDENCE_RISK_CATEGORY = {
    "known_exploited_vulnerability": RISK_SECURITY,
    "public_cve": RISK_SECURITY,
    "infrastructure_exposure": RISK_SECURITY,
    "certificate_transparency": RISK_SECURITY,
    "security_headers": RISK_SECURITY,
    "breach_disclosure": RISK_SECURITY,
    "spf": RISK_SECURITY,
    "dmarc": RISK_SECURITY,
    "dkim": RISK_SECURITY,
    "dns_mx": RISK_SECURITY,
    "dns_ns": RISK_SECURITY,
    "security_txt": RISK_COMPLIANCE,
    "certification_mention": RISK_COMPLIANCE,
    "trust_center": RISK_COMPLIANCE,
}

# Base relevance per evidence_type (0-100). Risk-bearing signals start high;
# generic hygiene/inventory signals start low. News is scored separately.
_BASE_RELEVANCE = {
    "known_exploited_vulnerability": 95,
    "breach_disclosure": 92,
    "public_cve": 78,
    "infrastructure_exposure": 60,
    "certificate_transparency": 25,
    "security_headers": 40,
    "dmarc": 45,
    "spf": 42,
    "dkim": 20,
    "dns_mx": 8,
    "dns_ns": 8,
    "security_txt": 30,
    "certification_mention": 60,   # positive but materially relevant
    "trust_center": 45,
    "reputation_summary": 35,
}

# Keyword groups that, when present in a (vendor-specific) news item, mark it as
# materially risk-relevant and route it to the right risk category.
_RISK_KEYWORDS = {
    RISK_SECURITY: (
        "breach", "hacked", "hack", "ransomware", "data leak", "leaked", "exposed",
        "vulnerability", "exploit", "compromise", "malware", "cyberattack", "phishing",
        "zero-day", "security incident",
    ),
    RISK_LEGAL: (
        "lawsuit", "sued", "settlement", "court", "litigation", "class action",
        "indicted", "fraud charges", "enforcement", "subpoena",
    ),
    RISK_COMPLIANCE: (
        "gdpr", "hipaa", "fined", "fine", "regulator", "regulatory", "privacy investigation",
        "data protection authority", "ftc", "sec ", "audit failure", "non-compliance",
        "violation",
    ),
    RISK_FINANCIAL: (
        "bankruptcy", "insolvency", "layoffs", "lays off", "acquisition", "acquired",
        "merger", "funding round", "raises", "going concern", "downsizing", "restructuring",
        "default",
    ),
    RISK_OPERATIONAL: (
        "outage", "downtime", "disruption", "service down", "degraded", "incident report",
        "supply chain", "discontinued", "shutting down",
    ),
}

# Words that mark an article as NOT vendor-risk (sector pieces, opinion, marketing).
_NOISE_MARKERS = (
    "opinion", "editorial", "sponsored", "advertisement", "press release",
    "how to", "top 10", "best practices", "guide to", "webinar", "ebook",
)


def classify_risk_category(finding: dict) -> str:
    """Map a finding to a vendor-risk intelligence category."""
    et = finding.get("evidence_type", "")
    if et in _EVIDENCE_RISK_CATEGORY:
        return _EVIDENCE_RISK_CATEGORY[et]
    # News / reputation items: route by detected risk keywords, else reputation.
    text = _finding_text(finding)
    for cat in (RISK_LEGAL, RISK_FINANCIAL, RISK_COMPLIANCE, RISK_OPERATIONAL, RISK_SECURITY):
        if any(k in text for k in _RISK_KEYWORDS[cat]):
            return cat
    return RISK_REPUTATION


def _finding_text(finding: dict) -> str:
    parts = [finding.get("finding_summary", "")]
    sd = finding.get("supporting_details")
    if sd:
        with contextlib.suppress(Exception):
            parts.append(json.dumps(sd, default=str))
    parts.extend(finding.get("risk_indicators") or [])
    return " ".join(p for p in parts if p).lower()


def _vendor_mentioned(finding: dict, vendor_name: str, vendor_domain: str) -> bool:
    text = _finding_text(finding)
    name = (vendor_name or "").strip().lower()
    dom = (vendor_domain or "").strip().lower()
    # Match the distinctive vendor token (first word of the name) or the domain.
    token = name.split()[0] if name else ""
    return bool((token and token in text) or (dom and dom.split(".")[0] in text))


def score_relevance(finding: dict, vendor_name: str = "", vendor_domain: str = "") -> tuple[int, list[str]]:
    """Return (relevance 0-100, reasons). Deterministic + explainable."""
    et = finding.get("evidence_type", "")
    reasons: list[str] = []
    score = _BASE_RELEVANCE.get(et, 30)

    # News items are scored from scratch on materiality. A risk keyword only
    # counts when the vendor is actually named — otherwise it's sector/industry
    # noise (a "breach" article about the sector, not this vendor) and stays low.
    if et in ("news_article", "reputation_summary"):
        score = 20
        text = _finding_text(finding)
        if _vendor_mentioned(finding, vendor_name, vendor_domain):
            score += 35
            reasons.append("vendor explicitly mentioned")
            for cat, kws in _RISK_KEYWORDS.items():
                if any(k in text for k in kws):
                    score += 30
                    reasons.append(f"{cat} risk keyword present")
                    break
        else:
            reasons.append("vendor not clearly mentioned (sector/keyword-only)")
        if any(m in text for m in _NOISE_MARKERS):
            score -= 30
            reasons.append("opinion/marketing/listicle marker")
    else:
        # Structured signals: boost when the vendor is named + on active risk.
        if finding.get("risk_indicators"):
            score += 10
            reasons.append("carries risk indicators")
        sev = finding.get("severity")
        if sev in ("critical", "high"):
            score += 10
            reasons.append(f"{sev} severity")

    return max(0, min(100, score)), reasons


def annotate_and_filter(
    findings: list[dict],
    *,
    vendor_name: str = "",
    vendor_domain: str = "",
    threshold: int = 35,
) -> list[dict]:
    """Score + classify every finding, drop below-threshold noise, dedup.

    Each kept finding gains ``relevance``, ``relevance_reasons`` and
    ``risk_category``. Deduplicates by (risk_category, finding_summary). Sorted
    by relevance desc so the most material findings lead.
    """
    annotated: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for f in findings or []:
        rel, reasons = score_relevance(f, vendor_name, vendor_domain)
        cat = classify_risk_category(f)
        if rel < threshold:
            continue
        key = (cat, (f.get("finding_summary") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out = dict(f)
        out["relevance"] = rel
        out["relevance_reasons"] = reasons
        out["risk_category"] = cat
        annotated.append(out)
    annotated.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return annotated


def risk_category_counts(findings: list[dict]) -> dict:
    """Count findings per risk category (all categories zero-filled).

    Uses the finding's annotated ``risk_category`` if present, else classifies
    on the fly — so it works on raw or annotated findings.
    """
    counts = {c: 0 for c in RISK_CATEGORIES}
    for f in findings:
        c = f.get("risk_category") or classify_risk_category(f)
        if c in counts:
            counts[c] += 1
    return counts
