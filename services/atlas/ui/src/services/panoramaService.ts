import { api } from '../api/http';
import { DOCRAG_URL } from '../api/axiosClient';
import { authHeaders } from '../auth/session';

/**
 * Company 360 — the register's panorama endpoint plus the documents ask-box.
 *
 * The panorama is ONE read: the register aggregates every module it owns for one
 * company, RBAC'd per section server-side (a section the role cannot see arrives
 * named in `restricted`, not silently missing). News is NOT here — the dialog
 * asks PULSE directly, the same endpoint the radar uses.
 *
 * The ask-box goes to DocRAG through the edge (`/docrag`), restricted to the
 * documents whose names carry this company — DocRAG's `doc_ids` filter, so an
 * answer can only come from this company's files, cited. No documents match →
 * the caller renders "nothing indexed yet", never an unscoped answer.
 */

export interface PanoramaLead {
  lead_no: string | null; status: string | null; temperature: string | null;
  rm: string | null; sector: string | null; source: string | null;
  last_interaction_date: string | null; next_action: string | null;
  next_action_date: string | null; notes: string | null; converted: boolean;
}

export interface PanoramaLine {
  tracker_no: string | null; stage?: string | null; status?: string | null;
  amount_cr: number | null; pending_with?: string | null; rm: string | null;
  analyst?: string | null; stage_updated_at?: string | null;
  sanction_date?: string | null; disbursed_amount?: number | null;
  remarks?: string | null; ladder: string[];
  lenders?: { name: string; status: string | null; amount_cr: number | null;
              last_chase: string | null; last_reply: string | null }[];
  indicative_value_cr?: number | null; deal_type?: string | null;
  investor?: string | null;
}

export interface Panorama {
  anchor: { entity_id: string | null; matched_by: string; name: string;
            cin: string | null; sector: string | null; sub_sector: string | null;
            state: string | null; domain: string | null; about: string | null };
  restricted: string[];
  stats: { open_leads: number; leads_converted: number; live_deals: number;
           exposure_ask_cr: number | null; last_touch: string | null;
           documents: number };
  leads: PanoramaLead[];
  deals: { deal_no: string | null; code: string | null; stage: string | null;
           product_type: string | null; rm: string | null }[];
  lending: PanoramaLine[];
  syndication: PanoramaLine[];
  asset_monetisation: PanoramaLine[];
  interactions: { occurred_at: string | null; type: string; summary: string | null;
                  notes: string | null; by: string | null; contact: string | null;
                  lender: string | null }[];
  documents: { title: string; section: string | null; doc_type: string | null;
               filename: string | null; uploaded_at: string | null;
               uploaded_by: string | null }[];
  prospect: { prospect_no: string | null; status: string; verticals: string[] | null;
              sub_sectors: string[] | null; remarks: string | null;
              revenue_cr: number | null; net_profit_cr: number | null;
              ebitda_cr: number | null; founded_year: number | null;
              emails: string[] | null; phones: string[] | null;
              lead_count: number } | null;
  contacts: { name: string; designation: string | null; phone: string | null;
              source: string }[];
  brief: string;
  generated_at: string;
}

export interface DocAskResult {
  answer: string | null;
  citations: { doc: string; where: string }[];
  noDocuments: boolean;
}

function docragHeaders(): Record<string, string> {
  return { Accept: 'application/json', 'Content-Type': 'application/json',
    ...authHeaders() };
}

export const panoramaService = {
  get(q: { entityId?: string | null; company?: string }): Promise<Panorama> {
    const params: Record<string, string> = {};
    if (q.entityId) params.entity_id = q.entityId;
    else if (q.company) params.company = q.company;
    return api.get<Panorama>('/panorama', params);
  },

  /** Ask this company's indexed documents. Scoped by DocRAG doc_ids whose names
   *  carry the company; empty match = an honest "nothing indexed yet". */
  async askDocuments(company: string, question: string): Promise<DocAskResult> {
    const listRes = await fetch(`${DOCRAG_URL}/v1/documents`,
      { headers: docragHeaders() });
    if (!listRes.ok) throw new Error(`DocRAG unavailable (${listRes.status})`);
    const listing = await listRes.json();
    const needle = company.toLowerCase().split(/\s+/).filter((w) => w.length > 3);
    const ids: string[] = (listing.items || [])
      .filter((d: any) => {
        const n = String(d.name || d.filename || '').toLowerCase();
        return needle.some((w) => n.includes(w));
      })
      .map((d: any) => String(d.id))
      .slice(0, 50);
    if (!ids.length) return { answer: null, citations: [], noDocuments: true };
    const qRes = await fetch(`${DOCRAG_URL}/v1/query`, {
      method: 'POST', headers: docragHeaders(),
      body: JSON.stringify({ query: question, mode: 'extractive', top_k: 5,
        doc_ids: ids }),
    });
    if (!qRes.ok) throw new Error(`DocRAG query failed (${qRes.status})`);
    const out = await qRes.json();
    const citations = (out.citations || []).slice(0, 3).map((c: any) => ({
      doc: String(c.doc || 'document'),
      where: [c.section_path,
        Array.isArray(c.pages) && c.pages.length ? `p. ${c.pages.join(', ')}` : null]
        .filter(Boolean).join(' · '),
    }));
    return { answer: typeof out.answer === 'string' ? out.answer : null,
      citations, noDocuments: false };
  },
};
