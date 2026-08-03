import { errText, USE_REAL_API } from '../api/http';
import { orchestrator } from '../api/orchestratorClient';
import { ORCHESTRATOR_URL } from '../api/axiosClient';
import { runSuffix } from './entitiesService';
import { settled } from './workflowRun';

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
  kind: string;            // 'deal-structuring', 'cpcs-checklist', …
  subjectId: string;       // the deal / lead / lending line the item is about
  // EMPTY for the register-sourced checker queues (CP/CS checklists, handover
  // packages): their prepare-workflows complete immediately and the wait lives as a
  // durable REGISTER row, so those items are keyed by subject, not by a run.
  workflowId: string;
  status: string;          // RUNNING / Completed / Prepared …
  stage: string;           // 'Awaiting committee decision'
  requestedBy: string;
  startedAt: string;
  statusUrl?: string;
  // The plane hands back the whole triad — approve, return-for-revision, reject —
  // per item. Nothing here constructs a workflow URL; the UI renders the buttons the
  // plane actually offers.
  decisionUrl?: string;    // named decision route (committee / syndication / AM)
  approveUrl?: string;
  returnUrl?: string;
  rejectUrl?: string;
  controlUrl?: string;     // run-control (return / resubmit / cancel) on a parked run
  checklistVersion?: number;
}

/** A stable key for a queue item — register rows carry no workflow id. */
export function pendingKey(w: PendingWorkflow): string {
  return w.workflowId || `${w.kind}:${w.subjectId}`;
}

export type DecisionAction = 'approve' | 'return' | 'reject';

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
    approveUrl: r?.approve_url || undefined,
    returnUrl: r?.return_url || undefined,
    rejectUrl: r?.reject_url || undefined,
    controlUrl: r?.control_url || undefined,
    checklistVersion: r?.checklist_version ?? undefined,
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
  action: DecisionAction;
  by: string;
  note?: string;
  committeeReference?: string;
  sanctionLetterReference?: string;
}

/** Which verbs this item actually offers — the buttons the UI may render. */
export function actionsFor(w: PendingWorkflow): DecisionAction[] {
  const out: DecisionAction[] = [];
  if (w.decisionUrl || w.approveUrl) out.push('approve');
  if (w.returnUrl || w.controlUrl) out.push('return');
  if (w.rejectUrl || w.decisionUrl) out.push('reject');
  return out;
}

/** A note is REQUIRED to return or reject — a refusal must say why, permanently. */
export function noteRequired(action: DecisionAction): boolean {
  return action !== 'approve';
}

export const workflowService = {
  /** Whether the workflow plane is wired up at all — nothing is shown in mock mode. */
  enabled: () => USE_REAL_API && !!ORCHESTRATOR_URL,

  /** Runs waiting on a human. Never throws — a plane that is down shows an empty queue. */
  async pending(): Promise<PendingWorkflow[]> {
    if (!workflowService.enabled()) return [];
    const data = await orchestrator.get<any>(PENDING_URL);
    const rows: any[] = Array.isArray(data) ? data : (data?.pending ?? data?.items ?? []);
    // Keep the register-sourced checker queues too: CP/CS checklists and handover
    // packages carry NO workflow id (their wait is a durable register row), and
    // dropping them hid the maker→checker items from the approver's own list.
    return rows.map(toPending).filter((w) => w.workflowId || w.subjectId);
  },

  /**
   * Record the human decision — approve, RETURN for revision, or reject — on whichever
   * lane this item lives on. The plane hands back the URLs; this picks the one for the
   * action and speaks that endpoint's body shape:
   *
   * * named decision route (committee / syndication / AM): `{approved, by, note, …refs}`
   * * conversion approve/reject: `{by, note}`
   * * checker queues (CP/CS, handover): `{approved_by | returned_by | rejected_by, note}`
   * * return on a PARKED run: run-control `{action:"return", by, note}` — the run goes
   *   back to its requester, non-terminal, and the SLA clock restarts on resubmit.
   */
  async decide(w: PendingWorkflow, input: DecisionInput): Promise<{ ok: boolean; error?: string }> {
    const { action } = input;
    const note = input.note?.trim() || '';
    if (noteRequired(action) && !note) {
      return { ok: false, error: `A note is required to ${action} — say why, for the record.` };
    }
    // Pick the URL + body for this action.
    let url: string | undefined;
    let body: Record<string, any>;
    const checkerQueue = w.kind === 'cpcs-checklist' || w.kind === 'advaya-handover';
    if (action === 'return') {
      url = w.returnUrl || w.controlUrl;
      body = checkerQueue
        ? { returned_by: input.by, note }
        : { action: 'return', by: input.by, note };
    } else if (checkerQueue) {
      url = action === 'approve' ? w.approveUrl : w.rejectUrl;
      body = action === 'approve'
        ? { approved_by: input.by, ...(note ? { note } : {}) }
        : { rejected_by: input.by, note };
    } else if (w.decisionUrl) {
      url = w.decisionUrl;
      body = { approved: action === 'approve', by: input.by, ...(note ? { note } : {}) };
      if (isCommitteeDecision(w)) {
        body.committee_reference = input.committeeReference?.trim() || committeeRef();
        if (action === 'approve') {
          body.sanction_letter_reference =
            input.sanctionLetterReference?.trim() || sanctionRef(w.subjectId);
        }
      }
    } else {
      url = action === 'approve' ? w.approveUrl : w.rejectUrl;
      body = { by: input.by, ...(note ? { note } : {}) };
    }
    if (!url) return { ok: false, error: `This item does not offer "${action}".` };
    try {
      const data = await orchestrator.post<any>(url, body);
      // The POST only records the DECISION. Everything the approval is FOR — converting a
      // lead into a deal and a lending line, minting the evidence, moving the stage — runs
      // afterwards, inside the workflow. If one of those activities is refused the run dies
      // and, without this watch, the approver is told the item was approved and never learns
      // that Deals and Lending stayed empty. So follow the run briefly and report what it did.
      const failure = await settled(data?.workflow_id || w.workflowId, w.statusUrl);
      if (failure) return { ok: false, error: failure };
      return { ok: true };
    } catch (e: any) {
      return { ok: false, error: workflowError(e, `${action} this item`) };
    }
  },

  /** The run's current state, read from the URL the list handed back. */
  async status(w: PendingWorkflow): Promise<any | null> {
    if (!w.statusUrl) return null;
    try { return await orchestrator.get<any>(w.statusUrl); } catch (e) { console.warn('[orchestrator] status read failed:', e); return null; }
  },
};
