import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, toParams, remote } from '../api/http';
import { writeAudit } from './auditService';
import { clientsService } from './clientsService';
import type { TableQuery } from './types';
import type { SynRow } from '../pages/Syndication/syndication.types';

export const SYN_TERM = ['Dropped', 'Withdrawn', 'Rejected'];
export const SYN_CLOSED = ['Sanctioned', 'Disbursed'];

// v12 ST2DOT — maps a lender chase status to a matrix dot state (1..5).
export const ST2DOT: Record<string, number> = {
  'Identified': 1, 'IM Circulated': 2, 'Docs Pending': 2, 'Queries Received': 3,
  'IP Received': 4, 'Sanctioned': 4, 'Declined': 5,
};

export const syndicationService = {
  async list(q: TableQuery) {
    return withFallback(
      () => api.get<any>('/syndication', toParams(q)),
      async () => {
        await delay();
        const rows = db().syn.map((r: SynRow) => ({ ...r, _name: clientsService.get(r.code).name }));
        return applyQuery(rows, { ...q, searchFields: ['code', '_name', 'status'] });
      },
    );
  },
  byCode(code: string): SynRow[] { return db().syn.filter((r: SynRow) => r.code === code); },
  find(id: string): SynRow | undefined { return db().syn.find((r: SynRow) => r.id === id); },
  update(id: string, key: keyof SynRow, value: any, by: string) {
    const r = this.find(id); if (!r) return;
    remote('patch', '/syndication/' + id, { [key]: value });
    const old = (r as any)[key]; (r as any)[key] = value;
    if (key === 'status') (r.h = r.h || []).push({ status: value, t: today(), by });
    writeAudit(by, key === 'status' ? 'Platform Deals status' : 'Platform Deals updated', r.code, key === 'status' ? `${old} → ${value}` : String(key));
  },
  addLender(code: string, name: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); if (!r) return;
    r.lenders = r.lenders || [];
    if (r.lenders.some((l: any) => l.name.toLowerCase() === name.toLowerCase())) return;
    remote('post', '/syndication/' + code + '/lenders', { name });
    r.lenders.push({ name, ex: false, st: 'Identified', since: today(), resp: today(), chased: null, note: '', h: [{ st: 'Identified', t: today(), by }] });
    writeAudit(by, 'Lender added', code, name);
  },
  setLenderStatus(code: string, name: string, st: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); const e = r?.lenders?.find((l: any) => l.name === name);
    if (!e) return; remote('patch', '/syndication/' + code + '/lenders/' + encodeURIComponent(name), { st });
    const before = e.st; e.st = st; e.since = today(); e.resp = today();
    (e.h = e.h || []).push({ st, t: today(), by });
    writeAudit(by, 'Lender status', code, `${name}: ${before || '—'} → ${st}`);
  },
  logChase(code: string, name: string, note: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); const e = r?.lenders?.find((l: any) => l.name === name);
    if (!e) return; remote('post', '/syndication/' + code + '/lenders/' + encodeURIComponent(name) + '/chase', { note });
    e.chased = today(); if (note) e.note = note;
    (e.h = e.h || []).push({ st: e.st || '(chase)', t: today(), by });
    writeAudit(by, 'Chased lender', code, name + (note ? ': ' + note.slice(0, 80) : ' (outbound)'));
    if (note) { db().interactions = db().interactions || []; db().interactions.push({ refId: code, refType: 'Platform Deals', occurredAt: today(), person: by, direction: 'outbound', lenderName: name, notes: note }); }
  },
  logResp(code: string, name: string, note: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); const e = r?.lenders?.find((l: any) => l.name === name);
    if (!e) return; remote('post', '/syndication/' + code + '/lenders/' + encodeURIComponent(name) + '/response', { note });
    e.resp = today(); if (note) e.note = note;
    (e.h = e.h || []).push({ st: e.st || '(response)', t: today(), by });
    writeAudit(by, 'Lender response', code, name + (note ? ': ' + note.slice(0, 80) : ' (inbound)'));
    if (note) { db().interactions = db().interactions || []; db().interactions.push({ refId: code, refType: 'Platform Deals', occurredAt: today(), person: by, direction: 'inbound', lenderName: name, notes: note }); }
  },
  remove(id: string, by: string) {
    remote('del', '/syndication/' + id);
    const i = db().syn.findIndex((r: SynRow) => r.id === id);
    if (i > -1) { const [x] = db().syn.splice(i, 1); writeAudit(by, 'Platform Deals deleted', x.code, x.id); }
  },

  // ---- Register by bank (v12 vSynReg) ----
  // Aggregate every outreach lender across all mandates, by financial institution.
  registerByBank(): BankRow[] {
    const banks: Record<string, BankRow> = {};
    db().syn.forEach((r: SynRow) => (r.lenders || []).forEach((l: any) => {
      if (l.ex) return;
      const b = banks[l.name] = banks[l.name] || { name: l.name, pursued: 0, sanc: 0, ip: 0, decl: 0, queries: 0, imCirc: 0, amt: 0, dots: [] };
      b.pursued++;
      b.amt += Number(r.amt) || 0;
      b.dots.push(l.st || 'Identified');
      if (l.st === 'Sanctioned') b.sanc++;
      else if (l.st === 'IP Received') b.ip++;
      else if (l.st === 'Declined') b.decl++;
      else if (l.st === 'Queries Received') b.queries++;
      else if (l.st === 'IM Circulated') b.imCirc++;
    }));
    return Object.values(banks);
  },
  async registerList(q: TableQuery) {
    await delay();
    return applyQuery(this.registerByBank(), { ...q, searchFields: ['name'] });
  },

  // ---- Matrix (dot grid: companies x lenders) ----
  // v12's final matrix is READ-ONLY and derived live from the chase-list lender
  // statuses (ST2DOT), not from a separate S.matrix — so the two views never drift.
  matrixFromLenders(): Record<string, Record<string, MatrixCell>> {
    const mx: Record<string, Record<string, MatrixCell>> = {};
    db().syn.forEach((r: SynRow) => {
      const cell = mx[r.code] = mx[r.code] || {};
      (r.lenders || []).forEach((l: any) => {
        if (l.ex || !l.st) return;
        cell[l.name] = { s: ST2DOT[l.st] || 1, since: l.since, h: l.h || [] };
      });
    });
    return mx;
  },
  offLive(code: string): number {
    return db().syn.filter((r: SynRow) => r.code === code && !SYN_TERM.includes(r.status)).reduce((a, r) => a + (Number(r.amt) || 0), 0);
  },
  lenderOrder(): string[] { return db().lenderOrder; },
  matrix(): Record<string, Record<string, MatrixCell>> { return db().matrix; },
  cell(code: string, lender: string): MatrixCell | null { return (db().matrix[code] || {})[lender] || null; },
  cycleCell(code: string, lender: string, by: string) {
    const m = (db().matrix[code] = db().matrix[code] || {});
    const cur = m[lender] ? m[lender].s : 0;
    const s = (cur + 1) % 6;
    remote('patch', '/syndication/matrix/' + code + '/' + encodeURIComponent(lender), { s });
    if (s === 0) { delete m[lender]; }
    else {
      const o = m[lender] || { h: [] };
      o.s = s; o.since = today(); (o.h = o.h || []).push({ s, t: today(), by }); m[lender] = o;
    }
    writeAudit(by, 'Matrix', code, `${lender} → ${MATRIX_LABELS[s]}`);
  },
  reorderLenders(from: number, to: number) {
    const o = db().lenderOrder;
    const [x] = o.splice(from, 1); o.splice(to, 0, x);
    remote('patch', '/syndication/matrix/lender-order', { order: o });
  },
};

