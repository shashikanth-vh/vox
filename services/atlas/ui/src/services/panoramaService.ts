import { api } from '../api/http';
import { DOCRAG_URL } from '../api/axiosClient';
import { orchestrator } from '../api/orchestratorClient';
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
  stats: { open_leads: number; leads_converted: number;
           deal_count: number | null; live_deals: number;
           deals_in_flight: number; deals_on_hold: number; deals_done: number;
           exposure_ask_cr: number | null; booked_cr: number | null;
           on_hold_cr: number | null; last_touch: string | null;
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
  documents: { id?: string; title: string; section: string | null; doc_type: string | null;
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
  /** The same sentences with full field notes — what "more" reveals. */
  brief_full: string;
  generated_at: string;
}

export interface TracxnSeries {
  points: { year: number; value: number }[];
  unit?: string | null;
  rows?: number;
  note?: string;
}

/** The market feed (Tracxn), CIN-anchored and cached register-side. */
export interface TracxnFinancials {
  cin: string;
  configured: boolean;
  resolved: boolean;
  note?: string;
  legal_entity?: { id: string; name: string | null };
  series?: Record<string, TracxnSeries>;
  board?: { name: string; designation: string | null; since: string | null }[];
  shareholders?: { name: string; pct: number }[];
  fetched_now?: number;
  fetched_at?: string;
}

export interface DocAskResult {
  answer: string | null;
  citations: { doc: string; where: string }[];
  noDocuments: boolean;
  /** 'generative' = a written summary; 'extractive' = cited passages. */
  mode: 'generative' | 'extractive' | null;
}

export interface IndexResult {
  company: string;
  total_on_register: number;
  indexed: { file: string; doc_id: string | null; duplicate: boolean }[];
  skipped: { file: string; reason: string }[];
  /** Files DocRAG now holds for this company (uploaded now or earlier). */
  ready?: number;
  /** Uploaded on THIS pass — these are still being read in the background. */
  fresh?: number;
  note: string | null;
}

export type RiskBand = 'RED' | 'AMBER' | 'GREEN';

/** The orchestrator's grade: the ATLAS client rubric, re-applied server-side. */
export interface RiskGrade {
  rating: RiskBand; provisional: boolean; label: string; score: number;
  confidence: 'High' | 'Medium' | 'Low'; verdict: string;
  /** 60% × data coverage + 40% × agreement between the independent readings. */
  confidence_pct?: number;
  confidence_detail?: { data_coverage: number; agreement: number; spread: number;
                        readings: number; model_said?: string };
  pillars: { key: string; name: string; weight: number; score: number;
             weighted: number; available: boolean; evidence: string }[];
  hard_triggers: { trigger: string; status: 'Hit' | 'Clear' | 'Unknown';
                   evidence: string }[];
  risks: string[]; mitigants: string[]; data_gaps: string[];
  move_up: string; move_down: string;
  action: string; conditions: string[];
  /** Where the rubric overrode the model — shown, never hidden. */
  adjustments: string[];
  /** Every independent reading; the grade shown is their median. */
  runs?: { label: string; score: number }[];
  /** Within a few points of a band line — the colour is not firm. */
  borderline?: boolean;
  company: string; entity_id?: string | null;
  /** A regrade found nothing new to read: this is the previous grade, unchanged. */
  unchanged?: boolean;
  engine: string; prompt: string; prompt_version: string;
  generated_at: string; generated_by: string; cached: boolean;
  inputs: { register_sections_hidden: string[]; lending_lines: number;
            platform_deals: number; asset_monetisation: number; interactions: number;
            register_documents: number; document_passages: number;
            documents_cited: string[]; document_note: string | null;
            news_items: number; news_note?: string | null; documents_read?: number;
            /** Every Data Register file in the graded sections, and what became of it. */
            documents?: { name: string; section: string | null; used: boolean;
                          engines: string[]; reason?: string; cached?: boolean;
                          note?: string }[] };
}

export interface RiskNewsIn { headline: string; source?: string; when?: string;
  severity?: string }

function docragHeaders(): Record<string, string> {
  return { Accept: 'application/json', 'Content-Type': 'application/json',
    ...authHeaders() };
}

export const panoramaService = {
  get(q: { entityId?: string | null; company?: string }): Promise<Panorama> {
    const params: Record<string, string> = {};
    if (q.entityId) params.entity_id = q.entityId;
    else if (q.company) params.company = q.company;
    // A missing gateway answers with the SPA's index.html (200, text) — refuse
    // anything that is not a panorama, so the dialog names the fault instead of
    // crashing the whole screen on `p.stats`.
    return api.get<Panorama>('/panorama', params).then((r) => {
      if (!r || typeof r !== 'object' || !r.stats || !r.anchor) {
        throw new Error('the Register did not return a company panorama '
          + '(is the gateway reachable? — Company 360 has no offline data)');
      }
      return r;
    });
  },

  /** The last grade for this company, or null when none was computed yet. */
  async riskGrade(entityId: string | null, company: string): Promise<RiskGrade | null> {
    try {
      return await orchestrator.get<RiskGrade>('/v1/panorama/risk-grade',
        { company, ...(entityId ? { entity_id: entityId } : {}) });
    } catch (e: any) {
      if (e?.response?.status === 404) return null;
      throw e;
    }
  },

  /** Grade (or regrade) with the ATLAS client rubric on the CAM's engine. One model
   *  call over the whole footprint — allow minutes, not seconds. */
  gradeRisk(entityId: string | null, company: string, news: RiskNewsIn[],
            refresh = false): Promise<RiskGrade> {
    return orchestrator.post<RiskGrade>('/v1/panorama/risk-grade',
      { company, entity_id: entityId, news: news.slice(0, 60), refresh },
      // A first grade reads the company's financials (OCR for scans) — allow minutes.
      { timeoutMs: 600_000 });
  },

  /** The grade on screen as a Word report, saved to the browser's downloads. The
   *  grade travels with the request, so the report is exactly what is shown — even
   *  after the server's saved copy has expired (a restart, or 24 h). */
  async downloadRiskReport(grade: RiskGrade): Promise<void> {
    const blob = await orchestrator.postBlob('/v1/panorama/risk-grade/report', { grade },
      { timeoutMs: 60_000 });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `Risk grade - ${grade.company} - ${String(grade.generated_at).slice(0, 10)}.docx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30_000);
  },

  /** The FINANCIALS card's live feed. 409 = not configured, or no CIN on
   *  record — both carry a human remedy in the error detail, and the card
   *  falls back to prospect figures rather than going blank. */
  financials(q: { entityId?: string | null; cin?: string | null;
                  refresh?: boolean }): Promise<TracxnFinancials> {
    const params: Record<string, string> = {};
    if (q.entityId) params.entity_id = q.entityId;
    if (q.cin) params.cin = q.cin;
    if (q.refresh) params.refresh = 'true';
    return api.get<TracxnFinancials>('/panorama/financials', params);
  },

  /** Bridge the seam the ask-box sits on: read the company's Data Register
   *  files (as the caller — RBAC and scope hold) and index the supported ones
   *  into DocRAG under "<Company> — <file>" names the ask-box matches. */
  indexDocuments(entityId: string, company: string): Promise<IndexResult> {
    return orchestrator.post<IndexResult>('/v1/panorama/index-documents',
      { entity_id: entityId, company });
  },

  /** Ask this company's indexed documents. Scoped by DocRAG doc_ids whose names
   *  carry the company; empty match = an honest "nothing indexed yet". */
  async askDocuments(company: string, question: string): Promise<DocAskResult> {
    const listRes = await fetch(`${DOCRAG_URL}/v1/documents`,
      { headers: docragHeaders() });
    if (!listRes.ok) throw new Error(`DocRAG unavailable (${listRes.status})`);
    const listing = await listRes.json();
    const items: any[] = listing.items || [];
    const nameOf = (d: any) => String(d.name || d.filename || '').toLowerCase();
    // The bridge names its uploads "<Company> — <file>", so the exact prefix is
    // the scope. Only when nothing carries it do we fall back to word matching
    // (hand-uploaded files) — and never on words generic enough to hit another
    // company ("Power", "Systems", "Private").
    const prefix = `${company.toLowerCase()} — `;
    let matched = items.filter((d) => nameOf(d).startsWith(prefix));
    if (!matched.length) {
      const generic = new Set(['private', 'limited', 'systems', 'power', 'india',
        'energy', 'solar', 'technologies', 'company', 'industries']);
      const needle = company.toLowerCase().split(/\s+/)
        .filter((w) => w.length > 3 && !generic.has(w));
      if (needle.length) {
        matched = items.filter((d) => needle.some((w) => nameOf(d).includes(w)));
      }
    }
    const ids: string[] = matched.map((d) => String(d.id)).slice(0, 50);
    if (!ids.length) {
      return { answer: null, citations: [], noDocuments: true, mode: null };
    }
    // A WRITTEN summary when the answer model is configured; cited passages
    // when it is not (409) or it stumbles (502) — never a dead end.
    const run = (mode: string) => fetch(`${DOCRAG_URL}/v1/query`, {
      method: 'POST', headers: docragHeaders(),
      body: JSON.stringify({ query: question, mode, top_k: 5, doc_ids: ids }),
    });
    let qRes = await run('generative');
    if (qRes.status === 409 || qRes.status === 502) qRes = await run('extractive');
    if (!qRes.ok) throw new Error(`DocRAG query failed (${qRes.status})`);
    const out = await qRes.json();
    const citations = (out.citations || []).slice(0, 3).map((c: any) => ({
      doc: String(c.doc || 'document'),
      where: [c.section_path,
        Array.isArray(c.pages) && c.pages.length ? `p. ${c.pages.join(', ')}` : null]
        .filter(Boolean).join(' · '),
    }));
    return { answer: typeof out.answer === 'string' ? out.answer : null,
      citations, noDocuments: false,
      mode: out.mode === 'generative' ? 'generative' : 'extractive' };
  },
};
