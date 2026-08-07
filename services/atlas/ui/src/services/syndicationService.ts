import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, remote, asRows, USE_REAL_API, listAll } from '../api/http';
import { fillFromDeal, fillCompanyFromEntity } from './nameResolver';
import { writeAudit } from './auditService';
import { clientsService } from './clientsService';
import type { TableQuery } from './types';
import type { SynRow, SynLender } from '../pages/Syndication/syndication.types';

export const SYN_TERM = ['Dropped', 'Withdrawn', 'Rejected'];
export const SYN_CLOSED = ['Sanctioned', 'Disbursed'];

// ST2DOT — maps a lender chase status to a matrix dot state (1..6). Sanctioned is
// its OWN state (green): the desk reads "who actually approved" straight off the
// grid instead of sharing a colour with the approval track.
export const ST2DOT: Record<string, number> = {
  'Identified': 1, 'IM Circulated': 2, 'Docs Pending': 2, 'Queries Received': 3,
  'IP Received': 4, 'Sanctioned': 5, 'Declined': 6,
};

// The legal next steps per lender status — the client-side mirror of the register's
// _LENDER_TRANSITIONS map, so the matrix popover and the chase list only OFFER moves
// the server will accept. FORWARD-ONLY (review decision): a bank never regresses —
// a fresh round of queries stays at Queries Received, remarks carry the nuance.
// 'Docs Pending' is legacy (imports); it can only move forward too.
export const LENDER_NEXT: Record<string, string[]> = {
  '': ['Identified'],
  'Identified': ['IM Circulated', 'Declined'],
  'IM Circulated': ['Queries Received', 'IP Received', 'Declined'],
  'Docs Pending': ['Queries Received', 'IP Received', 'Declined'],
  'Queries Received': ['IP Received', 'Declined'],
  'IP Received': ['Sanctioned', 'Declined'],
  'Sanctioned': [],
  'Declined': [],
};

// ---------------------------------------------------------------------------
// Real-API wiring. The three syndication views (chase / matrix / by-bank) all read
// the local store synchronously and key rows by `code`, so the platform build
// HYDRATES that store from the register (GET /v1/syndication embeds lenders[] —
// one call) and stashes the REAL row ids (`apiId`) that the write paths address.
// Mock mode never hydrates and behaves exactly as the prototype did.
// ---------------------------------------------------------------------------

/** One register lender row → the store's lender shape (`apiId` = its UUID). */
function toLender(l: any): SynLender {
  return {
    apiId: l?.id,
    name: l?.lender_name || '',
    ex: !!l?.is_existing,
    st: l?.status || '',
    since: l?.since || '',
    resp: l?.response_date || '',
    chased: l?.chased_date || null,
    note: l?.note || '',
    amt: l?.amount_cr == null ? null : Number(l.amount_cr),
    h: l?.status_history || [],
  };
}

/** One register syndication row → the store's SynRow (`apiId` = its UUID). */
function toSynRow(r: any): SynRow {
  return {
    apiId: r?.id,
    entityId: r?.entity_id,
    dealId: r?.deal_id,
    id: r?.tracker_no || r?.id || '',
    // The "Group Code" column: the deal's human number (joined below), else the
    // tracker's own number — never a UUID.
    code: r?.tracker_no || '',
    _name: '',
    toi: r?.toi || '',
    rm: r?.rm || '',
    an: r?.analyst || '',
    lc: r?.lc || '',
    pri: r?.priority || '',
    status: r?.status || '',
    amt: Number(r?.amount_cr) || 0,
    synType: r?.syndication_type || '',
    mstat3: r?.mandate_status3 || '',
    fac: r?.facility || '',
    tenor: r?.tenor || '',
    im: r?.im_status || '',
    pot: r?.potential || '',
    sancL: r?.sanctioned_lender || '',
    ipL: r?.ip_lender || '',
    exist: r?.existing || '',
    price: r?.price || '',
    pendingWith: r?.pending_with || '',
    lenders: (r?.lenders || []).map(toLender),
    h: r?.status_history || [],
    createdAt: (r?.created_at || '').slice(0, 10),
    remarks: r?.remarks || '',
  };
}

let hydratedAt = 0;
let inflight: Promise<void> | null = null;
const HYDRATE_TTL_MS = 30_000;

