// VRA frontend types — mirror the backend contract. Authoritative source of
// truth for shapes returned by the /vra/* endpoints.

export type Severity = 'info' | 'low' | 'medium' | 'high' | 'critical';

export type Category =
  | 'corporate'
  | 'domain'
  | 'security'
  | 'vulnerability'
  | 'breach'
  | 'compliance'
  | 'reputation'
  | 'open_source';

export type ScanState = 'pending' | 'in_flight' | 'complete' | 'failed';
export type AssessmentType = 'vra' | 'standard';
export type RiskRating = 'Low' | 'Medium' | 'High' | 'Critical' | 'Unknown';
export type TrendDirection = 'baseline' | 'worsening' | 'improving' | 'stable';

export interface VraAssessment {
  assessment_id: string;
  user_id: string;
  playbook_id: string | null;
  runbook_id: string | null;
  vendor_name: string;
  vendor_domain: string;
  assessment_type: AssessmentType;
  report_title: string; // "Vendor Risk Assessment – <name>"
  scan_state: ScanState;
  latest_scan_id: string | null;
  last_scan_at: string | null;
  next_scan_at: string | null;
  latest_risk_score?: number; // 0–100, present after a scan completes
  retention_until: string;
  created_at: string;
  updated_at: string;
}

export interface DefaultQuestion {
  id: string;
  question: string;
  user_answer: string;
  options: Record<string, string>;
  comment: string;
  section: 'Vendor Identification';
  help_text: string;
  vra_role: 'vendor_name' | 'vendor_domain';
  required: true;
  locked: true; // builder must block delete / reorder / role-edit
}

export interface CollectResult {
  status:
    | 'launched'
    | 'unchanged'
    | 'already_running'
    | 'disabled'
    | 'skipped'
    | 'error';
  scan_id?: string;
  reason?: string;
  message?: string;
}

export interface EvidenceRecord {
  source: string;
  source_url: string;
  collection_date: string;
  evidence_type: string;
  category: Category;
  category_label: string;
  finding_summary: string;
  supporting_details: Record<string, unknown>;
  risk_indicators: string[];
  severity: Severity;
}

export interface EvidenceBundle {
  records: EvidenceRecord[];
  total_findings: number;
  evidence_artifacts: number; // records carrying a source_url
  collected_window: { first: string; last: string };
  scan_id: string;
  scanned_at: string;
}

export interface KeyObservation {
  summary: string;
  severity: Severity;
  category: Category;
  category_label: string;
  source_url: string;
  risk_indicators: string[];
}

export interface CountBlock {
  total: number;
  by_severity: Record<Severity, number>;
  by_category: Record<Category, number>;
}

export interface AnalysisContext {
  vendor_name: string;
  vendor_domain: string;
  scanned_at: string;
  risk_rating: RiskRating;
  snapshot_risk_score: number;
  counts: CountBlock;
  key_observations: KeyObservation[];
  trend: {
    direction: TrendDirection;
    previous_score: number | null;
    current_score: number;
  };
  evidence_summary: {
    total_findings: number;
    evidence_artifacts: number;
    collected_window: { first: string; last: string };
  };
  traceability: { finding: string; source_url: string }[];
}

export interface ComplianceCoverage {
  certifications: string[];
  has_security_txt: boolean;
  has_trust_center: boolean;
}

export interface DashboardExecutiveSummary {
  vendor_name: string;
  overall_risk_rating: RiskRating;
  risk_trend?: TrendDirection;
  last_scan_date: string | null;
  total_findings: number;
  critical_findings?: number;
  high_findings?: number;
  medium_findings?: number;
  low_findings?: number;
  scan_state?: ScanState; // present when scanned === false
}

export interface DashboardRiskOverview {
  risk_score: number;
  risk_rating: RiskRating;
  trend_chart: { scanned_at: string; risk_score: number }[];
  finding_distribution: Record<Severity, number>;
  compliance_coverage: ComplianceCoverage;
  category_summary: {
    category: Category;
    label: string;
    count: number;
    worst_severity: Severity;
  }[];
}

export type DashboardPanelCategory =
  | 'corporate'
  | 'security'
  | 'compliance'
  | 'reputation';

export interface VraDashboard {
  assessment_id: string;
  dashboard_url: string;
  scanned: boolean;
  executive_summary: DashboardExecutiveSummary;
  risk_overview?: DashboardRiskOverview;
  categories?: Record<
    DashboardPanelCategory,
    { label: string; count: number; records: EvidenceRecord[] }
  >;
  evidence?: EvidenceBundle;
  key_observations?: KeyObservation[];
  analysis?: AnalysisContext;
}

export interface VraHealth {
  collection_enabled: boolean;
  region: string;
  rescan_cadence_days: number;
  retention_days: number;
}
