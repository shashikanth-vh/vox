import { api, errText, listAll } from '../api/http';
import { orchestrator } from '../api/orchestratorClient';

/**
 * The CAM workbench + sanction terms — the client half of lending increments ① and ②.
 *
 * Two planes, deliberately:
 *  * the ORCHESTRATOR drafts (generate / refine / finalise) — it owns the engine seam
 *    (Claude today, anything tomorrow) and reads the documents as the caller;
 *  * the REGISTER holds the record — versions, turns, the committee decision, the
 *    sanctioned terms — and enforces maker-checker.
 *
 * This service never composes credit judgement: it moves the analyst's choices to the
 * planes that act on them, and errors come back in the words the plane used.
 */

export interface CamReport {
  id: string;
  lending_id: string;
  report_version: number;
  status: 'Draft' | 'Submitted' | 'Approved' | 'Returned' | 'Rejected';
  engine?: string;
  source_doc_ids: string[];
  prompt_doc_id?: string;
  draft_md?: string;
  document_id?: string;
  prepared_by?: string;
  decided_by?: string;
  decision_note?: string;
  updated_at?: string;
  turns?: { turn_no: number; role: 'user' | 'assistant'; content: string }[];
}

export interface EntityDoc {
  id: string;
  title: string;
  section: string;
  doc_type: string;
  content_type: string;
  status: string;
}

export interface SanctionTermsOut {
  id: string;
  lending_id: string;
  amount_cr?: number;
  rate_kind: string;
  rate_pct?: number;
  spread_pct?: number;
  tenor_months?: number;
  emi_amount?: number;
  repayment_start?: string;
  day_count: string;
  penal_rate_pct?: number;
  moratorium_months: number;
  schedule_kind: string;
  cp_items: any[];
  cs_items: any[];
  covenants: any[];
  seeded_checklist_id?: string;
  seeded_covenant_ids: string[];
  note?: string;
}

function msg(e: any, what: string): string {
  const detail = errText(e?.response?.data);
  if (e?.response) console.warn('[cam] %s failed (%s):', what, e.response.status, e.response.data);
  return detail || e?.message || `Could not ${what}.`;
}

export const camService = {
  /** Every document on the company's file, flat — the pickers choose from these. */
  async entityDocs(entityId: string): Promise<EntityDoc[]> {
    const rows = await listAll(`/entities/${entityId}/documents`, { key: 'documents' });
    return rows
      .filter((r: any) => String(r.status) !== 'Superseded')
      .map((r: any): EntityDoc => ({
        id: r.id, title: r.title || r.original_filename || 'document',
        section: r.section || '', doc_type: r.doc_type || '',
        content_type: r.content_type || '', status: r.status || '',
      }));
  },

  /** The line's CAM history, oldest version first. */
  async list(lendingId: string): Promise<CamReport[]> {
    try {
      return await api.get<CamReport[]>('/internal/cam-reports', { lending_id: lendingId });
    } catch (e) { throw new Error(msg(e, 'read the CAM history')); }
  },

  /** One version with its full workbench transcript. */
  async get(reportId: string): Promise<CamReport> {
    try {
      return await api.get<CamReport>(`/internal/cam-reports/${reportId}`);
    } catch (e) { throw new Error(msg(e, 'read that CAM version')); }
  },

  /** Draft a new version from the selected documents + the brief (a prompt document
   *  or text typed in the workbench — exactly one). */
  async generate(lendingId: string, input: {
    source_doc_ids: string[]; prompt_doc_id?: string; prompt_text?: string;
    deal_id?: string;
  }): Promise<{ report_id: string; draft_md: string; engine: string;
                included: any[]; skipped: any[] }> {
    try {
      return await orchestrator.post<any>(`/v1/cam/${lendingId}/generate`, input);
    } catch (e) { throw new Error(msg(e, 'draft the CAM')); }
  },

  /** Rework the open draft with a further instruction. */
  async refine(lendingId: string, instruction: string): Promise<{ draft_md: string }> {
    try {
      return await orchestrator.post<any>(`/v1/cam/${lendingId}/refine`, { instruction });
    } catch (e) { throw new Error(msg(e, 'rework the draft')); }
  },

  /** File the draft to the Data Register and submit it to the committee. */
  async finalise(lendingId: string, title?: string): Promise<{ document_id: string }> {
    try {
      return await orchestrator.post<any>(`/v1/cam/${lendingId}/finalise`,
        title ? { title } : {});
    } catch (e) { throw new Error(msg(e, 'finalise the CAM')); }
  },

  /**
   * The committee's verb — through the orchestrator's triad (same door as the CP/CS
   * checker), so authority and four-eyes are enforced where every other decision is.
   */
  async decide(reportId: string, decision: 'Approved' | 'Returned' | 'Rejected',
               by: string, note: string): Promise<CamReport> {
    const path = decision === 'Approved' ? 'approve'
      : decision === 'Returned' ? 'return' : 'reject';
    const body = decision === 'Approved'
      ? { approved_by: by, ...(note ? { note } : {}) }
      : decision === 'Returned' ? { returned_by: by, note } : { rejected_by: by, note };
    try {
      return await orchestrator.post<any>(`/v1/workflows/cam-reports/${reportId}/${path}`, body);
    } catch (e) { throw new Error(msg(e, `${path} the CAM`)); }
  },

  /**
   * Save a HAND-EDITED draft: the analyst may polish the text directly between engine
   * turns. Recorded as a workbench turn (the transcript stays the audit answer to
   * "why does the CAM say this?") whose draft becomes the current version.
   */
  async saveDraft(reportId: string, draftMd: string): Promise<void> {
    try {
      await api.post(`/internal/cam-reports/${reportId}/turns`, {
        role: 'user', content: '[manual edit] The analyst edited the draft directly.',
        draft_md: draftMd,
      });
    } catch (e) { throw new Error(msg(e, 'save the edited draft')); }
  },

  /** The tenant's default template of one kind (cam_prompt, cam_example, …) — null
   *  when the deployment ships none and nobody uploaded one. */
  async template(docType: string): Promise<{ id: string; title: string } | null> {
    try {
      return await api.get<any>(`/templates/${docType}`);
    } catch (e: any) {
      if (e?.response?.status === 404) return null;
      console.warn('[cam] template read failed:', docType, e);
      return null;
    }
  },

  /** Put a prompt (or any workbench input) on the lending line's file. */
  async uploadDoc(lendingId: string, file: File, docType: string): Promise<{ id: string }> {
    const form = new FormData();
    form.append('file', file);
    form.append('section', 'CAM');
    form.append('title', file.name.replace(/\.[^.]+$/, ''));
    form.append('doc_type', docType);
    form.append('status', 'On File');
    try {
      return await api.post<any>(`/lending/${lendingId}/documents/upload`, form);
    } catch (e) { throw new Error(msg(e, 'upload that document')); }
  },

  /** The sanctioned terms for a line — null when none have been entered yet. */
  async terms(lendingId: string): Promise<SanctionTermsOut | null> {
    try {
      return await api.get<SanctionTermsOut>('/internal/sanction-terms',
        { lending_id: lendingId });
    } catch (e: any) {
      if (e?.response?.status === 404) return null;
      throw new Error(msg(e, 'read the sanction terms'));
    }
  },

  /** Enter the terms once — the register seeds the CP/CS checklist + covenants. */
  async createTerms(body: Record<string, any>): Promise<SanctionTermsOut> {
    try {
      return await api.post<SanctionTermsOut>('/internal/sanction-terms', body);
    } catch (e) { throw new Error(msg(e, 'save the sanction terms')); }
  },
};
