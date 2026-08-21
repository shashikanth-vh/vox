import { api, apiErr, listAll } from '../api/http';
import { newestByTime } from '../api/latest';
import { orchestrator } from '../api/orchestratorClient';

// Session-lived cache of engine reads, keyed per (document, credit note) — see
// extractTerms. A filed letter is immutable, so a repeat read is a repeat answer.
const extractCache = new Map<string, any>();

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
  /** When the file landed. Carried so callers can pick the LATEST of a doc_type by
   *  comparing dates instead of trusting the array's order — see `newestOf`. */
  uploaded_at?: string;
  created_at?: string;
}

/**
 * The most recently filed document of a type — e.g. THE sanction letter.
 *
 * Not `docs.filter(...).pop()`: the register orders documents `uploaded_at DESC`, so
 * that returned the OLDEST letter on file. See api/latest.ts for the full account.
 */
export function newestOf(docs: EntityDoc[], docType: string): EntityDoc | null {
  return newestByTime(docs.filter((d) => d.doc_type === docType));
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
  if (e?.response) console.warn('[cam] %s failed (%s):', what, e.response.status, e.response.data);
  // apiErr, not errText: the CAM lane is the one that runs for minutes against an
  // external engine, so it is the likeliest to meet an edge failure with no envelope in
  // it — and "Request failed with status code 502" is the least useful thing the desk
  // could be told about a letter it just waited on.
  return apiErr(e, what);
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
        uploaded_at: r.uploaded_at || undefined, created_at: r.created_at || undefined,
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

  /** The text the engine will actually read from one document — for the workbench's
   *  preview pane (and for anyone who wants to copy it out). */
  async docText(docId: string): Promise<{ text: string; reason?: string;
                                          truncated?: boolean; attachable?: boolean }> {
    try {
      return await orchestrator.get<any>('/v1/cam/doc-text', { doc_id: docId },
        { timeoutMs: 60_000 });
    } catch (e) { throw new Error(msg(e, 'read that document')); }
  },

  /** Draft a new version from the selected documents + the brief (a prompt document
   *  or text typed in the workbench — exactly one). */
  async generate(lendingId: string, input: {
    source_doc_ids: string[]; prompt_doc_id?: string; prompt_text?: string;
    deal_id?: string;
  }): Promise<{ report_id: string; draft_md: string; engine: string;
                included: any[]; skipped: any[] }> {
    try {
      // Reading several documents and streaming a full memo takes minutes — the
      // whole chain (edge 625s, gateway 600s) is budgeted for ten of them.
      return await orchestrator.post<any>(`/v1/cam/${lendingId}/generate`, input,
        { timeoutMs: 620_000 });
    } catch (e) { throw new Error(msg(e, 'draft the CAM')); }
  },

  /**
   * Continue the conversation. updateDraft=true reworks the draft (the reply replaces
   * it); false is an ASK — the reply comes back for the analyst to mine, the working
   * draft stays theirs. Both land on the transcript.
   */
  async refine(lendingId: string, instruction: string, updateDraft = true,
               sourceDocIds: string[] = []): Promise<{ draft_md: string;
                 updated_draft?: boolean; documents?: any[] }> {
    try {
      return await orchestrator.post<any>(`/v1/cam/${lendingId}/refine`,
        { instruction, update_draft: updateDraft,
          ...(sourceDocIds.length ? { source_doc_ids: sourceDocIds } : {}) },
        { timeoutMs: 620_000 });
    } catch (e) { throw new Error(msg(e, updateDraft ? 'rework the draft' : 'ask the engine')); }
  },

  /** The box content rendered as a Word file — headings, lists, tables styled like
   *  the CAM template. Downloads in the browser; the analyst reviews it in Word and
   *  uploads it through the normal completed-CAM lane. */
  async exportDocx(lendingId: string, markdown: string, title?: string): Promise<void> {
    try {
      const blob = await orchestrator.postBlob(`/v1/cam/${lendingId}/export-docx`,
        { markdown, ...(title ? { title } : {}) }, { timeoutMs: 60_000 });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `${(title || 'CAM').replace(/[\\/:*?"<>|]+/g, '_')}.docx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { throw new Error(msg(e, 'render the Word file')); }
  },

  /** File the draft to the Data Register and submit it to the committee. Passing a
   *  documentId instead submits that ALREADY-UPLOADED file (the Word-template lane) —
   *  it becomes the committee copy, with or without an in-app draft. */
  async finalise(lendingId: string, title?: string,
                 documentId?: string): Promise<{ document_id: string }> {
    try {
      return await orchestrator.post<any>(`/v1/cam/${lendingId}/finalise`,
        { ...(title ? { title } : {}), ...(documentId ? { document_id: documentId } : {}) },
        { timeoutMs: 120_000 });
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

  /**
   * Refresh means FRESH: wipe the recorded conversation and the working draft, so the
   * engine — which rebuilds its memory from the transcript on every question — starts
   * from nothing. The reset itself stays in the audit log.
   */
  async resetConversation(reportId: string): Promise<void> {
    try {
      await api.post(`/internal/cam-reports/${reportId}/reset`);
    } catch (e) { throw new Error(msg(e, 'reset the conversation')); }
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

  /** The engine FILLS the sanction-letter template (structure + wording) with the
   *  CAM's / credit note's / typed terms' figures and the browser downloads the
   *  result as .docx — the analyst edits it in Word and uploads the signed letter. */
  async draftLetter(lendingId: string, input: {
    template_doc_id: string; cam_doc_id?: string; credit_note?: string;
    terms?: Record<string, string>;
  }): Promise<void> {
    try {
      const blob = await orchestrator.postBlob(`/v1/cam/${lendingId}/draft-letter`,
        input, { timeoutMs: 620_000 });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'Sanction letter - draft.docx';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { throw new Error(msg(e, 'draft the sanction letter')); }
  },

  /** EVERY template of one kind, newest first — the prompt picker's options. Falls
   *  back to the single newest on an older register that lacks the list route. */
  async templates(docType: string): Promise<{ id: string; title: string }[]> {
    try {
      const rows = await api.get<any[]>(`/templates/${docType}/all`);
      return (rows || []).map((r) => ({ id: r.id, title: r.title }));
    } catch {
      const one = await this.template(docType);
      return one ? [one] : [];
    }
  },

  /** File a NEW default template version (credit-desk authority) — it becomes the
   *  picker's newest entry under the given title (or the file's own name). */
  async uploadTemplate(docType: string, file: File,
                       title?: string): Promise<{ id: string; title: string }> {
    const form = new FormData();
    form.append('file', file);
    if (title?.trim()) form.append('title', title.trim());
    return await api.post<any>(`/templates/${docType}`, form);
  },

  /** Rename a template document — the pickers show this name. */
  async renameTemplate(id: string, title: string): Promise<{ id: string; title: string }> {
    return await api.patch<any>(`/templates/${id}`, { title });
  },

  /** Engine-read the sanction letter (plus the committee's credit note when given):
   *  CP / CS / covenant lists AND the numeric terms, as data for the forms to pre-fill.
   *  The analyst reviews before anything is seeded.
   *
   *  CACHED PER DOCUMENT for the session: the letter is immutable once filed (a revised
   *  letter is a NEW document with a new id), so the engine's read never changes — and
   *  the checklist auto-fills on open now, which would otherwise pay the engine's
   *  seconds-long read on every single open of the same letter. */
  async extractTerms(docId: string, creditNote?: string): Promise<{
    engine: string; cp_items: string[];
    cs_items: { label: string; timeline?: string }[];
    covenants: { name: string; frequency: string; timeline?: string }[];
    terms: Record<string, string | number>;
  }> {
    const cacheKey = `${docId}:${creditNote || ''}`;
    const hit = extractCache.get(cacheKey);
    if (hit) return hit;
    try {
      const out = await orchestrator.post<any>('/v1/cam/extract-terms',
        { doc_id: docId, ...(creditNote ? { credit_note: creditNote } : {}) },
        { timeoutMs: 620_000 });
      extractCache.set(cacheKey, out);
      return out;
    } catch (e) { throw new Error(msg(e, 'read the terms out of the letter')); }
  },

  /** Put a prompt / sanction letter / any workbench input on the lending line's file. */
  async uploadDoc(lendingId: string, file: File, docType: string,
                  section = 'CAM'): Promise<{ id: string }> {
    const form = new FormData();
    form.append('file', file);
    form.append('section', section);
    form.append('title', file.name.replace(/\.[^.]+$/, ''));
    form.append('doc_type', docType);
    form.append('status', 'On File');
    try {
      return await api.post<any>(`/lending/${lendingId}/documents/upload`, form);
    } catch (e) { throw new Error(msg(e, 'upload that document')); }
  },

  /** Everything filed on the LENDING LINE itself (CAMs, prompts, the sanction letter). */
  async lendingDocs(lendingId: string): Promise<EntityDoc[]> {
    const rows = await listAll(`/lending/${lendingId}/documents`, { key: 'documents' });
    return rows
      .filter((r: any) => String(r.status) !== 'Superseded')
      .map((r: any): EntityDoc => ({
        id: r.id, title: r.title || r.original_filename || 'document',
        section: r.section || '', doc_type: r.doc_type || '',
        content_type: r.content_type || '', status: r.status || '',
        uploaded_at: r.uploaded_at || undefined, created_at: r.created_at || undefined,
      }));
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
