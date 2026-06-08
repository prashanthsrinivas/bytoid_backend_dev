// VRA API client — framework-agnostic. Wrap your repo's existing authed HTTP
// layer by passing a compatible `http`, or use the default fetch impl below.
//
// Handles the backend's user_id convention automatically:
//   * GET  -> user_id sent as ?user_id=<composite>
//   * POST/DELETE -> user_id merged into the JSON body
// All requests use credentials: 'include' so the session cookie / bearer rides.

import type {
  AnalysisContext,
  CollectResult,
  DefaultQuestion,
  EvidenceBundle,
  VraAssessment,
  VraDashboard,
  VraHealth,
} from './types';

export interface VraApiConfig {
  baseUrl: string; // API origin, e.g. https://api.bytoid.ai
  getUserId: () => string; // returns the composite user_id for the active user
}

function qs(params: Record<string, string | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v != null) as [string, string][];
  return entries.length ? `?${new URLSearchParams(entries).toString()}` : '';
}

async function request<T>(
  cfg: VraApiConfig,
  method: 'GET' | 'POST' | 'DELETE',
  path: string,
  opts: { query?: Record<string, string | undefined>; body?: Record<string, unknown> } = {},
): Promise<T> {
  const userId = cfg.getUserId();
  const isWrite = method !== 'GET';
  const query = { ...(opts.query ?? {}), ...(isWrite ? {} : { user_id: userId }) };
  const body = isWrite ? { ...(opts.body ?? {}), user_id: userId } : undefined;

  const res = await fetch(`${cfg.baseUrl}${path}${qs(query)}`, {
    method,
    credentials: 'include',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await res.text();
  const json = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const message = (json && (json.message || json.error)) || `VRA API ${res.status}`;
    throw Object.assign(new Error(message), { status: res.status, body: json });
  }
  return json as T;
}

export function createVraApi(cfg: VraApiConfig) {
  return {
    getHealth: () => request<VraHealth>(cfg, 'GET', '/vra/health'),

    getDefaultQuestions: () =>
      request<{ questions: DefaultQuestion[] }>(cfg, 'GET', '/vra/default-questions').then(
        (r) => r.questions,
      ),

    createAssessment: (input: {
      playbook_id?: string;
      runbook_id?: string;
      vendor_name?: string;
      vendor_domain?: string;
    }) =>
      request<{ assessment: VraAssessment; default_questions: DefaultQuestion[] }>(
        cfg,
        'POST',
        '/vra/assessment',
        { body: input },
      ),

    getAssessment: (id: string) =>
      request<{ assessment: VraAssessment }>(cfg, 'GET', `/vra/assessment/${id}`).then(
        (r) => r.assessment,
      ),

    listAssessments: () =>
      request<{ assessments: VraAssessment[] }>(cfg, 'GET', '/vra/assessments').then(
        (r) => r.assessments,
      ),

    setVendor: (id: string, input: { vendor_name?: string; vendor_domain?: string }) =>
      request<{ assessment: VraAssessment }>(cfg, 'POST', `/vra/assessment/${id}/vendor`, {
        body: input,
      }).then((r) => r.assessment),

    runCollection: (id: string, force = false) =>
      request<CollectResult>(cfg, 'POST', `/vra/assessment/${id}/collect`, {
        body: { force },
      }),

    getDashboard: (id: string) =>
      request<{ dashboard: VraDashboard }>(cfg, 'GET', `/vra/assessment/${id}/dashboard`).then(
        (r) => r.dashboard,
      ),

    getEvidence: (id: string, scanId?: string) =>
      request<{ evidence: EvidenceBundle }>(cfg, 'GET', `/vra/assessment/${id}/evidence`, {
        query: { scan_id: scanId },
      }).then((r) => r.evidence),

    getAnalysis: (id: string) =>
      request<{ context: AnalysisContext; markdown: string }>(
        cfg,
        'GET',
        `/vra/assessment/${id}/analysis`,
      ),

    linkRunbook: (id: string, input: { runbook_id: string; playbook_id: string }) =>
      request<{ assessment: VraAssessment }>(cfg, 'POST', `/vra/assessment/${id}/link`, {
        body: input,
      }).then((r) => r.assessment),

    regenerateReport: (id: string) =>
      request<{ status: 'queued' | 'not_linked' | 'not_found' | 'error' }>(
        cfg,
        'POST',
        `/vra/assessment/${id}/report`,
      ),

    deleteAssessment: (id: string) =>
      request<{ deleted: string }>(cfg, 'DELETE', `/vra/assessment/${id}`),
  };
}

export type VraApi = ReturnType<typeof createVraApi>;
