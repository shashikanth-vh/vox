import { db } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, toParams, remote } from '../api/http';
import { writeAudit } from './auditService';
import type { TableQuery } from './types';
import type { FiRow, FiDeal, FiLedgerRow } from '../pages/FIMaster/fi.types';

interface FiEng { pursued: number; sanc: number; ip: number; decl: number; live: number; cos: FiDeal[] }

export const fiService = {
  async list(q: TableQuery) {
    return withFallback(
      () => api.get<any>('/fi', toParams(q)),
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
    remote('post', '/fi', fi);
    db().lenders.push(fi);
    writeAudit(by, 'FI updated', name, 'FI added');
    return { ok: true };
  },
  update(index: number, patch: Partial<FiRow>, by: string) {
    remote('patch', '/fi/' + index, patch);
    Object.assign(db().lenders[index], patch); writeAudit(by, 'FI updated', db().lenders[index].name, Object.keys(patch).join(','));
  },
};
