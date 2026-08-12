import { db } from '../api/atlasStore';
import { writeAudit } from './auditService';
import { api, withFallback, errText, USE_REAL_API } from '../api/http';

export interface Interaction {
  interactionId: string; refId: string; refType: string;
  occurredAt: string; loggedAt: string; person: string; interactionType: string;
  direction?: string | null; lenderName?: string | null;
  notes: string; nextAction?: string | null; nextActionDate?: string | null;
  // The depth behind the summary line — everything the register stores that the
  // timeline's collapsed row does not show. All optional: manual quick-logs carry
  // none of it, VOX-logged rows carry most of it.
  fullNotes?: string | null;
  outcome?: string | null;
  /** Structured credit intel. The writers disagree on shape (VocX a dict, its reports
   *  a bullet list), so this stays `any` and the renderer treats each shape. */
  keyIntel?: any;
  nextSteps?: any[] | null;
  transcript?: string | null;
  attendees?: any[] | null;
  location?: string | null;
  nextMeetingDate?: string | null;
  /** Manual / VOX / Email / System — badges the row with where it came from. */
  source?: string | null;
}

const newId = () => 'INT-' + Date.now().toString(36).toUpperCase() + '-' + Math.random().toString(36).slice(2, 6).toUpperCase();

/** Enough of a Lead to address /v1/leads/{obj_id}/interactions and name the contact. */
export interface LeadRef { id: string; apiId?: string; contact?: string }

/** URLs address the API's UUID; `id` is the human lead_no the grid shows. */
const leadPath = (r: LeadRef) => r.apiId || r.id;

/** A `YYYY-MM-DD` from the form widened to the timestamp the API stores. */
const toStamp = (d?: string | null) => {
  const day = (d || '').trim() || new Date().toISOString().slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? `${day}T00:00:00Z` : day;
};

/** The hand-typed dialog's record as the register's wire shape. The API caps `summary`
 *  at 300 chars, so the FULL text always rides in `notes` (the server keeps both) and
 *  the summary is the cap — a long note must never bounce with a 422 or lose its tail.
 *  Typed next actions go on the wire too — they used to survive only in the local
 *  store, which a register-backed timeline rightly never showed. */
const manualWire = (rec: Partial<Interaction>, by: string) => {
  const text = (rec.notes || '').trim();
  return {
    interaction_type: rec.interactionType || '',
    occurred_at: toStamp(rec.occurredAt),
    summary: text.slice(0, 300),
    ...(text.length > 300 ? { notes: text } : {}),
    performed_by: rec.person || by,
    ...(rec.nextAction?.trim() ? { next_action: rec.nextAction.trim() } : {}),
    ...(rec.nextActionDate ? { next_action_date: rec.nextActionDate } : {}),
    source: 'Manual',
  };
};

/** An API interaction read back as the ledger row the drawer renders. */
function fromWire(r: any, refId: string, refType = 'Lead'): Interaction {
  const at = String(r?.occurred_at || '');
  return {
    interactionId: r?.id || newId(),
    refId, refType,
    occurredAt: at.slice(0, 10),
    loggedAt: r?.created_at || at,
    person: r?.performed_by || '',
    interactionType: r?.interaction_type || '',
    direction: r?.direction || null,
    lenderName: r?.contact_name || r?.lender_name || null,
    notes: r?.summary || '',
    nextAction: r?.next_action || null, nextActionDate: r?.next_action_date || null,
    // The depth the expanded row shows. fullNotes only when it ADDS to the summary —
    // the register mirrors short manual notes into both columns.
    fullNotes: r?.notes && r.notes !== r.summary ? r.notes : null,
    outcome: r?.outcome || null,
    keyIntel: r?.key_intel ?? null,
    nextSteps: Array.isArray(r?.next_steps) && r.next_steps.length ? r.next_steps : null,
    transcript: r?.transcript || null,
    attendees: Array.isArray(r?.attendees) && r.attendees.length ? r.attendees : null,
    location: r?.location || null,
    nextMeetingDate: r?.next_meeting_date || null,
    source: r?.source || null,
  };
}

