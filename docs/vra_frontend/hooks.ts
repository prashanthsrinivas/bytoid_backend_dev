// VRA React Query hooks. Assumes a `useVraApi()` that returns a `VraApi`
// (createVraApi from vraApi.ts) provided via context — wire that to your repo's
// existing API/provider setup. Requires @tanstack/react-query.

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import type {
  AnalysisContext,
  CollectResult,
  DefaultQuestion,
  EvidenceBundle,
  VraAssessment,
  VraDashboard,
} from './types';
import type { VraApi } from './vraApi';

// Provide this from your app (e.g. a React context holding createVraApi(...)).
declare function useVraApi(): VraApi;

export const vraKeys = {
  all: ['vra'] as const,
  defaultQuestions: () => [...vraKeys.all, 'default-questions'] as const,
  assessments: () => [...vraKeys.all, 'assessments'] as const,
  assessment: (id: string) => [...vraKeys.all, 'assessment', id] as const,
  dashboard: (id: string) => [...vraKeys.all, 'dashboard', id] as const,
  evidence: (id: string, scanId?: string) =>
    [...vraKeys.all, 'evidence', id, scanId ?? 'latest'] as const,
  analysis: (id: string) => [...vraKeys.all, 'analysis', id] as const,
};

// ---- queries ----------------------------------------------------------------

export function useDefaultQuestions() {
  const api = useVraApi();
  return useQuery({ queryKey: vraKeys.defaultQuestions(), queryFn: () => api.getDefaultQuestions() });
}

export function useVraAssessments() {
  const api = useVraApi();
  return useQuery({ queryKey: vraKeys.assessments(), queryFn: () => api.listAssessments() });
}

export function useVraAssessment(
  id: string,
  options?: Partial<UseQueryOptions<VraAssessment>>,
) {
  const api = useVraApi();
  return useQuery({
    queryKey: vraKeys.assessment(id),
    queryFn: () => api.getAssessment(id),
    enabled: !!id,
    ...options,
  });
}

export function useVraDashboard(id: string, enabled = true) {
  const api = useVraApi();
  return useQuery({
    queryKey: vraKeys.dashboard(id),
    queryFn: () => api.getDashboard(id),
    enabled: enabled && !!id,
  });
}

export function useVraEvidence(id: string, scanId?: string) {
  const api = useVraApi();
  return useQuery({
    queryKey: vraKeys.evidence(id, scanId),
    queryFn: () => api.getEvidence(id, scanId),
    enabled: !!id,
  });
}

export function useVraAnalysis(id: string) {
  const api = useVraApi();
  return useQuery<{ context: AnalysisContext; markdown: string }>({
    queryKey: vraKeys.analysis(id),
    queryFn: () => api.getAnalysis(id),
    enabled: !!id,
  });
}

const TERMINAL: ReadonlyArray<VraAssessment['scan_state']> = ['complete', 'failed'];

/**
 * Polls the assessment until the scan reaches a terminal state, then stops and
 * invalidates the dashboard + evidence so they refetch with fresh data.
 */
export function useScanStatus(id: string, intervalMs = 8000) {
  const api = useVraApi();
  const qc = useQueryClient();
  return useQuery({
    queryKey: vraKeys.assessment(id),
    queryFn: () => api.getAssessment(id),
    enabled: !!id,
    refetchInterval: (query) => {
      const state = query.state.data?.scan_state;
      if (state && TERMINAL.includes(state)) {
        qc.invalidateQueries({ queryKey: vraKeys.dashboard(id) });
        qc.invalidateQueries({ queryKey: vraKeys.evidence(id) });
        return false;
      }
      return intervalMs;
    },
  });
}

// ---- mutations --------------------------------------------------------------

export function useCreateVra() {
  const api = useVraApi();
  const qc = useQueryClient();
  return useMutation<
    { assessment: VraAssessment; default_questions: DefaultQuestion[] },
    Error,
    { playbook_id?: string; runbook_id?: string; vendor_name?: string; vendor_domain?: string }
  >({
    mutationFn: (input) => api.createAssessment(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: vraKeys.assessments() }),
  });
}

export function useSetVendor(id: string) {
  const api = useVraApi();
  const qc = useQueryClient();
  return useMutation<VraAssessment, Error, { vendor_name?: string; vendor_domain?: string }>({
    mutationFn: (input) => api.setVendor(id, input),
    onSuccess: (rec) => qc.setQueryData(vraKeys.assessment(id), rec),
  });
}

export function useRunCollection(id: string) {
  const api = useVraApi();
  const qc = useQueryClient();
  return useMutation<CollectResult, Error, { force?: boolean } | void>({
    mutationFn: (vars) => api.runCollection(id, !!(vars && vars.force)),
    onSuccess: () => qc.invalidateQueries({ queryKey: vraKeys.assessment(id) }),
  });
}

export function useLinkRunbook(id: string) {
  const api = useVraApi();
  const qc = useQueryClient();
  return useMutation<VraAssessment, Error, { runbook_id: string; playbook_id: string }>({
    mutationFn: (input) => api.linkRunbook(id, input),
    onSuccess: (rec) => qc.setQueryData(vraKeys.assessment(id), rec),
  });
}

export function useRegenerateReport(id: string) {
  const api = useVraApi();
  return useMutation({ mutationFn: () => api.regenerateReport(id) });
}

export function useDeleteVra() {
  const api = useVraApi();
  const qc = useQueryClient();
  return useMutation<{ deleted: string }, Error, string>({
    mutationFn: (id) => api.deleteAssessment(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: vraKeys.assessments() }),
  });
}

// Convenience re-exports so a component can import types from one place.
export type { EvidenceBundle, VraDashboard };
