// The Register's entity plane — `POST {{baseUrl}}/v1/entities` (api.json, folder
// "02 · Client (Entity)").
//
// In PRISM a lead does not stand alone: it hangs off an ENTITY (the company itself),
// and `POST /v1/leads` carries that entity's id. So "Add lead" is two writes in order —
// entity first, then the lead — which is exactly what the collection does.
//
// Only the fields the Add-lead form actually collects are taken from the form
// (company -> legal/display name, sector, lens, notes). Everything the form has no
// input for — entity_type, register_status, state, location, cin — is sent with the
// collection's fixed values.

import { api, errText, listAll } from '../api/http';
import { normName } from '../utils/format';
import { API_BASE_URL } from '../api/axiosClient';
import type { ClientRow } from '../pages/Clients/client.types';

/** The create body the Register accepts on POST /v1/entities. */
export interface EntityInput {
  code: string;
  legal_name: string;
  display_name: string;
  entity_type: string;
  sector: string;
  lens: string;
  register_status: string;
  state: string;
  location: string;
  cin: string;
  notes: string;
}

export interface Entity extends EntityInput {
  id: string;
}

/** What the form contributes to the entity. */
export interface EntitySeed {
  company: string;
  sector?: string;
  lens?: string;
  notes?: string;
  /** Add client collects these; Add lead does not, so they fall back to the fixed values. */
  state?: string;
  location?: string;
  registerStatus?: string;
}

// Mirrors the collection's pre-request script: runSuffix = String(Date.now()).slice(-6).
// It exists to keep `code` and `cin` unique per run — both are unique keys in the
// Register, so a fixed value would 409 on the second lead.
export function runSuffix(): string {
  return String(Date.now()).slice(-6);
}

/**
 * A Register-safe entity code from the company name: first word, uppercased, letters and
 * digits only, plus the run suffix — "EcoSoch Solar Private Limited" -> "ECOSOCH-123456".
 */
export function entityCode(company: string, suffix: string): string {
  const stem = (company.trim().split(/\s+/)[0] || 'ENTITY').replace(/[^A-Za-z0-9]/g, '').toUpperCase() || 'ENTITY';
  return `${stem.slice(0, 16)}-${suffix}`;
}

export function buildEntityPayload(seed: EntitySeed, suffix = runSuffix()): EntityInput {
  const name = seed.company.trim();
  const state = (seed.state || '').trim();
  return {
    code: entityCode(name, suffix),
    legal_name: name,
    display_name: name,
    // No form input for these — fixed, as in the collection.
    entity_type: 'Company',
    sector: seed.sector || 'Other',
    lens: seed.lens || 'Mitigation',
    register_status: seed.registerStatus || 'Pipeline',
    // state/location are required by the Register, so a form that leaves them blank
    // still sends the collection's values rather than an empty string.
    state: state || 'Karnataka',
    location: (seed.location || '').trim() || state || 'Bengaluru, Karnataka',
    cin: `U40106KA2015PTC${suffix}`,
    notes: seed.notes || '',
  };
}

// --- reads -----------------------------------------------------------------

/**
 * GET /v1/entities filters FAIL-CLOSED: the collection asserts `?bogus_filter=x`
 * comes back 400/422, so an unknown query param fails the whole list. Only `limit`
 * and `cursor` (both proven on /v1/lending, /v1/syndication, /v1/audit) are sent —
 * sorting and search stay client-side through applyQuery, exactly as the mock path does.
 *
 * ENTITY_PAGE is the register's own ceiling (`max_page_size`); asking for more is a 422.
 * It was once set to 1, which is how a register holding two companies rendered a Clients
 * grid with one row in it — so the list now FOLLOWS THE CURSOR rather than trusting a
 * single page to be the whole register.
 */
export const ENTITY_PAGE = 200;

/** Hard stop, so a large register cannot turn one grid render into endless requests. */
export const ENTITY_MAX = 5000;

/** List payloads differ by service; accept a bare array or the usual envelopes. */
function asRows(data: any): any[] {
  if (Array.isArray(data)) return data;
  return data?.items ?? data?.results ?? data?.data ?? data?.entities ?? [];
}

// The Register's own status vocabulary is not ATLAS's LIFE_STAGES. "Pipeline" is the
// one value the collection documents and it means a not-yet-onboarded company, i.e.
// Prospect. Anything else is passed through rather than guessed at.
const LIFECYCLE_OF: Record<string, string> = { Pipeline: 'Prospect' };

