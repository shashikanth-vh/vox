import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, errText, toCursorParams, asRows, nextCursorOf, totalOf, USE_REAL_API, listAll } from '../api/http';
import { writeAudit } from './auditService';
import type { TableQuery, Paged } from './types';
import type { Lead } from '../pages/Leads/lead.types';
import { inScope, type RowScope } from '../auth/rbac';

const ADAPT_SECT = ['Industrial Water', 'Water Treatment / WASH', 'Climate Data & IoT', 'Agri / Drone'];

/** The create body accepted on POST {{baseUrl}}/v1/leads (api.json, "03 · Lead, ownership & interaction"). */
export interface LeadInput {
  lead_no: string;
  entity_id?: string;
  company: string;
  sector: string;
  lens: string;
  source: string;
  source_name?: string;
  rm: string;
  status: string;
  temperature: string;
  contact?: string;
  designation?: string;
  phone?: string;
  last_interaction_date?: string;
  next_action?: string;
  next_action_date?: string;
  converted_deal_id?: string;
  conv?: string;
  notes?: string;
}

// The date fields are `YYYY-MM-DD` on the wire. ATLAS keeps `next` as free text
// ("Site visit"), so only a value that really is a date is sent as one — anything
// else would 422 the whole lead.
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const isoDate = (v?: string | null) => (v && ISO_DATE.test(v.trim()) ? v.trim() : undefined);

/**
 * A Lead mapped onto the wire contract. ATLAS's short names differ from the API's
 * (temp -> temperature, sourceDetail -> source_name, last -> last_interaction_date).
 *
 * Optional fields are OMITTED when blank rather than sent as "" — the services behind
 * the gateway validate with Pydantic, where an empty string is a value that fails the
 * field's own rules, not an absent one. `converted_deal_id` is never set here: a lead
 * being created has no deal yet, that id only exists after POST /v1/leads/{id}/convert.
 */
export function toLeadPayload(lead: Lead): LeadInput {
  const body: LeadInput = {
    lead_no: lead.id,
    company: (lead.company || '').trim(),
    sector: lead.sector || 'Other',
    lens: lead.lens || 'Mitigation',
    source: lead.source || 'RM',
    rm: lead.rm || '',
    status: lead.status || 'Active',
    temperature: lead.temp || 'Warm',
  };
  const optional: Record<string, string | undefined> = {
    entity_id: lead.entityId,
    source_name: lead.sourceDetail,
    contact: lead.contact,
    designation: lead.designation,
    phone: lead.phone,
    last_interaction_date: isoDate(lead.last),
    next_action: lead.next,
    next_action_date: isoDate(lead.nextDate),
    conv: lead.conv,
    notes: lead.notes,
  };
  for (const [k, v] of Object.entries(optional)) {
    if (v != null && String(v).trim() !== '') (body as any)[k] = String(v).trim();
  }
  return body;
}

/**
 * The inverse of toLeadPayload — an API lead read back as the row the grid renders.
 *
 * The ID column is `lead_no` (LD-001), NOT the API's `id`, which is a UUID and reads as
 * meaningless in the grid. The UUID is kept on `apiId` because that is what /v1/leads/{id}
 * URLs address; nothing renders it.
 */
export function toLeadRow(r: any): Lead {
  return {
    id: r?.lead_no || '',
    apiId: r?.id,
    entityId: r?.entity_id,
    company: r?.company || '',
    sector: r?.sector || '',
    lens: r?.lens || '',
    source: r?.source || '',
    sourceDetail: r?.source_name || '',
    rm: r?.rm || '',
    status: r?.status || '',
    temp: r?.temperature || '',
    contact: r?.contact || '',
    designation: r?.designation || '',
    phone: r?.phone || '',
    last: r?.last_interaction_date || '',
    next: r?.next_action || '',
    nextDate: r?.next_action_date || '',
    conv: r?.conv || '',
    createdAt: r?.created_at || r?.last_interaction_date || '',
    notes: r?.notes || '',
  };
}

/**
 * ATLAS's Lead keys mapped to the wire names PATCH /v1/leads/{id} expects. A patch body
 * keyed `temp` or `last` is silently not the field the API knows, so every editable key
 * the drawer can send is listed here and anything absent is refused rather than guessed.
 */
const PATCH_FIELD: Partial<Record<keyof Lead, string>> = {
  company: 'company', sector: 'sector', lens: 'lens', source: 'source',
  sourceDetail: 'source_name', rm: 'rm', status: 'status', temp: 'temperature',
  contact: 'contact', designation: 'designation', phone: 'phone',
  last: 'last_interaction_date', next: 'next_action', nextDate: 'next_action_date',
  conv: 'conv', notes: 'notes',
};

