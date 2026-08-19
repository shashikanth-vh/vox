import { db } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, remote, toCursorParams, asRows, nextCursorOf, totalOf, listAll, isRegisterId, USE_REAL_API } from '../api/http';
import { fillFromDeal } from './nameResolver';
import { writeAudit } from './auditService';
import { clientsService } from './clientsService';
import type { TableQuery, Paged } from './types';
import type { AmRow } from '../pages/AssetMonetisation/am.types';
import { inScope, type RowScope } from '../auth/rbac';

/** The register's path — `asset-monetisation`, as the collection spells it. */
const AM_PATH = '/asset-monetisation';

// Client mirror of the register's _AM_PIPELINE transition graph (forward one step,
// back one step for rework, Dropped from any live state) so the status dropdowns
// offer only moves the register will accept. The AM book is a plain update surface
// (desk review decision): no workflow, no approval — Closed is an ordinary move
// from SPA / Documentation. The two terminals are final.
export const AM_NEXT: Record<string, string[]> = {
  'Teaser Prepared': ['Teaser Shared', 'Dropped'],
  'Teaser Shared': ['In Discussion', 'Teaser Prepared', 'Dropped'],
  'In Discussion': ['NBO Received', 'Teaser Shared', 'Dropped'],
  'NBO Received': ['BO Received', 'In Discussion', 'Dropped'],
  'BO Received': ['SPA / Documentation', 'NBO Received', 'Dropped'],
  'SPA / Documentation': ['Closed', 'BO Received', 'Dropped'],
  'Closed': [],
  'Dropped': [],
};

/**
 * The ENTRY states — what the register accepts when a mandate's status is set for the
 * FIRST time. Mirrors INITIAL_STATUS['AssetMonetisation']: a row may be born at the start
 * of its lifecycle, never part-way along it.
 */
export const AM_ENTRY = ['Teaser Prepared', 'Teaser Shared', 'In Discussion'];

/**
 * The choices a status dropdown should offer from `current` (current first).
 *
 * A mandate with NO status yet is NOT stuck. The desk creates placeholder rows — a company
 * flagged Asset Mon on the Deals sheet with no tracker entry behind it — and the import
 * lands them with the status blank. Setting a blank status is an INITIAL set, which the
 * register governs by the entry allowlist rather than the forward graph, so those are the
 * choices to offer. Returning [] left every such mandate with a dropdown that was empty AND
 * (being <= 1 option) disabled: the one screen for the job refused to do it.
 */
export const amStatusOptions = (current: string): string[] =>
  (current ? [current, ...(AM_NEXT[current] ?? [])] : AM_ENTRY)
    .filter((s, i, a) => s && a.indexOf(s) === i);

/**
 * An API asset-monetisation line read back as the row the grid renders. The wire is
 * snake_case (value_cr, size_mw, investor_type); unmapped, the columns render blank.
 */
export function toAmRow(r: any): AmRow {
  return {
    id: r?.id || '',
    dealId: r?.deal_id,
    entityId: r?.entity_id,
    // The "Group Code" column: the deal's human number, not a UUID.
    code: r?.deal_no || r?.code || '',
    _name: r?.company || r?.entity_name || r?.display_name || '',
    state: r?.state || '',
    // The register's read field is indicative_value_cr (the update map below always
    // knew this); the old value_cr key made every register row display ₹0.00.
    val: Number(r?.indicative_value_cr ?? r?.value_cr ?? r?.amount_cr) || 0,
    mw: Number(r?.size_mw ?? r?.mw) || 0,
    nature: r?.nature || '',
    dtype: r?.deal_type || '',
    inv: r?.investor || '',
    itype: r?.investor_type || '',
    status: r?.status || '',
    teaser: r?.teaser ?? null,
    createdAt: (r?.created_at || '').slice(0, 10),
    notes: r?.notes || '',
  };
}

/** Live/closed/dropped totals + investor & status spreads, from whichever rows exist. */
export function computeAmSummary(rows: AmRow[]) {
  const live = rows.filter((a) => !['Closed', 'Dropped'].includes(a.status));
  const closed = rows.filter((a) => a.status === 'Closed');
  const dropped = rows.filter((a) => a.status === 'Dropped');
  const totVal = (n: AmRow[]) => n.reduce((a, x) => a + (Number(x.val) || 0), 0);
  const totMW = (n: AmRow[]) => n.reduce((a, x) => a + (Number(x.mw) || 0), 0);
  const tally = (key: (a: AmRow) => string) => {
    const m: Record<string, number> = {};
    live.forEach((a) => { const k = key(a); m[k] = (m[k] || 0) + 1; });
    return Object.entries(m);
  };
  return {
    live: { n: live.length, val: totVal(live), mw: totMW(live) },
    closed: { n: closed.length, val: totVal(closed) },
    dropped: { n: dropped.length, val: totVal(dropped) },
    investors: tally((a) => a.itype || 'Other'),
    statuses: tally((a) => a.status || '—'),
  };
}

