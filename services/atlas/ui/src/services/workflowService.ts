import { errText, USE_REAL_API } from '../api/http';
import { orchestrator } from '../api/orchestratorClient';
import { ORCHESTRATOR_URL } from '../api/axiosClient';
import { runSuffix } from './entitiesService';

/**
 * Runs on the workflow plane that are parked on a HUMAN decision. The plane lists them
 * at /v1/workflows/pending and hands back, per run, the URL that reads its status and
 * the URL that takes the decision — so nothing here hard-codes a workflow's endpoints.
 *
 * This is the workflow plane's own queue and sits alongside (not instead of) the local
 * stage-change requests on Today: those are Atlas's approvals, these are Temporal's.
 */
const PENDING_URL = '/v1/workflows/pending';

export interface PendingWorkflow {
  kind: string;            // 'deal-structuring', …
  subjectId: string;       // the deal / lead the run is about
  workflowId: string;
  status: string;          // RUNNING, …
  stage: string;           // 'Awaiting committee decision'
  requestedBy: string;
  startedAt: string;
  statusUrl?: string;
  decisionUrl?: string;
}

/** Turn an orchestrator failure into a message the UI can render verbatim. */
export function workflowError(e: any, step: string): string {
  const status = e?.response?.status;
  const asText = errText(e?.response?.data);
  if (e?.response) console.warn('[orchestrator] %s failed (%s):', step, status, e.response.data);
  if (status === 401 || status === 403) return asText || `Not permitted to ${step}.`;
  if (status === 404) return asText || 'That run is no longer on the workflow plane — refresh and try again.';
  if (status === 409) return asText || 'That run has already been decided.';
  if (status === 400 || status === 422) return asText || `The workflow rejected the request to ${step}.`;
  if (status) return asText || `The workflow plane returned ${status}.`;
  return asText || `Cannot reach the workflow plane at ${ORCHESTRATOR_URL || '(unset)'}.`;
}

function toPending(r: any): PendingWorkflow {
  return {
    kind: r?.kind || '',
    subjectId: r?.subject_id || '',
    workflowId: r?.workflow_id || '',
    status: r?.status || '',
    stage: r?.stage || '',
    requestedBy: r?.requested_by || '',
    startedAt: r?.started_at || '',
    statusUrl: r?.status_url || undefined,
    decisionUrl: r?.decision_url || undefined,
  };
}

/** 'deal-structuring' -> 'Deal structuring'. */
export function kindLabel(kind: string): string {
  const s = kind.replace(/[-_]+/g, ' ').trim();
  return s ? s[0].toUpperCase() + s.slice(1) : 'Workflow';
}

/** How long the run has been waiting — "3h ago", "2d ago". */
export function since(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const mins = Math.floor((Date.now() - t) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/** The last path segment of the decision URL — 'committee-decision' and friends. */
function decisionOf(w: PendingWorkflow): string {
  return (w.decisionUrl || '').split('?')[0].replace(/\/+$/, '').split('/').pop() || '';
}

/** Whether this run is decided at the credit committee (so it carries references). */
export function isCommitteeDecision(w: PendingWorkflow): boolean {
  return decisionOf(w) === 'committee-decision';
}

/** Committee reference in the collection's shape — CC/<year>/<runSuffix>. */
export function committeeRef(suffix = runSuffix()): string {
  return `CC/${new Date().getFullYear()}/${suffix}`;
}

/** Sanction letter reference — SL/<subject stem>/<runSuffix>. */
export function sanctionRef(subjectId: string, suffix = runSuffix()): string {
  const stem = (subjectId.split('-')[0] || 'DEAL').replace(/[^A-Za-z0-9]/g, '').toUpperCase() || 'DEAL';
  return `SL/${stem.slice(0, 16)}/${suffix}`;
}

export interface DecisionInput {
  approved: boolean;
  by: string;
  note?: string;
  committeeReference?: string;
  sanctionLetterReference?: string;
}

export const workflowService = {
  /** Whether the workflow plane is wired up at all — nothing is shown in mock mode. */
  enabled: () => USE_REAL_API && !!ORCHESTRATOR_URL,

  /** Runs waiting on a human. Never throws — a plane that is down shows an empty queue. */
  async pending(): Promise<PendingWorkflow[]> {
    if (!workflowService.enabled()) return [];
    const data = await orchestrator.get<any>(PENDING_URL);
    const rows: any[] = Array.isArray(data) ? data : (data?.pending ?? data?.items ?? []);
    return rows.map(toPending).filter((w) => w.workflowId);
  },

  /**
   * Record the human decision on a run. The body follows the endpoint the plane pointed
   * at: a committee decision also carries the committee reference, and the sanction
   * letter reference when it is an approval — an approved committee run mints one.
   */
  async decide(w: PendingWorkflow, input: DecisionInput): Promise<{ ok: boolean; error?: string }> {
    if (!w.decisionUrl) return { ok: false, error: 'This run does not expose a decision URL.' };
    const body: Record<string, any> = { approved: input.approved, by: input.by };
    if (input.note?.trim()) body.note = input.note.trim();
    if (isCommitteeDecision(w)) {
      body.committee_reference = input.committeeReference?.trim() || committeeRef();
      if (input.approved) body.sanction_letter_reference = input.sanctionLetterReference?.trim() || sanctionRef(w.subjectId);
    }
    try {
      await orchestrator.post<any>(w.decisionUrl, body);
      return { ok: true };
    } catch (e: any) {
      return { ok: false, error: workflowError(e, input.approved ? 'approve the run' : 'reject the run') };
    }
  },

  /** The run's current state, read from the URL the list handed back. */
  async status(w: PendingWorkflow): Promise<any | null> {
    if (!w.statusUrl) return null;
    try { return await orchestrator.get<any>(w.statusUrl); } catch (e) { console.warn('[orchestrator] status read failed:', e); return null; }
  },
};
