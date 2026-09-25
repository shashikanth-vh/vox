import axiosClient from '../api/axiosClient';
import { api, listAll } from '../api/http';

// The prospect universe (Masters → Prospects) — a LIVE-register feature: the
// curated market lists exist only on the server, so there is no mock-store twin.
// Reads fetch the whole universe (a few thousand rows at most) and the grid pages
// locally, exactly as the clients grid does; the chips' counts are computed from
// the same fetched rows so the chips and the grid can never disagree.

export interface ProspectRow {
  id: string;
  prospect_no: string | null;
  name: string;
  domain: string | null;
  cin: string | null;
  overview: string | null;
  verticals: string[] | null;
  sub_sectors: string[] | null;
  emails: string[] | null;
  phones: string[] | null;
  founded_year: number | null;
  state: string | null;
  city: string | null;
  country: string | null;
  revenue_cr: number | null;
  net_profit_cr: number | null;
  ebitda_cr: number | null;
  total_funding_cr: number | null;
  latest_funding_cr: number | null;
  latest_valuation_cr: number | null;
  latest_funded_on: string | null;
  status: 'uncontacted' | 'contacted' | 'interested' | 'lead_created' | 'not_relevant';
  remarks: string | null;
  lead_ids: string[] | null;
  entity_id: string | null;
}

export interface ProspectImportPreview {
  mode: 'preview' | 'apply';
  batch: string;
  files: { file: string; vertical: string | null; sheet: string | null; rows: number }[];
  counts: { new: number; merged: number; in_file_duplicates: number;
            skipped: number; conflicts: number };
  new_sample: { name: string; cin: string | null; verticals: string[] }[];
  merge_sample: { prospect_no: string | null; name: string; added_fields: string[];
                  conflicts: any[] }[];
  skipped: { file: string; row: number | null; reason: string }[];
  conflicts: { prospect_no: string | null; name: string; field: string;
               existing: string; incoming: string }[];
  created?: number;
  merged?: number;
}

export const prospectsService = {
  async list(): Promise<ProspectRow[]> {
    return await listAll('/prospects', { key: 'prospects', max: 10000 }) as ProspectRow[];
  },

  /** The desk verbs (workProspect): status + remarks. */
  update(id: string, patch: { status?: string; remarks?: string }): Promise<ProspectRow> {
    return api.patch<ProspectRow>(`/prospects/${id}`, patch);
  },

  /** Curated master-data edit (manageProspects on the server). */
  updateMaster(id: string, patch: Record<string, unknown>): Promise<ProspectRow> {
    return api.patch<ProspectRow>(`/prospects/${id}`, patch);
  },

  remove(id: string): Promise<unknown> {
    return api.del(`/prospects/${id}`);
  },

  createLead(id: string, body: { rm?: string; sector?: string; notes?: string }):
      Promise<{ lead_id: string; lead_no: string; entity_id: string | null;
                company_outcome: string; lead_count: number }> {
    return api.post(`/prospects/${id}/create-lead`, body);
  },

  /** Import, both steps: preview writes nothing; apply executes and audits. */
  async importLists(files: File[], mode: 'preview' | 'apply',
                    verticalOverrides?: Record<string, string>):
      Promise<ProspectImportPreview> {
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    const params: Record<string, string> = { mode };
    if (verticalOverrides && Object.keys(verticalOverrides).length) {
      params.verticals = JSON.stringify(verticalOverrides);
    }
    const res = await axiosClient.post('/prospects/import', form, { params });
    return res.data as ProspectImportPreview;
  },

  /** Download the universe in the same workbook shape the import reads. */
  async exportXlsx(): Promise<void> {
    const res = await axiosClient.get('/prospects/export-xlsx', { responseType: 'blob' });
    const dispo = String(res.headers?.['content-disposition'] || '');
    const name = /filename="?([^";]+)"?/.exec(dispo)?.[1] || 'prism-prospects.xlsx';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(res.data as Blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  },
};