export interface MatrixCell { s: number; since?: string; h?: { s: number; t: string; by: string }[]; }

// One aggregated row of the by-bank register.
export interface BankRow {
  name: string; pursued: number; sanc: number; ip: number; decl: number;
  queries: number; imCirc: number; amt: number; dots: string[];
}

// Lender chase workflow states + colours (mirrors template LSTATES / ST_COLOR)
export const LSTATES = ['Identified', 'IM Circulated', 'Docs Pending', 'Queries Received', 'IP Received', 'Sanctioned', 'Declined'];
export const LSTATE_COLOR: Record<string, string> = {
  Identified: '#94a3b8', 'IM Circulated': '#64748b', 'Docs Pending': '#0891b2',
  'Queries Received': '#2563eb', 'IP Received': '#d97706', Sanctioned: '#059669', Declined: '#dc2626',
};

export const MATRIX_LABELS = ['Not in play', 'Identified — to showcase', 'IM submitted', 'Queries received', 'Approval track', 'Declined'];
// state -> colour (mirrors template --m1..--m5)
export const MATRIX_COLORS = ['transparent', '#E0B400', '#E07B1F', '#2D6FC4', '#2E7D4F', '#B3432B'];

export const MATRIX_PRESETS = [
  { id: 'show', label: 'To showcase', states: [1], dwell: '', scope: 'Live' as const },
  { id: 'await', label: 'Awaiting lender ≥7d', states: [2], dwell: 7, scope: 'Live' as const },
  { id: 'ballus', label: 'Ball with us ≥5d', states: [3], dwell: 5, scope: 'Live' as const },
  { id: 'appr', label: 'Approval track', states: [4], dwell: '', scope: 'Live' as const },
  { id: 'decl', label: 'Declined', states: [5], dwell: '', scope: 'All' as const },
  { id: 'noout', label: 'No outreach yet', states: [] as number[], dwell: '', scope: 'Live' as const, noout: true },
];