/** A Register entity read back as the row shape the Clients grid renders. */
export function toClientRow(e: any): ClientRow {
  const name = e?.legal_name || e?.display_name || '';
  return {
    code: e?.code || '',
    name,
    sector: e?.sector || '',
    lens: e?.lens || '',
    state: e?.state || '',
    // The entity has no `about`; the collection puts the company description in notes.
    about: e?.notes || '',
    toi: '',
    lifecycle: LIFECYCLE_OF[e?.register_status] || e?.register_status || 'Prospect',
    entityId: e?.id,
    entityCode: e?.code,
  };
}

/** Turn a Register failure into a message a dialog can render verbatim. */
export function entityError(e: any, what = 'add the client'): string {
  const status = e?.response?.status;
  const asText = errText(e?.response?.data);
  // The full body always reaches devtools — the alert only has room for the summary.
  if (e?.response) console.warn('[register] POST /entities failed (%s):', status, e.response.data);
  if (status === 401 || status === 403) return asText || `Not permitted to ${what}.`;
  if (status === 409) return asText || 'That company is already on the Register.';
  if (status === 400 || status === 422) return asText || `The Register rejected the ${what} request.`;
  if (status) return asText || `The Register returned ${status}.`;
  return `Cannot reach the Register at ${API_BASE_URL}.`;
}

export const entitiesService = {
  buildEntityPayload,
  toClientRow,
  /**
   * GET {{baseUrl}}/v1/entities — the WHOLE register, mapped to client rows.
   *
   * The endpoint is keyset-paged, so one request returns one page and `next_cursor`
   * points at the rest. Following that cursor is the difference between "the whole
   * register" and "however many rows happened to fit in the first page" — the grid
   * shows every company or it says so; it never quietly drops the tail.
   */
  async list(max = ENTITY_MAX): Promise<ClientRow[]> {
    return (await listAll('/entities', { key: 'entities', max })).map(toClientRow);
  },
  /**
   * The register row for a company NAME, or null — the authoritative duplicate check.
   *
   * `?q=` is the register's own search over legal_name / display_name / code / cin; the
   * shortlist it returns is then matched on the normalised name, so "EcoSoch Solar Pvt
   * Ltd" and "EcoSoch Solar Private Limited" are one company.
   *
   * This has to ask the REGISTER. Asking the browser's client cache said "Already on the
   * register as DEF-312035" about a row that had been dropped when the database was
   * recreated — the cache is never pruned, and it also ships pre-loaded with the
   * prototype's sample companies, so on a fresh deployment it would refuse those names
   * too, for companies the register had never held.
   */
  async findByName(name: string): Promise<ClientRow | null> {
    const wanted = normName(name);
    if (!wanted) return null;
    const rows = await listAll('/entities', { key: 'entities', params: { q: name.trim() }, max: 200 });
    const hit = rows.find((e: any) => normName(e?.legal_name) === wanted
      || normName(e?.display_name) === wanted);
    return hit ? toClientRow(hit) : null;
  },
  /**
   * The register's CURRENT row for a Group Code — the authoritative answer to "which
   * entity id does this company have right now?". The local client cache can hold a
   * stale id (a previous database's, a deleted row's), and every dialog that trusted
   * it hit "Entity … not found" on a perfectly healthy company.
   */
  async byCode(code: string): Promise<ClientRow | null> {
    const wanted = (code || '').trim().toUpperCase();
    if (!wanted) return null;
    const rows = await listAll('/entities', { key: 'entities', params: { q: wanted }, max: 50 });
    const hit = rows.find((e: any) => String(e?.code || '').toUpperCase() === wanted);
    return hit ? toClientRow(hit) : null;
  },
  /** GET {{baseUrl}}/v1/entities/:id/dossier — the entity plus everything hanging off it. */
  async dossier(entityId: string): Promise<any> {
    return api.get<any>(`/entities/${entityId}/dossier`);
  },
  /**
   * Awaited, not fire-and-forget: the lead write that follows needs the returned id, and
   * a rejection (duplicate code/CIN, bad sector) has to reach the user rather than
   * console.warn.
   */
  async create(seed: EntitySeed): Promise<Entity> {
    return api.post<Entity>('/entities', buildEntityPayload(seed));
  },
};
