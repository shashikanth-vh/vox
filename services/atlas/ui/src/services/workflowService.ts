import { api, errText, USE_REAL_API } from '../api/http';
import { orchestrator } from '../api/orchestratorClient';
import { ORCHESTRATOR_URL } from '../api/axiosClient';
import { db } from '../api/atlasStore';
import { clientsService } from './clientsService';
import { runSuffix } from './entitiesService';
import { settled } from './workflowRun';
import { latestBy } from '../api/latest';

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
  /** The BUSINESS state, separate from the technical stage — 'ReturnedForInformation'
   *  means the run is parked with its REQUESTER, not awaiting an approver. */
  businessStatus?: string;
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
  // Reminder rows (cs-followup / covenant-due): what the client needs to CLOSE the
  // cycle in place — the observation to record against, and whether a figure is owed.
  monitoringId?: string;
  metric?: string;
  covenantName?: string;
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
    businessStatus: r?.business_status || undefined,
    requestedBy: r?.requested_by || '',
    startedAt: r?.started_at || '',
    statusUrl: r?.status_url || undefined,
    decisionUrl: r?.decision_url || undefined,
    approveUrl: r?.approve_url || undefined,
    returnUrl: r?.return_url || undefined,
    rejectUrl: r?.reject_url || undefined,
    controlUrl: r?.control_url || undefined,
    checklistVersion: r?.checklist_version ?? undefined,
    monitoringId: r?.monitoring_id || undefined,
    metric: r?.metric || undefined,
    covenantName: r?.covenant_name || undefined,
  };
}

/** 'deal-structuring' -> 'Deal structuring'. */
export function kindLabel(kind: string): string {
  const s = kind.replace(/[-_]+/g, ' ').trim();
  return s ? s[0].toUpperCase() + s.slice(1) : 'Workflow';
}

// The approver's queue speaks the DESK's language, not the engine's: the run kinds are
// internal identifiers, so each gets its business name. In the house process the CAM is
// sent to the Credit Committee, and the committee's outcome is the Credit note — hence
// a deal-structuring decision is chip-labelled by WHO decides it.
const KIND_CHIP: Record<string, string> = {
  'lead-conversion': 'Push to Deals',
  'deal-structuring': 'Credit committee',
  'cam-report': 'CAM report',
  'cpcs-checklist': 'CP/CS checklist',
  'syndication': 'Platform Deals mandate',
  'asset-monetisation': 'Asset closure',
  'advaya-handover': 'Disbursement handover',
  'cs-followup': 'CS chase',
  'covenant-due': 'Covenant',
};
export const kindChip = (kind: string): string => KIND_CHIP[kind] || kindLabel(kind);

/** The COMPANY a queue item is about, resolved from whichever store row carries the
 *  subject — the first thing an approver scans for. Fail-soft: an unhydrated store
 *  simply yields '' and the row falls back to its stage text. */
export function subjectName(w: PendingWorkflow): string {
  const sid = w.subjectId;
  if (!sid) return '';
  const lead = (db().leads || []).find((l: any) => l.apiId === sid || l.id === sid);
  if (lead?.company) return lead.company;
  for (const key of ['deals', 'syn', 'lending', 'am'] as const) {
    const row = (db()[key] || []).find((r: any) => r.apiId === sid || r.id === sid);
    if (row) return row._name || clientsService.get(row.code).name || row.code || '';
  }
  return '';
}

/** A person's display name for 'requested by' lines — the roster's short handle when
 *  the email is on record, else the email's name part, title-cased. Nobody should have
 *  to read priya.nair@evamfinance.com in a work queue. */
