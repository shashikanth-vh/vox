import { db } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, remote, asRows, USE_REAL_API, listAll } from '../api/http';
import { syndicationService } from './syndicationService';
import { writeAudit } from './auditService';
import type { TableQuery } from './types';
import type { FiRow, FiDeal, FiLedgerRow } from '../pages/FIMaster/fi.types';

interface FiEng { pursued: number; sanc: number; ip: number; decl: number; live: number; cos: FiDeal[] }

/** One register counterparty → the store's lender-master shape (`apiId` = its UUID). */
function toFi(r: any) {
  return {
    apiId: r?.id,
    name: r?.name || '',
    type: r?.counterparty_type || '',
    preferredSectors: r?.sectors || '',
    notes: r?.notes || '',
    inactive: r?.is_active === false,
  };
}

/** UI patch keys → register columns (PATCH /v1/counterparties/{id}). */
function toFiPatch(patch: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = {};
  if ('name' in patch) out.name = patch.name;
  if ('type' in patch) out.counterparty_type = patch.type;
  if ('preferredSectors' in patch) out.sectors = patch.preferredSectors;
  if ('notes' in patch) out.notes = patch.notes;
  if ('inactive' in patch) out.is_active = !patch.inactive;
  return out;
}

let hydratedAt = 0;
const HYDRATE_TTL_MS = 30_000;

export const fiService = {
  /** Real mode: replace the lender master from GET /v1/counterparties (cached briefly).
   *  The engagement rollup additionally needs the syndication book — hydrated too. */
  async hydrate(): Promise<void> {
    if (!USE_REAL_API) return;
    const jobs: Promise<any>[] = [syndicationService.hydrate()];
    if (Date.now() - hydratedAt >= HYDRATE_TTL_MS) {
      jobs.push(listAll('/counterparties', { key: 'counterparties' }).then((rows: any[]) => {
        db().lenders = rows.map(toFi);
        hydratedAt = Date.now();
      }));
    }
    try { await Promise.all(jobs); } catch (e) { console.warn('[api] fi hydrate failed:', e); }
  },

  async list(q: TableQuery) {
    return withFallback(
      async () => {
        // The grid's engagement columns (# pursued / live / IP / sanctioned /
        // declined) derive from the SYNDICATION book, so the rollup runs over the
        // hydrated stores — one shape in both modes.
        await this.hydrate();
        return applyQuery(fiService.rollup(), { ...q, searchFields: ['name', 'type', 'preferredSectors', 'sectors'] });
      },
      async () => {
        await delay();
        const rows = fiService.rollup();
        return applyQuery(rows, { ...q, searchFields: ['name', 'type', 'preferredSectors', 'sectors'] });
      },
    );
  },
  // v12 vFI(): engagement rollup per lender, computed from the syndication book.
  // A lender counts as "live" while it's still in play — anything other than
  // Sanctioned or Declined.
  rollup(): FiRow[] {
    const eng: Record<string, Omit<FiEng, never>> = {};
    db().syn.forEach((r: any) => (r.lenders || []).forEach((l: any) => {
      if (!l.name) return;
      const e = eng[l.name] = eng[l.name] || { pursued: 0, sanc: 0, ip: 0, decl: 0, live: 0, cos: [] };
      e.pursued++;
      if (l.st === 'Sanctioned') e.sanc++;
      if (l.st === 'IP Received') e.ip++;
      if (l.st === 'Declined') e.decl++;
      if (l.st && !['Sanctioned', 'Declined'].includes(l.st)) e.live++;
      e.cos.push({ co: db().clients?.[r.code]?.name || r.code, code: r.code, st: l.st, resp: l.resp, amt: Number(r.amt) || 0 });
    }));
    return db().lenders.map((f: any, i: number) => {
      const e = eng[f.name] || { pursued: 0, sanc: 0, ip: 0, decl: 0, live: 0, cos: [] };
      return { ...f, _i: i, engagements: e.pursued, ...e };
    });
  },

  // v12 openBank(name): the full deal ledger for one bank across every mandate,
  // split into live vs closed/declined. Richer than the rollup's `cos`.
  ledger(name: string): { rows: FiLedgerRow[]; active: FiLedgerRow[]; past: FiLedgerRow[] } {
    const rows: FiLedgerRow[] = [];
    db().syn.forEach((r: any) => (r.lenders || []).forEach((l: any) => {
      if (l.name !== name || l.ex) return;
      rows.push({
        code: r.code, co: db().clients?.[r.code]?.name || r.code, amt: Number(r.amt) || 0,
        st: l.st, resp: l.resp, note: l.note, synStatus: r.status, rm: r.rm, an: r.an,
      });
    }));
    const closed = (x: FiLedgerRow) => x.st === 'Declined' || x.st === 'Sanctioned';
    return { rows, active: rows.filter((x) => !closed(x)), past: rows.filter(closed) };
  },

  // Add a bank / financial institution to the lender master.
  create(input: { name: string; type?: string; preferredSectors?: string; notes?: string }, by: string): { ok: boolean; error?: string } {
    const name = (input.name || '').trim();
    if (!name) return { ok: false, error: 'Bank / FI name is required' };
    if (db().lenders.some((l: any) => (l.name || '').toLowerCase() === name.toLowerCase())) return { ok: false, error: `${name} is already on the register` };
    const fi: any = { name, type: input.type || '', preferredSectors: input.preferredSectors || '', notes: input.notes || '', inactive: false };
    db().lenders.push(fi);
    if (USE_REAL_API) {
      // Capture the created row's UUID so a follow-up edit can address it.
      api.post<any>('/counterparties', {
        name, counterparty_type: input.type || null,
        sectors: input.preferredSectors || null, notes: input.notes || null,
      }).then((created: any) => { fi.apiId = created?.id; })
        .catch((e: unknown) => console.warn('[api] write failed: add counterparty', e));
    }
    writeAudit(by, 'FI updated', name, 'FI added');
    return { ok: true };
  },
  update(index: number, patch: Partial<FiRow>, by: string) {
    const f = db().lenders[index]; if (!f) return;
    if (f.apiId) remote('patch', '/counterparties/' + f.apiId, toFiPatch(patch));
    Object.assign(f, patch); writeAudit(by, 'FI updated', f.name, Object.keys(patch).join(','));
  },
};