async function loadReal(): Promise<void> {
  const rows = (await listAll('/syndication', { key: 'syndication' })).map(toSynRow);
  // Join the deal number + company through the deal, else the company via the entity.
  await fillFromDeal(rows as any[]);
  await fillCompanyFromEntity(rows as any[]);
  rows.forEach((r) => {
    if (!r.code) r.code = r.id;
    // The views name companies via clientsService.get(code) — seed that cache so a
    // register-born code resolves to the real company name, not to itself.
    if (r.code && r._name && !db().clients[r.code]) db().clients[r.code] = { name: r._name };
    // Column order: append lender names the default order doesn't know yet.
    (r.lenders || []).forEach((l: any) => {
      if (l.name && !db().lenderOrder.includes(l.name)) db().lenderOrder.push(l.name);
    });
  });
  db().syn = rows;
  hydratedAt = Date.now();
}

export const syndicationService = {
  /** Replace the local store with the register's rows (real mode; briefly cached).
   *  Fail-soft: on error the store keeps whatever it had, and the failure logs. */
  async hydrate(force = false): Promise<void> {
    if (!USE_REAL_API) return;
    if (!force && Date.now() - hydratedAt < HYDRATE_TTL_MS) return;
    if (!inflight) inflight = loadReal().finally(() => { inflight = null; });
    try { await inflight; } catch (e) { console.warn('[api] syndication hydrate failed:', e); }
  },

  async list(q: TableQuery) {
    return withFallback(
      async () => {
        await this.hydrate();
        const rows = db().syn.map((r: SynRow) => ({ ...r, _name: r._name || clientsService.get(r.code).name }));
        return applyQuery(rows, { ...q, searchFields: ['code', '_name', 'status'] });
      },
      async () => {
        await delay();
        const rows = db().syn.map((r: SynRow) => ({ ...r, _name: clientsService.get(r.code).name }));
        return applyQuery(rows, { ...q, searchFields: ['code', '_name', 'status'] });
      },
    );
  },
  byCode(code: string): SynRow[] { return db().syn.filter((r: SynRow) => r.code === code); },
  find(id: string): SynRow | undefined { return db().syn.find((r: SynRow) => r.id === id || r.apiId === id); },
  update(id: string, key: keyof SynRow, value: any, by: string) {
    const r = this.find(id); if (!r) return;
    const wire = WIRE_FIELD[key as string];
    if (r.apiId && wire) remote('patch', '/syndication/' + r.apiId, { [wire]: value });
    const old = (r as any)[key]; (r as any)[key] = value;
    if (key === 'status') (r.h = r.h || []).push({ status: value, t: today(), by });
    writeAudit(by, key === 'status' ? 'Platform Deals status' : 'Platform Deals updated', r.code, key === 'status' ? `${old} → ${value}` : String(key));
  },
  addLender(code: string, name: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); if (!r) return;
    r.lenders = r.lenders || [];
    if (r.lenders.some((l: any) => l.name.toLowerCase() === name.toLowerCase())) return;
    const local: SynLender = { name, ex: false, st: 'Identified', since: today(), resp: today(), chased: null, note: '', h: [{ st: 'Identified', t: today(), by }] };
    r.lenders.push(local);
    if (USE_REAL_API && r.apiId) {
      // Capture the created row's UUID so the follow-up status/chase writes can
      // address it without waiting for the next hydration.
      api.post<any>('/syndication/' + r.apiId + '/lenders', { lender_name: name, status: 'Identified' })
        .then((created: any) => { local.apiId = created?.id; })
        .catch((e: unknown) => console.warn('[api] write failed: add lender', e));
    }
    writeAudit(by, 'Lender added', code, name);
  },
  /** Move a lender's chase status. The two outcomes carry substance: a Declined move
   *  should hand in the reason (note), a Sanctioned move the bank's allocation
   *  (amountCr) — the register REQUIRES both, so callers capture them first. */
  setLenderStatus(code: string, name: string, st: string, by: string,
                  opts?: { note?: string; amountCr?: number | null }) {
    const r = db().syn.find((x: SynRow) => x.code === code); const e = r?.lenders?.find((l: any) => l.name === name);
    if (!e) return;
    const body: any = { status: st, since: today() };
    if (opts?.note?.trim()) body.note = opts.note.trim();
    if (opts?.amountCr != null) body.amount_cr = opts.amountCr;
    if (r?.apiId && e.apiId) remote('patch', '/syndication/' + r.apiId + '/lenders/' + e.apiId, body);
    const before = e.st; e.st = st; e.since = today(); e.resp = today();
    if (body.note) e.note = body.note;
    if (body.amount_cr != null) e.amt = body.amount_cr;
    (e.h = e.h || []).push({ st, t: today(), by });
    writeAudit(by, 'Lender status', code, `${name}: ${before || '—'} → ${st}`
      + (body.amount_cr != null ? ` · ₹${body.amount_cr} Cr` : '')
      + (body.note ? ` · ${String(body.note).slice(0, 60)}` : ''));
  },
  /** The live lender row behind a matrix cell (first mandate carrying that bank —
   *  the same row setLenderStatus writes). */
  lenderRow(code: string, name: string): SynLender | undefined {
    return db().syn.find((x: SynRow) => x.code === code)?.lenders?.find((l: any) => l.name === name);
  },
  /** Update ONLY the remark on a lender row — the manual tracker's Remarks column
   *  ("Reply awaited", "No update", "call with promoters Monday") lives at every
   *  stage, not just the outcomes, so it gets its own status-free write. */
  setLenderNote(code: string, name: string, note: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); const e = r?.lenders?.find((l: any) => l.name === name);
    if (!e) return;
    if (r?.apiId && e.apiId) remote('patch', '/syndication/' + r.apiId + '/lenders/' + e.apiId, { note });
    e.note = note;
    writeAudit(by, 'Lender note', code, `${name}: ${note.slice(0, 80)}`);
  },
  logChase(code: string, name: string, note: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); const e = r?.lenders?.find((l: any) => l.name === name);
    if (!e) return;
    // An OUTBOUND interaction against the mandate: the register rolls chased_date
    // onto the matching lender row itself, so this one call is the whole write.
    if (r?.apiId) remote('post', '/syndication/' + r.apiId + '/interactions', {
      interaction_type: 'Phone Call', direction: 'outbound', lender_name: name,
      summary: (note || 'Chase').slice(0, 300), notes: note || null, performed_by: by,
    });
    e.chased = today(); if (note) e.note = note;
    (e.h = e.h || []).push({ st: e.st || '(chase)', t: today(), by });
    writeAudit(by, 'Chased lender', code, name + (note ? ': ' + note.slice(0, 80) : ' (outbound)'));
    if (note) { db().interactions = db().interactions || []; db().interactions.push({ refId: code, refType: 'Platform Deals', occurredAt: today(), person: by, direction: 'outbound', lenderName: name, notes: note }); }
  },
  logResp(code: string, name: string, note: string, by: string) {
    const r = db().syn.find((x: SynRow) => x.code === code); const e = r?.lenders?.find((l: any) => l.name === name);
    if (!e) return;
    // INBOUND — the register rolls response_date onto the lender row.
    if (r?.apiId) remote('post', '/syndication/' + r.apiId + '/interactions', {
      interaction_type: 'Phone Call', direction: 'inbound', lender_name: name,
      summary: (note || 'Lender response').slice(0, 300), notes: note || null, performed_by: by,
    });
    e.resp = today(); if (note) e.note = note;
    (e.h = e.h || []).push({ st: e.st || '(response)', t: today(), by });
    writeAudit(by, 'Lender response', code, name + (note ? ': ' + note.slice(0, 80) : ' (inbound)'));
    if (note) { db().interactions = db().interactions || []; db().interactions.push({ refId: code, refType: 'Platform Deals', occurredAt: today(), person: by, direction: 'inbound', lenderName: name, notes: note }); }
  },
  remove(id: string, by: string) {
    const r = this.find(id);
    remote('del', '/syndication/' + (r?.apiId || id));
    const i = db().syn.findIndex((x: SynRow) => x === r || x.id === id);
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
    if (USE_REAL_API) await this.hydrate(); else await delay();
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
        cell[l.name] = { s: ST2DOT[l.st] || 1, st: l.st, since: l.since, h: l.h || [] };
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
  // Legacy standalone-matrix editor (pre-v12); the live MatrixView derives from the
  // chase-list lenders instead. Local-only: no register route stores this grid.
  cycleCell(code: string, lender: string, by: string) {
    const m = (db().matrix[code] = db().matrix[code] || {});
    const cur = m[lender] ? m[lender].s : 0;
    const s = (cur + 1) % 6;
    if (s === 0) { delete m[lender]; }
    else {
      const o = m[lender] || { h: [] };
      o.s = s; o.since = today(); (o.h = o.h || []).push({ s, t: today(), by }); m[lender] = o;
    }
    writeAudit(by, 'Matrix', code, `${lender} → ${MATRIX_LABELS[s]}`);
  },
  // Column order is a per-browser display preference — nothing to persist server-side.
  reorderLenders(from: number, to: number) {
    const o = db().lenderOrder;
    const [x] = o.splice(from, 1); o.splice(to, 0, x);
  },
  /** Merge the FI master (db().lenders — hydrated by fiService) into the matrix
   *  column order, so a FRESH mandate faces the whole market as clickable columns
   *  instead of a blank grid. Existing order is preserved; newcomers append in a
   *  deterministic reading order — banks, then small finance banks, then NBFCs,
   *  alphabetical within each group (the register lists by created_at, which ties
   *  for a bulk seed and would scramble the grid). Inactive lenders that never
   *  entered a deal stay out of the way; drag-to-reorder still wins afterwards. */
  ensureLenderColumns() {
    const order = db().lenderOrder;
    const rank: Record<string, number> = { 'Bank': 0, 'Small Finance Bank': 1, 'NBFC': 2 };
    (db().lenders || [])
      .filter((f: any) => f?.name && !f.inactive && !order.includes(f.name))
      .sort((a: any, b: any) =>
        (rank[a.type] ?? 3) - (rank[b.type] ?? 3) || a.name.localeCompare(b.name))
      .forEach((f: any) => order.push(f.name));
  },
};

