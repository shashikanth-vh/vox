import { orchestrator } from '../api/orchestratorClient';
import { errText, USE_REAL_API } from '../api/http';

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
  enabled: boolean;
  /** Present only when `enabled` is false — why not, in words for the user. */
  reason?: string;
  /** Named screen for a step a flat form cannot express; otherwise build from `form`. */
  screen?: 'cpcs-checklist' | 'handover-package';
  /** Ids and constants the plane pre-filled; merged under the form's own values. */
  body: Record<string, any>;
  form: ActionField[];
}

export interface SubjectActions {
  subject: { type: string; id: string; stage: string };
  run: { workflow_id: string; status: string; stage: string; status_url: string } | null;
  scoped_to: { email: string; roles: string[] };
  actions: WorkflowAction[];
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
    const body: Record<string, any> = { ...values, ...action.body };
    for (const f of action.form) {
      const v = values[f.name];
      if (v === '' || v === undefined || v === null) { delete body[f.name]; continue; }
      body[f.name] = f.type === 'number' ? Number(v) : v;
    }
    try {
      const data = action.method === 'PATCH'
        ? await orchestrator.patch<any>(action.url, body)
        : await orchestrator.post<any>(action.url, body);
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
