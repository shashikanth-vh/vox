import { api, asRows, LIST_MAX_LIMIT, USE_REAL_API } from '../api/http';

/**
 * The register keeps grids NORMALISED: a deal row carries entity_id (no company name),
 * a tracker row (lending / asset-mon / syndication) carries deal_id (no deal number and
 * no company). The prototype's grids render both — so the platform build joins them
 * client-side from two small lookup maps (entity id → display name, deal id → number +
 * entity), fetched once and cached briefly. Everything here fails SOFT: a resolver
 * outage leaves the joined columns blank, it never fails the grid that asked.
 */

type DealRef = { code: string; entityId: string | null };

let entityNames = new Map<string, string>();
let dealRefs = new Map<string, DealRef>();
let loadedAt = 0;
let inflight: Promise<void> | null = null;
const TTL_MS = 60_000;

async function load(): Promise<void> {
  const [ents, deals] = await Promise.all([
    api.get<any>('/entities', { limit: LIST_MAX_LIMIT }),
    api.get<any>('/deals', { limit: LIST_MAX_LIMIT }),
  ]);
  const e = new Map<string, string>();
  asRows(ents, 'entities').forEach((r: any) => {
    if (r?.id) e.set(String(r.id), r.display_name || r.legal_name || '');
  });
  const d = new Map<string, DealRef>();
  asRows(deals, 'deals').forEach((r: any) => {
    if (r?.id) d.set(String(r.id), {
      code: r.deal_no || r.code || '',
      entityId: r.entity_id ? String(r.entity_id) : null,
    });
  });
  entityNames = e;
  dealRefs = d;
  loadedAt = Date.now();
}

async function ensure(): Promise<void> {
  if (!USE_REAL_API) return;
  if (Date.now() - loadedAt < TTL_MS) return;
  if (!inflight) inflight = load().finally(() => { inflight = null; });
  try { await inflight; } catch (e) { console.warn('[api] name lookup failed:', e); }
}

/** Deals grid: fill the COMPANY column from the row's entity_id. Mutates in place. */
export async function fillCompanyFromEntity<T extends { _name?: string; entityId?: string }>(rows: T[]): Promise<T[]> {
  if (rows.some((r) => !r._name && r.entityId)) {
    await ensure();
    rows.forEach((r) => {
      if (!r._name && r.entityId) r._name = entityNames.get(String(r.entityId)) || '';
    });
  }
  return rows;
}

/** Tracker grids: fill GROUP CODE + COMPANY from the row's deal_id. Mutates in place. */
export async function fillFromDeal<T extends { _name?: string; code?: string; dealId?: string }>(rows: T[]): Promise<T[]> {
  if (rows.some((r) => r.dealId && (!r._name || !r.code))) {
    await ensure();
    rows.forEach((r) => {
      const ref = r.dealId ? dealRefs.get(String(r.dealId)) : undefined;
      if (!ref) return;
      if (!r.code) r.code = ref.code;
      if (!r._name && ref.entityId) r._name = entityNames.get(ref.entityId) || '';
    });
  }
  return rows;
}