// UI row key → register column, for PATCH /v1/syndication/{id}. Keys that have no
// register column (mock-only leftovers) are simply not sent.
const WIRE_FIELD: Record<string, string> = {
  toi: 'toi', rm: 'rm', an: 'analyst', lc: 'lc', pri: 'priority', status: 'status',
  amt: 'amount_cr', synType: 'syndication_type', mstat3: 'mandate_status3',
  fac: 'facility', tenor: 'tenor', im: 'im_status', pot: 'potential',
  sancL: 'sanctioned_lender', ipL: 'ip_lender', exist: 'existing', price: 'price',
  pendingWith: 'pending_with', remarks: 'remarks',
};

export interface MatrixCell { s: number; st?: string; since?: string; h?: any[]; }

// One aggregated row of the by-bank register.
export interface BankRow {
  name: string; pursued: number; sanc: number; ip: number; decl: number;
  queries: number; imCirc: number; amt: number; dots: string[];
}

// Lender chase workflow states + colours (mirrors template LSTATES / ST_COLOR)
export const LSTATES = ['Identified', 'IM Circulated', 'Docs Pending', 'Queries Received', 'IP Received', 'Sanctioned', 'Declined'];

/** Display name for a lender status. The STORED value stays 'Sanctioned' (one
 *  canonical term across history, transitions and the mandate-level statuses);
 *  every human-facing surface says "Approved" — the desk's word for a bank
 *  saying yes. Map here, never at call sites, so the rename is one line. */