export const interactionService = {
  // Live mode: the vocabulary comes from /v1/ref like every other dropdown; the
  // bundled list only backs mock/offline development.
  types(): string[] { return db().ref?.['Interaction Type'] || db().interactionTypes || []; },
  for(refId: string): Interaction[] {
    return (db().interactions || []).filter((i: any) => i.refId === refId)
      .sort((a: any, b: any) => ((b.occurredAt || '') + (b.loggedAt || '')).localeCompare((a.occurredAt || '') + (a.loggedAt || '')));
  },
  log(rec: Partial<Interaction>, by: string): Interaction {
    if (!db().interactions) db().interactions = [];
    const full: Interaction = {
      interactionId: newId(), refId: rec.refId || '', refType: rec.refType || 'General',
      occurredAt: rec.occurredAt || new Date().toISOString().slice(0, 10), loggedAt: new Date().toISOString(),
      person: rec.person || by, interactionType: rec.interactionType || '',
      direction: rec.direction || null, lenderName: rec.lenderName || null,
      notes: rec.notes || '', nextAction: rec.nextAction || null, nextActionDate: rec.nextActionDate || null,
    };
    db().interactions.push(full);
    writeAudit(by, 'Interaction logged', full.refId, full.interactionType + (full.notes ? ' — ' + full.notes.slice(0, 60) : ''));
    return full;
  },

  /**
   * The COMPANY's whole story, live: interactions logged on the entity itself PLUS the
   * lead-phase discussion from every lead that belongs to it — converted ones included.
   * The deal team inherits the conversation that won the mandate, not just what was
   * logged after conversion. Lead-phase rows carry refType 'Lead' so the drawer can
   * badge them. Falls back to the local store (mock mode / unreachable register).
   */
  async forCompany(entityId: string | null | undefined, code: string): Promise<Interaction[]> {
    if (!USE_REAL_API || !entityId) return this.for(code);
    try {
      const rowsOf = (d: any): any[] =>
        (Array.isArray(d) ? d : (d?.items ?? d?.results ?? d?.interactions ?? d?.leads ?? []));
      const [entData, leadData] = await Promise.all([
        api.get<any>(`/entities/${entityId}/interactions`).catch(() => []),
        api.get<any>('/leads', { entity_id: entityId, limit: 20 }).catch(() => null),
      ]);
      // THE ENTITY TIMELINE ALREADY CONTAINS THE LEAD ROWS. The register's Entity
      // timeline is a ROLL-UP — "every interaction rolled up to the entity" — so a note
      // logged against a lead of this company comes back from BOTH calls. Pushing both
      // showed one capture twice: the same text, the same minute, once plain and once
      // badged LEAD PHASE, with the header counting two. Key by the interaction's own
      // id and let the per-lead pass OVERWRITE the roll-up copy: the row is identical
      // either way, but the lead-sourced one knows which lead it came from, which is
      // what earns the badge. A row with no id (mock/offline shapes) falls back to a
      // composite key rather than collapsing into its neighbours.
      const byId = new Map<string, Interaction>();
      const keyOf = (i: Interaction, r: any) =>
        String(r?.id || `${i.occurredAt}|${i.person}|${i.interactionType}|${i.notes}`);
      rowsOf(entData).forEach((r: any) => {
        const i = fromWire(r, code, 'Entity');
        byId.set(keyOf(i, r), i);
      });
      const leadRows = rowsOf(leadData);
      const perLead = await Promise.all(leadRows.map((l: any) =>
        api.get<any>(`/leads/${l.id}/interactions`).then(rowsOf).catch(() => [])));
      perLead.forEach((rows, i) => rows.forEach((r: any) => {
        const it = fromWire(r, leadRows[i].lead_no || code, 'Lead');
        byId.set(keyOf(it, r), it);
      }));
      const out: Interaction[] = [...byId.values()];
      out.sort((a, b) =>
        ((b.occurredAt || '') + (b.loggedAt || '')).localeCompare((a.occurredAt || '') + (a.loggedAt || '')));
      return out;
    } catch { return this.for(code); }
  },

  /**
   * GET {{baseUrl}}/v1/leads/{obj_id}/interactions — the ledger for one lead. Falls back
   * to the local store, which is also what mock mode uses.
   */
  async forLead(ref: LeadRef): Promise<Interaction[]> {
    return withFallback(
      async () => {
        const data = await api.get<any>(`/leads/${leadPath(ref)}/interactions`);
        const rows = Array.isArray(data) ? data : (data?.items ?? data?.results ?? data?.data ?? data?.interactions ?? []);
        return rows.map((r: any) => fromWire(r, ref.id));
      },
      () => this.for(ref.id),
    );
  },

  /**
   * POST {{baseUrl}}/v1/entities/{obj_id}/interactions — the company drawer's Log
   * interaction. This used to write ONLY the local store, so the entry showed in the
   * local audit but never reached the register — and the register-backed timeline
   * (forCompany) rightly refused to show it. Same append-only honesty as the lead lane.
   */
  async logForEntity(entityId: string, rec: Partial<Interaction>,
                     by: string): Promise<{ ok: boolean; error?: string }> {
    if (USE_REAL_API && entityId) {
      try {
        await api.post<any>(`/entities/${entityId}/interactions`, {
          ...manualWire(rec, by),
          contact_name: rec.lenderName || '',
        });
      } catch (e: any) {
        const detail = errText(e?.response?.data);
        if (e?.response) console.warn('[interactions] POST /entities/%s/interactions failed (%s):', entityId, e.response.status, e.response.data);
        return { ok: false, error: detail || 'Could not log the interaction.' };
      }
    }
    this.log({ ...rec, refType: rec.refType || 'General' }, by);
    return { ok: true };
  },

  /**
   * POST {{baseUrl}}/v1/leads/{obj_id}/interactions. Awaited so a rejected entry doesn't
   * sit in the ledger looking recorded — the records are append-only, so a phantom one
   * can't be tidied up afterwards.
   */
  async logForLead(ref: LeadRef, rec: Partial<Interaction>, by: string): Promise<{ ok: boolean; error?: string }> {
    if (USE_REAL_API) {
      try {
        await api.post<any>(`/leads/${leadPath(ref)}/interactions`, {
          ...manualWire(rec, by),
          contact_name: rec.lenderName || ref.contact || '',
        });
      } catch (e: any) {
        const detail = errText(e?.response?.data);
        if (e?.response) console.warn('[leads] POST /leads/%s/interactions failed (%s):', leadPath(ref), e.response.status, e.response.data);
        return { ok: false, error: detail || 'Could not log the interaction.' };
      }
    }
    this.log({ ...rec, refId: ref.id, refType: 'Lead' }, by);
    return { ok: true };
  },
};
