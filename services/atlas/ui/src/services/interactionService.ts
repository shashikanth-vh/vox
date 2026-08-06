import { db } from '../api/atlasStore';
import { writeAudit } from './auditService';
import { api, withFallback, errText, USE_REAL_API } from '../api/http';

export interface Interaction {
  interactionId: string; refId: string; refType: string;
  occurredAt: string; loggedAt: string; person: string; interactionType: string;
  direction?: string | null; lenderName?: string | null;
  notes: string; nextAction?: string | null; nextActionDate?: string | null;
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
    direction: null,
    lenderName: r?.contact_name || null,
    notes: r?.summary || '',
    nextAction: null, nextActionDate: null,
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
      const out: Interaction[] = rowsOf(entData).map((r: any) => fromWire(r, code, 'Entity'));
      const leadRows = rowsOf(leadData);
      const perLead = await Promise.all(leadRows.map((l: any) =>
        api.get<any>(`/leads/${l.id}/interactions`).then(rowsOf).catch(() => [])));
      perLead.forEach((rows, i) => out.push(
        ...rows.map((r: any) => fromWire(r, leadRows[i].lead_no || code, 'Lead'))));
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
   * POST {{baseUrl}}/v1/leads/{obj_id}/interactions. Awaited so a rejected entry doesn't
   * sit in the ledger looking recorded — the records are append-only, so a phantom one
   * can't be tidied up afterwards.
   */
  async logForLead(ref: LeadRef, rec: Partial<Interaction>, by: string): Promise<{ ok: boolean; error?: string }> {
    if (USE_REAL_API) {
      try {
        await api.post<any>(`/leads/${leadPath(ref)}/interactions`, {
          interaction_type: rec.interactionType || '',
          // The API takes a timestamp; the form collects a date, so midnight UTC stands
          // in for the time of day, which ATLAS does not ask for.
          occurred_at: toStamp(rec.occurredAt),
          summary: rec.notes || '',
          contact_name: rec.lenderName || ref.contact || '',
          performed_by: rec.person || by,
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