/** Read-through cache — the company drawer reads AM lines from the store, by code, so
 *  they must be the REGISTER's rows and not a local optimistic copy. */
function hydrateAm(rows: AmRow[]): void {
  const store = db().am as AmRow[];
  rows.forEach((row) => {
    const i = store.findIndex((r) => r.id === row.id);
    if (i >= 0) store[i] = { ...store[i], ...row };
    else store.unshift(row);
  });
  const codes = new Set(rows.map((r) => r.code).filter(Boolean));
  for (let i = store.length - 1; i >= 0; i--) {
    const r = store[i];
    if (r && codes.has(r.code) && !rows.some((x) => x.id === r.id)) store.splice(i, 1);
  }
}

export const assetMonService = {
  /** The WHOLE AM book into the store (live mode) — dashboard-grade completeness. */
  async hydrateAll(): Promise<void> {
    if (!USE_REAL_API) return;
    const rows = (await listAll(AM_PATH, { key: 'asset_monetisation' })).map(toAmRow);
    await fillFromDeal(rows);
    const store = db().am as AmRow[];
    store.length = 0;
    store.push(...rows);
  },

  async list(q: TableQuery, scope?: RowScope | null) {
    return withFallback<Paged<AmRow>>(
      async () => {
        // Server-paged like the other registers: `limit` is the table's page size and
        // Next carries the previous page's cursor, so applyQuery must NOT re-slice it.
        const data = await api.get<any>(AM_PATH, toCursorParams(q));
        // No inScope here: the register already scoped this list (see auth/rbac.ts).
        const rows = asRows(data, 'asset_monetisation').map(toAmRow);
        // The wire row carries deal_id only — join the deal number + company in.
        await fillFromDeal(rows);
        hydrateAm(rows);
        return { rows, total: totalOf(data, rows.length), nextCursor: nextCursorOf(data) };
      },
      async () => {
        await delay();
        const rows = db().am.map((r: AmRow) => ({ ...r, _name: clientsService.get(r.code).name })).filter((r: any) => inScope(scope ?? null, r));
        return applyQuery(rows, { ...q, searchFields: ['code', '_name', 'status', 'state'] });
      },
    );
  },
  // v12 vAM overlay strip: live/closed/dropped totals + investor & status spreads.
  // Computed over the REAL register rows on the platform (mock's tiles beside a real
  // grid read as phantom mandates); mock mode keeps the bundled store.
  async summary() {
    return withFallback(
      async () => {
        const rows = await listAll(AM_PATH, { key: 'asset_monetisation' });
        return computeAmSummary(rows.map(toAmRow));
      },
      async () => { await delay(); return computeAmSummary(db().am as AmRow[]); },
    );
  },
  byCode(code: string): AmRow[] { return db().am.filter((r: AmRow) => r.code === code); },
  find(id: string): AmRow | undefined { return db().am.find((r: AmRow) => r.id === id); },
  // UI field → the AssetMonUpdate wire name — same defect, same cure as lending.
  update(id: string, key: keyof AmRow, value: any, by: string) {
    const r = this.find(id); if (!r) return;
    const wire: Record<string, string> = {
      state: 'state', val: 'indicative_value_cr', mw: 'size_mw', nature: 'nature',
      dtype: 'deal_type', inv: 'investor', itype: 'investor_type', status: 'status',
      teaser: 'teaser_date', notes: 'notes',
    };
    if (isRegisterId(id) && wire[key as string]) {
      remote('patch', AM_PATH + '/' + id, { [wire[key as string]]: value === '' ? null : value });
    }
    const old = (r as any)[key]; (r as any)[key] = value;
    writeAudit(by, key === 'status' ? 'Asset Mon status' : 'Asset Mon updated', r.code, key === 'status' ? `${old} → ${value}` : String(key));
  },
  remove(id: string, by: string) {
    if (isRegisterId(id)) remote('del', AM_PATH + '/' + id);
    const i = db().am.findIndex((r: AmRow) => r.id === id);
    if (i > -1) { const [x] = db().am.splice(i, 1); writeAudit(by, 'Asset Mon deleted', x.code, x.id); }
  },
};