/** Turn a /v1/leads failure into a message the UI can render verbatim. */
function leadError(e: any, what = 'save the lead'): string {
  const status = e?.response?.status;
  const asText = errText(e?.response?.data);
  if (e?.response) console.warn('[leads] %s failed (%s):', what, status, e.response.data);
  if (status === 401 || status === 403) return asText || `Not permitted to ${what}.`;
  if (status === 404) return asText || 'That lead no longer exists — refresh the list.';
  if (status === 409) return asText || 'That lead already exists.';
  if (status === 400 || status === 422) return asText || `The API rejected the request to ${what}.`;
  if (status) return asText || `The API returned ${status}.`;
  return asText || `Cannot reach the API to ${what}.`;
}

/** The path segment that addresses a lead: the API's UUID, falling back to lead_no. */
const leadRef = (l: Pick<Lead, 'id' | 'apiId'>) => l.apiId || l.id;

/** How many times to step the number up when the register says it is taken. */
const LEAD_NO_RETRIES = 10;

/** LD-001, LD-042, LD-1234 — padded to three digits, wider once it outgrows them. */
export const leadNo = (n: number) => `LD-${String(n).padStart(3, '0')}`;

/** One past the highest lead_no the local store holds. */
function nextLocalSeq(): number {
  return Math.max(0, ...db().leads.map((l: Lead) => +((l.id || '').match(/\d+$/) || [0])[0])) + 1;
}

/**
 * How many leads the register holds, read from the list endpoint's total. A single row is
 * requested because only the count is wanted. Any failure returns 0, which just leaves
 * the local store's own sequence as the floor.
 */
async function leadTotal(): Promise<number> {
  try {
    const data = await api.get<any>('/leads', { limit: 1 });
    return totalOf(data, asRows(data, 'leads').length);
  } catch (e) {
    console.warn('[leads] could not read the register total for the next lead_no:', e);
    return 0;
  }
}

