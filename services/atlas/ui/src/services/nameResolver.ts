import { listAll, USE_REAL_API } from '../api/http';

/**
 * The register keeps grids NORMALISED: a deal row carries entity_id (no company name),
 * a tracker row (lending / asset-mon / syndication) carries deal_id (no deal number and
 * no company). The prototype's grids render both — so the platform build joins them
 * client-side from two small lookup maps (entity id → display name, deal id → number +
 * entity), fetched once and cached briefly. Everything here fails SOFT: a resolver
 * outage leaves the joined columns blank, it never fails the grid that asked.
 */

type DealRef = { code: string; entityId: string | null };
type EntityRef = { name: string; code: string; lens: string };

let entities = new Map<string, EntityRef>();
let dealRefs = new Map<string, DealRef>();
let loadedAt = 0;
let inflight: Promise<void> | null = null;
const TTL_MS = 60_000;

async function load(): Promise<void> {
  const [ents, deals] = await Promise.all([
    listAll('/entities', { key: 'entities' }),
    listAll('/deals', { key: 'deals' }),
  ]);
  const e = new Map<string, EntityRef>();
  ents.forEach((r: any) => {
    if (!r?.id) return;
    // The CODE as well as the name: a deal row's "Group Code" is the company's code, and
    // the company drawer is addressed by it. Without it a converted deal rendered a blank
    // Group Code and clicking the row opened nothing at all, because the drawer was being
    // asked to open '' — the deal was on the register and unreachable from the grid.
    e.set(String(r.id), { name: r.display_name || r.legal_name || '', code: r.code || '',
      lens: r.lens || '' });
  });
  const d = new Map<string, DealRef>();
  deals.forEach((r: any) => {
    if (r?.id) d.set(String(r.id), {
      code: r.deal_no || r.code || '',
      entityId: r.entity_id ? String(r.entity_id) : null,
    });
  });
  entities = e;
  dealRefs = d;
  loadedAt = Date.now();
}

async function ensure(): Promise<void> {
  if (!USE_REAL_API) return;
  if (Date.now() - loadedAt < TTL_MS) return;
  if (!inflight) inflight = load().finally(() => { inflight = null; });
  try { await inflight; } catch (e) { console.warn('[api] name lookup failed:', e); }
}

/**
 * Deals grid: fill COMPANY and GROUP CODE from the row's entity_id. Mutates in place.
 *
 * The code matters as much as the name — it is what the row click passes to the company
 * drawer. A deal carries `deal_no` only when one was assigned, so on a converted deal
 * both were blank and the row was a dead end.
 */
export async function fillCompanyFromEntity<T extends { _name?: string; code?: string; entityId?: string; lens?: string }>(rows: T[]): Promise<T[]> {
  if (rows.some((r) => r.entityId && (!r._name || !r.code || !r.lens))) {
    await ensure();
    rows.forEach((r) => {
      const ref = r.entityId ? entities.get(String(r.entityId)) : undefined;
      if (!ref) return;
      if (!r._name) r._name = ref.name;
      if (!r.code) r.code = ref.code;
      // The climate lens is really a COMPANY attribute: a deal carries its own copy
      // (from the lead at conversion, or the Leads sheet on import), but a deal-only
      // company has no leads row to copy from and its column sat blank while the
      // company profile said Mitigation right there in the drawer. Show the company's
      // lens whenever the deal has none of its own.
      if (!r.lens && ref.lens) r.lens = ref.lens;
    });
  }
  return rows;
}

/**
 * Tracker grids: fill GROUP CODE + COMPANY from the row's deal_id, FALLING BACK to the
 * row's own entity_id.
 *
 * The deal is only ever a shortcut. A tracker row always carries entity_id — the register
 * requires it — but deal_id is set only when that company also has a row on the Deals
 * sheet, so a mandate for a company that was never a deal arrived with deal_id NULL and
 * rendered a blank Company. On the asset-monetisation grid that is not an edge case: a
 * land parcel listed under its own name ("Axel Renewable Private Limited (Land in
 * Solapur)") is its own company and rarely has a deal, so whole pages of the register
 * looked nameless while the data was complete underneath.
 *
 * Mutates in place, and still fails soft: an unresolvable row keeps its blanks rather
 * than failing the grid.
 */
export async function fillFromDeal<T extends { _name?: string; code?: string; dealId?: string; entityId?: string }>(rows: T[]): Promise<T[]> {
  if (rows.some((r) => (r.dealId || r.entityId) && (!r._name || !r.code))) {
    await ensure();
    rows.forEach((r) => {
      const ref = r.dealId ? dealRefs.get(String(r.dealId)) : undefined;
      // The deal's entity when there is a deal; the row's own entity when there is not.
      const ent = entities.get(String(ref?.entityId || r.entityId || ''));
      // Prefer the company's group code; a deal number is not what the drawer opens on.
      if (!r.code) r.code = ent?.code || ref?.code || '';
      if (!r._name) r._name = ent?.name || '';
    });
  }
  return rows;
}