export const lenderLabel = (st: string): string => (st === 'Sanctioned' ? 'Approved' : st);
export const MATRIX_LABELS = ['Un-Assigned', 'Identified', 'IM submitted', 'Queries received', 'Approval track', 'Approved', 'Declined'];
// state -> colour: yellow / orange / blue / purple (approval track) / green / red
export const MATRIX_COLORS = ['transparent', '#E0B400', '#E07B1F', '#2D6FC4', '#6B5AAE', '#2E7D4F', '#B3432B'];

// ONE palette everywhere: a lender status colours identically in the chase list,
// the matrix, the by-bank register and the dashboards — derived from the matrix
// palette through ST2DOT so the two can never drift again.
export const LSTATE_COLOR: Record<string, string> = Object.fromEntries(
  Object.entries(ST2DOT).map(([st, s]) => [st, MATRIX_COLORS[s]]),
);

export const MATRIX_PRESETS = [
  { id: 'await', label: 'Awaiting lender ≥7d', states: [2], dwell: 7, scope: 'Live' as const },
  { id: 'ballus', label: 'Ball with us ≥5d', states: [3], dwell: 5, scope: 'Live' as const },
  { id: 'appr', label: 'Approval track', states: [4], dwell: '', scope: 'Live' as const },
  { id: 'sanc', label: 'Approved', states: [5], dwell: '', scope: 'All' as const },
  { id: 'decl', label: 'Declined', states: [6], dwell: '', scope: 'All' as const },
  { id: 'noout', label: 'No outreach yet', states: [] as number[], dwell: '', scope: 'Live' as const, noout: true },
];