export const leadsService = {
  /** The WHOLE lead book into the store (live mode) — converted leads included, so
   *  the dashboard's RM-origination and funnel figures see every row. */
  async hydrateAll(): Promise<void> {
    if (!USE_REAL_API) return;
    const rows = (await listAll('/leads', { key: 'leads' })).map(toLeadRow);
    const store = db().leads as Lead[];
    store.length = 0;
    store.push(...rows);
  },
  async list(q: TableQuery, scope?: RowScope | null,
             opts?: { includeConverted?: boolean }) {
    return withFallback<Paged<Lead>>(
      async () => {
        // Server-paged: `limit` is the table's page size and Next carries the cursor the
        // previous page returned, so exactly one page comes back and applyQuery must NOT
        // re-slice it. Search goes up as `q`; the total comes from with_total.
        const data = await api.get<any>('/leads', toCursorParams(q));
        // Wire rows are snake_case, so they are mapped to Lead before the grid sees
        // them — otherwise every accessorKey (id, temp, last, next) reads undefined
        // and the ID column shows a raw UUID instead of lead_no.
        // No inScope here: the register already scoped this list (see auth/rbac.ts).
        const all = asRows(data, 'leads').map(toLeadRow);
        // The page promises "Converted leads leave this register automatically" — the
        // register keeps them (the deal is their continuation), so the LIVE list drops
        // them here, exactly as the mock path always did. Management's "Show
        // converted" toggle lifts the filter for source-of-business analysis.
        const rows = opts?.includeConverted ? all
          : all.filter((l) => l.status !== 'Converted');
        const total = Math.max(0, totalOf(data, all.length) - (all.length - rows.length));
        return { rows, total, nextCursor: nextCursorOf(data) };
      },
      async () => {
        await delay();
        const rows = db().leads
          .filter((l: Lead) => opts?.includeConverted || l.status !== 'Converted')
          .filter((l: Lead) => inScope(scope ?? null, l));
        return applyQuery(rows, { ...q, searchFields: ['id', 'company', 'sector', 'rm'] });
      },
    );
  },
  find(id: string): Lead | undefined { return db().leads.find((l: Lead) => l.id === id); },

  /**
   * GET {{baseUrl}}/v1/leads/{obj_id} — the single lead behind the row, read when the
   * drawer opens so it edits the current record rather than a possibly stale grid row.
   * Falls back to the local store (and so to the row already on screen) in mock mode
   * or if the read fails — an unreachable API shouldn't blank out an open drawer.
   */
  async get(l: Pick<Lead, 'id' | 'apiId'>): Promise<Lead | undefined> {
    return withFallback(
      async () => toLeadRow(await api.get<any>('/leads/' + leadRef(l))),
      () => this.find(l.id),
    );
  },

  /**
   * PATCH {{baseUrl}}/v1/leads/{obj_id} — every field the drawer changed, in ONE request
   * sent when the user presses Done. Awaited rather than fire-and-forget so a refusal
   * reaches the user: the API rejects some transitions outright (status → Converted must
   * go through /convert), and a dropped write would leave the drawer showing a value the
   * API never took.
   */
  async patch(lead: Lead, changes: Partial<Lead>, by: string): Promise<{ ok: boolean; error?: string }> {
    const edits: Partial<Lead> = { ...changes };
    // Lens is derived from sector, so a sector edit carries its lens in the same request
    // rather than leaving the two disagreeing on the server.
    if (edits.sector != null && edits.lens == null) {
      edits.lens = ADAPT_SECT.includes(edits.sector) ? 'Adaptation' : 'Mitigation';
    }
    const body: Record<string, any> = {};
    const unknown: string[] = [];
    for (const [k, v] of Object.entries(edits)) {
      const field = PATCH_FIELD[k as keyof Lead];
      if (field) body[field] = v; else unknown.push(k);
    }
    if (unknown.length) return { ok: false, error: `Not an editable field: ${unknown.join(', ')}.` };
    if (!Object.keys(body).length) return { ok: true };
    if (USE_REAL_API) {
      try {
        await api.patch<any>('/leads/' + leadRef(lead), body);
      } catch (e: any) {
        return { ok: false, error: leadError(e, 'update the lead') };
      }
    }
    // Applied only once the API has accepted, so the grid never shows an unsaved value.
    Object.assign(lead, edits);
    const local = this.find(lead.id);
    if (local && local !== lead) Object.assign(local, edits);
    writeAudit(by, 'Lead updated', lead.id, Object.keys(edits).join(', '));
    return { ok: true };
  },
  // Adding a lead is ONE write: POST /v1/leads. No entity is created alongside it — the
  // Register's entity plane is not touched from here, so the lead goes up without an
  // entity_id and the API links it however it chooses. Awaited rather than
  // fire-and-forget: a lead the API rejected must not sit in the grid looking saved, so
  // nothing is inserted locally until it lands. Mock mode skips straight to the insert.
  async create(input: Partial<Lead>, by: string): Promise<{ ok: boolean; lead?: Lead; error?: string }> {
    const lead: Lead = {
      id: '', company: '', sector: 'Other', lens: 'Mitigation',
      source: 'RM', sourceDetail: '', rm: '', status: 'Active', temp: 'Warm', contact: '', phone: '',
      last: today(), next: '', conv: '', createdAt: today(), notes: '', ...input,
    } as Lead;
    // The number to start from: past every lead_no the local store has seen and, on the
    // real API, past the register's total as well.
    let seq = nextLocalSeq();
    if (USE_REAL_API) {
      seq = Math.max(seq, (await leadTotal()) + 1);
      // A total is only a floor — deleted rows mean it can name one that already exists.
      // A 409 is the register saying so, so the next number up is tried rather than
      // handing the user an error they can do nothing about.
      for (let attempt = 0; ; attempt++) {
        lead.id = leadNo(seq + attempt);
        try {
          // The API's returned id is a UUID, kept on apiId so later PATCH/convert calls
          // address the row the backend knows about. `id` stays the LD-nnn lead_no that
          // was sent up and that the grid's ID column renders.
          const saved = await api.post<any>('/leads', toLeadPayload(lead));
          if (saved?.lead_no) lead.id = saved.lead_no;
          if (saved?.id) lead.apiId = saved.id;
          if (saved?.entity_id) lead.entityId = saved.entity_id;
          break;
        } catch (e: any) {
          if (e?.response?.status === 409 && attempt < LEAD_NO_RETRIES) continue;
          return { ok: false, error: leadError(e) };
        }
      }
    } else {
      lead.id = leadNo(seq);
    }
    db().leads.unshift(lead);
    writeAudit(by, 'Lead added', lead.id, lead.company);
    return { ok: true, lead };
  },
  /**
   * DELETE {{baseUrl}}/v1/leads/{obj_id}. Awaited: the row is only dropped from the local
   * store once the API has actually accepted the delete, so a refused one (a lead already
   * converted, or no permission) doesn't vanish from the grid and reappear on refresh.
   */
  async remove(lead: Lead, by: string): Promise<{ ok: boolean; error?: string }> {
    if (USE_REAL_API) {
      try {
        await api.del<any>('/leads/' + leadRef(lead));
      } catch (e: any) {
        return { ok: false, error: leadError(e, 'delete the lead') };
      }
    }
    const i = db().leads.findIndex((l: Lead) => l.id === lead.id);
    if (i > -1) db().leads.splice(i, 1);
    writeAudit(by, 'Lead deleted', lead.id, lead.company);
    return { ok: true };
  },
};
