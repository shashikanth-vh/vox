import { api, apiErr } from '../api/http';

/**
 * Import reconciliation — the Admin queue for rows a governed import RETAINED.
 *
 * A ledger import may land a row whose stage demands data the sheet did not carry: a
 * facility the book says is Disbursed, with no proposed drawdown amount or date. Those
 * rows are too real to drop and too incomplete to work, so they import FLAGGED, and
 * every operational read excludes them until the flag clears.
 *
 * That exclusion is the reason this screen exists. Without it the desk sees "7
 * reconciliation item(s)" once, in the import dialog, and then a lending list quietly
 * missing seven facilities — which reads as a broken workflow rather than a queue
 * nobody has worked.
 */

export interface ReconItem {
  id: string;
  version: number;
  subject_type: string;
  subject_id: string | null;
  sheet: string | null;
  company: string | null;
  stage_field: string | null;
  stage_value: string | null;
  missing_fields: string[];
  /** What the SHEET said — preserved on the item, so the fix starts from the source. */
  original_values: Record<string, any>;
  status: 'Required' | 'Resolved' | 'Waived' | string;
  owner: string | null;
  resolution_note: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string | null;
  import_batch_id: string | null;
}

type Result<T> = Promise<{ ok: boolean; error?: string; data?: T }>;

export const reconciliationService = {
  async list(status?: string): Result<ReconItem[]> {
    try {
      const d = await api.get<any>('/reconciliation', status ? { status } : undefined);
      return { ok: true, data: (d?.items ?? []) as ReconItem[] };
    } catch (e) { return { ok: false, error: apiErr(e, 'read the reconciliation queue') }; }
  },

  /**
   * Close an item. RESOLVED means the record has actually been corrected — the register
   * re-reads it, checks every flagged field is now present and re-runs the full policy
   * engine before it will accept the closure. There is deliberately no field-correction
   * here: the fix goes through the record's own update API so it gets the update schema,
   * the policy engine, the field locks and the history like any other edit.
   *
   * WAIVED keeps an incomplete record in the business of record on purpose, and is
   * Management-only.
   */
  async resolve(id: string, status: 'Resolved' | 'Waived', note: string,
                ticket?: string): Result<ReconItem> {
    try {
      const d = await api.post<any>(`/reconciliation/${id}/resolve`,
        { status, note, ...(ticket ? { ticket } : {}) });
      return { ok: true, data: d as ReconItem };
    } catch (e) {
      return { ok: false, error: apiErr(e, status === 'Waived' ? 'waive that item' : 'resolve that item') };
    }
  },

  async assign(id: string, owner: string): Result<ReconItem> {
    try {
      return { ok: true, data: await api.post<any>(`/reconciliation/${id}/assign`, { owner }) };
    } catch (e) { return { ok: false, error: apiErr(e, 'assign that item') }; }
  },
};