export function personName(email: string): string {
  const e = (email || '').trim();
  if (!e) return '';
  const p = (db().people || []).find(
    (x: any) => (x.email || '').toLowerCase() === e.toLowerCase());
  if (p?.name || p?.full) return p.name || p.full;
  if (!e.includes('@')) return e;
  return e.split('@')[0].replace(/[._-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Engine states ('Pending', 'RUNNING') teach the user nothing — being listed already
 *  means "waiting". A stage is shown only when it says something businessful. */
export const businessStage = (stage: string | undefined): string => {
  const s = (stage || '').trim();
  return /^(pending|running|started|starting|completed)$/i.test(s) ? '' : s;
};

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

/** A run the committee sent back — it sits with the REQUESTER until resubmitted. */
export function isReturned(w: PendingWorkflow): boolean {
  return (w.businessStatus || '').toLowerCase().includes('return');
}

/** Which verbs this item actually offers — the buttons the UI may render.
 *
 * TWO on purpose (field decision): Approve or Reject. The three-verb model's Return
 * lane confused more than it helped — a rejection carries the mandatory note, the
 * maker is notified with it, amends, and raises a FRESH request (the plane restarts
 * a closed run cleanly). The return machinery stays server-side for anything already
 * parked as returned — those runs still resolve through their resubmit surfaces. */
export function actionsFor(w: PendingWorkflow): DecisionAction[] {
  // Legacy runs parked as "returned" (from before the two-verb model) offer the same
  // two buttons: the approver clears them with a plain Reject — stage rolls back, the
  // maker starts fresh. One flow for everything.
  const out: DecisionAction[] = [];
  if (w.decisionUrl || w.approveUrl) out.push('approve');
  if (w.rejectUrl || w.decisionUrl) out.push('reject');
  return out;
}

/** A note is REQUIRED to return or reject — a refusal must say why, permanently. */
export function noteRequired(action: DecisionAction): boolean {
  return action !== 'approve';
}

/** What the approver is actually deciding — in business words, read from the subject. */
export interface ApprovalContext {
  headline: string;
  facts: [string, string][];
  /** Long-form content to verify (a CAM draft, a checklist) — rendered scrollable. */
  preview?: string;
  /** The filed artefact itself (an uploaded CAM .docx) — the dialog offers a download. */
  document?: { id: string; name: string };
  /** The company whose Data Register holds the collected documents — set when the
   *  decision turns on documents (CP/CS check, handover), so the approver can open and
   *  verify them without leaving the review. The entity id addresses the register
   *  directly; the code is the display key. */
  dataRegisterCode?: string;
  dataRegisterEntityId?: string;
}

const rows = (x: any): any[] => (Array.isArray(x) ? x : (x?.items ?? []));

/**
 * An approval card that shows only ids asks the approver to decide blind — they had to
 * open the register themselves to learn what the request even was. This reads the
 * SUBJECT (and, for register-queue kinds, the artefact awaiting the check) and answers
 * "what am I approving?" in the words the business uses. Best-effort: a failed read
 * returns null and the dialog still works — ids are the fallback, not the headline.
 */
export async function approvalContext(w: PendingWorkflow): Promise<ApprovalContext | null> {
  const clean = (facts: [string, any][]): [string, string][] =>
    facts.filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== '')
      .map(([l, v]) => [l, String(v)]);
  try {
    if (w.kind === 'lead-conversion') {
      const lead = await api.get<any>(`/leads/${w.subjectId}`);
      return {
        headline: `Convert ${lead.company || 'this lead'} into a client, deal and lending line.`,
        facts: clean([['Company', lead.company], ['RM', lead.rm], ['Sector', lead.sector],
          ['Temperature', lead.temperature], ['Source', lead.source],
          ['Next action', lead.next_action],
          // The qualification stamp appends its internal run id to the notes —
          // plumbing, not information; the approver reads words only.
          ['Notes', String(lead.notes || '').replace(/\s*\(workflow [^)]*\)\.?/g, '')]]),
      };
    }
    if (w.kind === 'deal-structuring') {
      const deal = await api.get<any>(`/deals/${w.subjectId}`);
      const ent = deal?.entity_id
        ? await api.get<any>(`/entities/${deal.entity_id}`).catch(() => null) : null;
      // The CAM prepared for this decision: the deal's lending line carries the filed
      // document (and/or the in-app draft) — the committee reads it HERE, on the card.
      let cam: any = null;
      let document: { id: string; name: string } | undefined;
      try {
        const lendRows = rows(await api.get<any>('/lending',
          { deal_id: w.subjectId, limit: 10 }).catch(() => []));
        for (const line of lendRows) {
          const cams = rows(await api.get<any>('/internal/cam-reports',
            { lending_id: line.id }).catch(() => []));
          const withContent = cams.filter((r: any) => r.draft_md || r.document_id).pop();
          if (withContent) {
            cam = withContent;
            if (cam.document_id) {
              const docs = rows(await api.get<any>(`/lending/${line.id}/documents`)
                .catch(() => []));
              const d = docs.find((x: any) => x.id === cam.document_id);
              const ct = String(d?.content_type || '');
              const ext = ct.includes('wordprocessingml') ? '.docx' : ct.includes('pdf') ? '.pdf'
                : ct.includes('markdown') ? '.md' : ct.includes('plain') ? '.txt' : '';
              document = { id: String(cam.document_id),
                name: `${d?.title || `CAM v${cam.report_version}`}${ext}` };
            }
            break;
          }
        }
      } catch { /* the card still works without the CAM */ }
      return {
        headline: `Credit committee decision on ${ent?.display_name || ent?.legal_name || 'this deal'}.`
          + (document ? ' The prepared CAM is attached below.' : ''),
        facts: clean([['Company', ent?.legal_name], ['Product', deal.product_type],
          ['Amount ₹ Cr', deal.amount_cr], ['RM', deal.rm], ['Analyst', deal.analyst],
          ['Stage', deal.stage],
          ['CAM', cam ? `v${cam.report_version} · prepared by ${cam.prepared_by || '—'}` : undefined],
          ['Filed document', document?.name]]),
        preview: cam?.draft_md || undefined,
        document,
      };
    }
    if (['cpcs-checklist', 'cam-report', 'advaya-handover'].includes(w.kind)) {
      const lending = await api.get<any>(`/lending/${w.subjectId}`).catch(() => null);
      const ent = lending?.entity_id
        ? await api.get<any>(`/entities/${lending.entity_id}`).catch(() => null) : null;
      const base: [string, any][] = [['Company', ent?.legal_name],
        ['Stage', lending?.stage], ['Amount ₹ Cr', lending?.amount_cr]];
      if (w.kind === 'cpcs-checklist') {
        const all = rows(await api.get<any>('/internal/cpcs-checklists',
          { lending_id: w.subjectId }).catch(() => []));
        // Highest version — the checklist list arrives created_at DESC, so `.pop()`
        // showed the checker the OLDEST completed version of the conditions.
        const row = latestBy(all.filter((r: any) => r.status === 'Completed'),
          'checklist_version');
        const items: any[] = row?.items || [];
        const done = items.filter((i: any) => i.status === 'Completed').length;
        return {
          headline: `CP/CS checklist v${row?.checklist_version ?? w.checklistVersion ?? '?'} — every condition and its evidence, awaiting your check.`,
          facts: clean([...base, ['Prepared by', row?.prepared_by || w.requestedBy],
            ['Conditions', items.length ? `${done}/${items.length} completed` : undefined]]),
          preview: items.map((i: any) =>
            `${i.condition_type || 'CP'} · ${i.label || i.key} — ${i.status}`
            + (i.reason ? ` (${i.reason})` : '')
            + (i.evidence_ref ? `  [evidence: ${i.evidence_ref}]` : '')).join('\n') || undefined,
          dataRegisterCode: ent?.code || undefined,
          dataRegisterEntityId: ent?.id ? String(ent.id) : undefined,
        };
      }
      if (w.kind === 'cam-report') {
        const all = rows(await api.get<any>('/internal/cam-reports',
          { lending_id: w.subjectId }).catch(() => []));
        const row = all.filter((r: any) => r.status === 'Submitted').pop();
        // An analyst who finished the CAM in Word filed a DOCUMENT — that file is what
        // the committee reads, so name it and offer it for download.
        let document: { id: string; name: string } | undefined;
        if (row?.document_id) {
          const docs = rows(await api.get<any>(`/lending/${w.subjectId}/documents`)
            .catch(() => []));
          const d = docs.find((x: any) => x.id === row.document_id);
          const ct = String(d?.content_type || '');
          const ext = ct.includes('wordprocessingml') ? '.docx' : ct.includes('pdf') ? '.pdf'
            : ct.includes('markdown') ? '.md' : ct.includes('plain') ? '.txt' : '';
          document = { id: String(row.document_id),
            name: `${d?.title || `CAM v${row.report_version}`}${ext}` };
        }
        return {
          headline: `CAM v${row?.report_version ?? '?'} submitted for the committee — `
            + (row?.draft_md ? 'the draft is below.' : 'the filed document is attached.'),
          facts: clean([...base, ['Prepared by', row?.prepared_by || w.requestedBy],
            ['Filed document', document?.name]]),
          preview: row?.draft_md || undefined,
          document,
        };
      }
      const pkg = await api.get<any>(`/lending/${w.subjectId}/handover-package`).catch(() => null);
      return {
        headline: 'Advaya handover package — the documents and recipient, awaiting your check.',
        facts: clean([...base, ['Recipient', pkg?.recipient],
          ['Delivery', pkg?.delivery_method],
          ['Documents', (pkg?.documents || []).length || undefined],
          ['Prepared by', pkg?.prepared_by || w.requestedBy]]),
        dataRegisterCode: ent?.code || undefined,
        dataRegisterEntityId: ent?.id ? String(ent.id) : undefined,
      };
    }
    if (w.kind === 'syndication' || w.kind === 'asset-monetisation') {
      const path = w.kind === 'syndication' ? 'syndication' : 'asset-monetisation';
      const row = await api.get<any>(`/${path}/${w.subjectId}`).catch(() => null);
      const ent = row?.entity_id
        ? await api.get<any>(`/entities/${row.entity_id}`).catch(() => null) : null;
      return row && {
        headline: `${kindLabel(w.kind)} decision on ${ent?.legal_name || 'this mandate'}.`,
        facts: clean([['Company', ent?.legal_name], ['Status', row.status],
          ['RM', row.rm], ['Analyst', row.analyst]]),
      };
    }
    return null;
  } catch (e) {
    console.warn('[workflows] approval context unavailable for %s:', w.kind, e);
    return null;
  }
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
    const checkerQueue = w.kind === 'cpcs-checklist' || w.kind === 'advaya-handover'
      || w.kind === 'cam-report';
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
