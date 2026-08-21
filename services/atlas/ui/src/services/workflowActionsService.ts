import { orchestrator } from '../api/orchestratorClient';
import { gwClient } from '../api/axiosClient';
import { errText, USE_REAL_API } from '../api/http';
import { settled } from './workflowRun';

/**
 * The MAKER's half of the workflow plane, mirroring `workflowService` (the approver's).
 *
 * Neither service knows what a CP/CS checklist *is*. The plane answers what this user may
 * do to this line right now — label, method, URL, the form to collect — and the UI renders
 * it. That is deliberate: a client that decides for itself which buttons to show ends up
 * offering steps the platform refuses, which is precisely what the Lending stage dropdown
 * did when it listed four stages the register would always reject.
 *
 * An unavailable action still comes back, carrying `reason`. It is rendered disabled with
 * that text, because the sequence is the thing a user most needs to learn.
 */

export type FieldType = 'text' | 'textarea' | 'number' | 'date' | 'select';

export interface ActionField {
  name: string;
  label: string;
  type: FieldType;
  required?: boolean;
  options?: string[];
  placeholder?: string;
  default?: any;
  help?: string;
}

export interface WorkflowAction {
  key: string;
  label: string;
  method: 'POST' | 'PATCH';
  url: string;
  /**
   * Which service answers `url`. The catalogue spans both planes — starting a workflow is
   * the orchestrator's, filing evidence is the register's — and sending every action to
   * the orchestrator 404s the register ones. The plane is the plane's to declare.
   */
  plane?: 'orchestrator' | 'register';
  enabled: boolean;
  /** Present only when `enabled` is false — why not, in words for the user. */
  reason?: string;
  /** Named screen for a step a flat form cannot express; otherwise build from `form`. */
  screen?: 'cpcs-checklist' | 'handover-package' | 'executed-agreement'
    | 'cam-workbench' | 'sanction-terms' | 'disburse';
  /** Ids and constants the plane pre-filled; merged under the form's own values. */
  body: Record<string, any>;
  /**
   * Document refs the client must INCLUDE in what it sends — the handover package has to
   * cite the executed agreement's digest, and the register reconciles the submitted refs
   * against the evidence on file. Kept out of `body` because the client adds to these
   * rather than replacing them.
   */
  evidence_refs?: { reference: string; sha256: string }[];
  form: ActionField[];
}

/**
 * One box of the credit-pipeline strip, coloured server-side from the same facts that
 * gate the actions — the client renders it blind and can never disagree with the verbs.
 */
export interface PipelineStep {
  key: string;
  label: string;
  state: 'done' | 'active' | 'rejected' | 'pending';
  note?: string;
  /** The steps after the fork — Disbursement and CP/CS run side by side. */
  parallel?: boolean;
}

export interface SubjectActions {
  subject: { type: string; id: string; stage: string };
  run: { workflow_id: string; status: string; stage: string; status_url: string } | null;
  scoped_to: { email: string; roles: string[] };
  actions: WorkflowAction[];
  /** Present for lending lines only — the other books have no committee spine. */
  pipeline?: PipelineStep[];
}

export type SubjectType = 'Lending' | 'Syndication' | 'AssetMonetisation';

/** Empty (rather than throwing) when the plane cannot be reached — the drawer still opens. */
const NOTHING: SubjectActions = {
  subject: { type: '', id: '', stage: '' }, run: null,
  scoped_to: { email: '', roles: [] }, actions: [],
};

export const workflowActionsService = {
  async forSubject(subjectType: SubjectType, subjectId: string): Promise<SubjectActions> {
    if (!USE_REAL_API || !subjectId) return NOTHING;
    try {
      return await orchestrator.get<SubjectActions>('/v1/workflows/actions', {
        subject_type: subjectType, subject_id: subjectId,
      });
    } catch (e: any) {
      console.warn('[workflows] actions unavailable for %s %s:', subjectType, subjectId, e);
      return NOTHING;
    }
  },

  /**
   * Perform one action. The body is the plane's pre-fill merged with what the user typed —
   * in that order, so a form field can never overwrite the subject id it was issued with.
   */
  async run(action: WorkflowAction, values: Record<string, any>): Promise<{ ok: boolean; error?: string; data?: any }> {
    // `action.body` LAST, always: it carries the subject ids and the identity the plane
    // filled from the verified caller, and no form value may overwrite either.
    const body: Record<string, any> = { ...values, ...action.body };
    for (const f of action.form) {
      const v = values[f.name];
      if (v === '' || v === undefined || v === null) { delete body[f.name]; continue; }
      body[f.name] = f.type === 'number' ? Number(v) : v;
    }
    // A bespoke screen (the CP/CS checklist, the handover package) passes a payload the
    // catalogue's flat `form` cannot describe; those keys pass through untouched.
    Object.entries(values).forEach(([k, v]) => {
      if (!action.form.some((f) => f.name === k) && v !== undefined) body[k] = v;
    });
    Object.assign(body, action.body);
    // The register plane is reached through the ORIGIN-rooted client: its urls already
    // carry the /v1 prefix, so the register client (based at /v1) would double it.
    const send = async (): Promise<any> => {
      if (action.plane === 'register') {
        const r = action.method === 'PATCH'
          ? await gwClient.patch<any>(action.url, body)
          : await gwClient.post<any>(action.url, body);
        return r.data;
      }
      return action.method === 'PATCH'
        ? orchestrator.patch<any>(action.url, body)
        : orchestrator.post<any>(action.url, body);
    };
    try {
      const data = await send();
      // A 202 means the RUN STARTED, not that the step succeeded. A CP/CS checklist that
      // the register refused failed inside its workflow seconds later, while the screen
      // said "sent for checking" and nothing ever reached the approver's queue. So when a
      // run id comes back, watch it briefly and report what actually happened.
      const failure = await settled(data?.workflow_id);
      if (failure) return { ok: false, error: failure };
      return { ok: true, data };
    } catch (e: any) {
      const status = e?.response?.status;
      const detail = errText(e?.response?.data);
      console.warn('[workflows] %s failed (%s):', action.key, status, e?.response?.data ?? e);
      if (status === 401 || status === 403) return { ok: false, error: detail || 'You are not permitted to do this.' };
      if (status === 409) return { ok: false, error: detail || 'That step has already been taken.' };
      if (status === 422) return { ok: false, error: detail || 'The workflow plane rejected those details.' };
      return { ok: false, error: detail || `The workflow plane returned ${status ?? 'no response'}.` };
    }
  },

  /** Fields that are required but empty — the dialog blocks on these. */
  missing(action: WorkflowAction, values: Record<string, any>): string[] {
    return action.form
      .filter((f) => f.required && !String(values[f.name] ?? '').trim())
      .map((f) => f.label);
  },
};
