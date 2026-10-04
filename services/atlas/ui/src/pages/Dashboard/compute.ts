// Dashboard computation — ports the ATLAS v11 template dashboard (vDashV4 + hero)
// onto the live store. All figures derive from db(); pass a `person` to scope every
// section to that RM/Analyst's book (mirrors the template's dashScopeS).
import { db } from '../../api/atlasStore';
import { num } from '../../utils/format';
import { referenceService } from '../../services/referenceService';
import { SYN_TERM, SYN_CLOSED } from '../../services/syndicationService';

export interface DrillRow { code: string; name: string; sector: string; amt: number; }
export interface BarRow { label: string; value: number; note?: string; }
export interface GeoRow { state: string; pipeCount: number; pipeAmt: number; closedCount: number; closedAmt: number; }
export interface LensRow { lens: string; lending: number; syn: number; am: number; total: number; }
export interface FunnelRow { status: string; rows: number; amt: number; }
export interface VelRow { stage: string; n: number; median: number | null; p75: number | null; }
export interface RmRow { name: string; sourced: number; activeLeads: number; hotLeads: number; converted: number; pipeline: number; closed: number; }
export interface AnRow { name: string; activeLend: number; activeSyn: number; activeAM: number; closed: number; rejected: number; }
export interface BankRow { name: string; pursued: number; live: number; sanc: number; decl: number; }

export interface DashboardV11 {
  hero: { closedAmt: number; closedN: number; pipeAmt: number; pipeN: number; conv: number; clients: number; lenders: number; liveMandates: number };
  closed: { lend: DrillRow[]; syn: DrillRow[]; am: DrillRow[] };
  sanctioned: DrillRow[];        // v16: lending Sanctioned/Documentation/Disbursed
  pipe: { lend: DrillRow[]; syn: DrillRow[]; am: DrillRow[] };
  sector: BarRow[];              // active pipeline by sector
  sectorClosed: BarRow[];        // deals closed by sector
  ma: BarRow[];                  // Mitigation vs Adaptation breakup
  lens: LensRow[];
  geography: GeoRow[];
  geoClosed: BarRow[];           // deals closed by geography
  geoPipe: BarRow[];             // active pipeline by geography
  funnel: FunnelRow[];           // syndication most-advanced-stage
  lendingFunnel: FunnelRow[];    // lending most-advanced-stage
  velocity: VelRow[];
  leadTouch: BarRow[];
  sourcing: BarRow[];
  rm: RmRow[];
  analyst: AnRow[];
  bank: BankRow[];
}

// Sanctioned and not yet disbursed — the lending book's third state (see the
// buckets below). Change this list to move a stage between the two tiles.
export const LEND_SANCTIONED = ['Sanctioned', 'CP/CS Completed', 'Ready for Disbursement'];

const median = (a: number[]): number | null => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y), m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const p75 = (a: number[]): number | null => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  return s[Math.floor(s.length * 0.75)];
};

// ---- people: a stored rm/analyst is a HANDLE ("AT"), a full name ("Arun Tiwari")
// or whatever an import carried ("chetan"); the roster (/v1/ref) knows the handle
// and the label. Matching is normalised across all of them, so "?person=Chetan"
// from the Employees page and a lead filed under "Chetan Malik" meet (B13), and
// the RM table does not silently drop rows whose spelling differs (B44).
const norm = (s: any) => String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
function rosterIndex(): { handles: string[]; keyToHandle: Map<string, string> } {
  const handles: string[] = [...new Set([...(referenceService.getRefSync('RM') || []), ...(referenceService.getRefSync('Analyst') || [])])];
  const labels = { ...referenceService.getRefLabels('Analyst'), ...referenceService.getRefLabels('RM') };
  const keyToHandle = new Map<string, string>();
  const firstTokens = new Map<string, string[]>();
  handles.forEach((h) => {
    const label = labels[h] || '';
    [h, label].map(norm).filter(Boolean).forEach((k) => keyToHandle.set(k, h));
    // "Chetan" for "Chetan Malik" — only when one person on the roster starts so.
    const ft = norm(label || h).split(' ')[0];
    if (ft) firstTokens.set(ft, [...(firstTokens.get(ft) || []), h]);
  });
  firstTokens.forEach((hs, ft) => { if (hs.length === 1 && !keyToHandle.has(ft)) keyToHandle.set(ft, hs[0]); });
  return { handles, keyToHandle };
}
/** The roster handle a stored person value resolves to, or null when not on the roster. */
export function resolvePerson(value: any, idx = rosterIndex()): string | null {
  const n = norm(value);
  if (!n) return null;
  return idx.keyToHandle.get(n) ?? idx.keyToHandle.get(n.split(' ')[0]) ?? null;
}

export function computeDashboard(person?: string): DashboardV11 {
  const D = db();
  const cli = (code: string) => (D.clients && D.clients[code]) || { name: code, sector: 'Other', lens: '', state: '' };
  const idx = rosterIndex();
  // The person asked for, as a roster handle when the roster knows them; a name the
  // roster does not know still matches its own exact (normalised) spelling.
  const who = person ? (resolvePerson(person, idx) ?? norm(person)) : '';
  const same = (v: any) => !!norm(v) && ((resolvePerson(v, idx) ?? norm(v)) === who);
  const mine = (r: any) => !person || same(r.rm) || same(r.an);

  const leads = (D.leads || []).filter(mine);
  const deals = (D.deals || []).filter(mine);
  const lending = (D.lending || []).filter(mine);
  const syn = (D.syn || []).filter(mine);
  const am = (D.am || []).filter(mine);

  const drill = (r: any, valKey = 'amt'): DrillRow => ({ code: r.code, name: cli(r.code).name, sector: cli(r.code).sector || 'Other', amt: num(r[valKey]) });

  // ---- closed / pipeline buckets ----
  // Lending has three money states, not two: DISBURSED (closed), SANCTIONED but
  // not yet disbursed (its own tile under Closed Deals: Sanctioned, CP/CS
  // Completed, Ready for Disbursement) and everything before sanction (pipeline).
  // The Sanctioned tile and the Pipeline tile used to overlap on 'Sanctioned', so
  // a ₹10 Cr sanction was counted twice (B12). The LIVE book — every line not
  // disbursed, dropped or rejected — still feeds the sector / lens / geography
  // charts, which describe where the work is, not a tile sum.
  const closedLend = lending.filter((r: any) => r.stage === 'Disbursed');
  const closedSyn = syn.filter((r: any) => SYN_CLOSED.includes(r.status));
  const closedAm = am.filter((a: any) => a.status === 'Closed');
  const liveLend = lending.filter((r: any) => !['Disbursed', 'Dropped', 'Rejected'].includes(r.stage));
  const pipeLend = liveLend.filter((r: any) => !LEND_SANCTIONED.includes(r.stage));
  const pipeSyn = syn.filter((r: any) => !SYN_TERM.includes(r.status) && !SYN_CLOSED.includes(r.status));
  const pipeAm = am.filter((a: any) => !['Closed', 'Dropped'].includes(a.status));

  const sum = (a: any[], k = 'amt') => a.reduce((s, x) => s + num(x[k]), 0);
  const closedAmt = sum(closedLend) + sum(closedSyn) + sum(closedAm, 'val');
  const pipeAmt = sum(pipeLend) + sum(pipeSyn) + sum(pipeAm, 'val');
  const closedN = closedLend.length + closedSyn.length + closedAm.length;
  const pipeN = pipeLend.length + pipeSyn.length + pipeAm.length;

  const lenderSet = new Set<string>();
  syn.forEach((r: any) => (r.lenders || []).forEach((l: any) => { if (l.name) lenderSet.add(l.name); }));

  const hero = {
    closedAmt, closedN, pipeAmt, pipeN,
    conv: (closedN + pipeN) ? Math.round((closedN / (closedN + pipeN)) * 100) : 0,
    // Registry rows only — hydration seeds _shadow name-lookup entries for tracker
    // codes, which are not clients.
    // ...nor are the <code>-2 facility aliases hydration adds for a company's
    // second deal — same company, one client (B06).
    clients: Object.values(D.clients || {}).filter((c: any) => !c?._shadow && !c?.aliasOf).length,
    lenders: lenderSet.size,
    liveMandates: pipeSyn.length,
  };

  // ---- count-bar helper (v16 _cnt/_bars): tally rows across buckets by a key ----
  const cntBars = (arrs: any[][], keyFn: (r: any) => string): BarRow[] => {
    const m: Record<string, number> = {};
    arrs.forEach((a) => a.forEach((r) => { const k = keyFn(r) || 'Other'; m[k] = (m[k] || 0) + 1; }));
    return Object.entries(m).map(([label, value]) => ({ label, value, note: value === 1 ? 'deal' : 'deals' }))
      .sort((a, b) => b.value - a.value).slice(0, 10);
  };
  const secOf = (r: any) => cli(r.code).sector || 'Other';
  const geoOf = (r: any) => cli(r.code).state || r.state || '(unspecified)';

  // ---- sector split: active pipeline + closed ----
  const sector = cntBars([liveLend, pipeSyn, pipeAm], secOf);
  const sectorClosed = cntBars([closedLend, closedSyn, closedAm], secOf);

  // ---- geography split: active pipeline + closed (counts) ----
  const geoPipe = cntBars([liveLend, pipeSyn, pipeAm], geoOf);
  const geoClosed = cntBars([closedLend, closedSyn, closedAm], geoOf);

  // ---- Mitigation & Adaptation breakup (all pipe + closed rows) ----
  const maMap = { Mitigation: 0, Adaptation: 0 };
  [...liveLend, ...pipeSyn, ...pipeAm, ...closedLend, ...closedSyn, ...closedAm]
    .forEach((r: any) => { maMap[cli(r.code).lens === 'Adaptation' ? 'Adaptation' : 'Mitigation']++; });
  const ma: BarRow[] = [
    { label: 'Mitigation', value: maMap.Mitigation, note: maMap.Mitigation === 1 ? 'deal' : 'deals' },
    { label: 'Adaptation', value: maMap.Adaptation, note: maMap.Adaptation === 1 ? 'deal' : 'deals' },
  ];

  // ---- sanctioned lending: sanctioned, not yet disbursed ----
  const sancRows = lending.filter((r: any) => LEND_SANCTIONED.includes(r.stage));

  // ---- lending funnel: most advanced stage reached ----
  const LORDER: string[] = (D.ref && D.ref['Lending Stage']) || [];
  const lfMap: Record<string, FunnelRow> = {};
  lending.forEach((r: any) => {
    let best = r.stage, bi = LORDER.indexOf(r.stage);
    (r.h || []).forEach((x: any) => { const i = LORDER.indexOf(x.stage); if (i > bi) { bi = i; best = x.stage; } });
    lfMap[best] = lfMap[best] || { status: best, rows: 0, amt: 0 };
    lfMap[best].rows++; lfMap[best].amt += num(r.amt);
  });
  const lendingFunnel = LORDER.filter((s) => lfMap[s]).map((s) => lfMap[s]);

  // ---- business line composition by climate lens ----
  const lensMap: Record<string, LensRow> = {};
  const bumpLens = (lens: string, line: 'lending' | 'syn' | 'am') => {
    const k = lens || '(unset)';
    lensMap[k] = lensMap[k] || { lens: k, lending: 0, syn: 0, am: 0, total: 0 };
    lensMap[k][line]++; lensMap[k].total++;
  };
  liveLend.forEach((r: any) => bumpLens(cli(r.code).lens, 'lending'));
  pipeSyn.forEach((r: any) => bumpLens(cli(r.code).lens, 'syn'));
  pipeAm.forEach((r: any) => bumpLens(cli(r.code).lens, 'am'));
  const lens = Object.values(lensMap).sort((a, b) => b.total - a.total);

  // ---- geography rollup ----
  const geo: Record<string, GeoRow> = {};
  const bumpGeo = (state: string, bucket: 'pipe' | 'closed', amt: number) => {
    const st = state || '(unspecified)';
    geo[st] = geo[st] || { state: st, pipeCount: 0, pipeAmt: 0, closedCount: 0, closedAmt: 0 };
    if (bucket === 'pipe') { geo[st].pipeCount++; geo[st].pipeAmt += amt; } else { geo[st].closedCount++; geo[st].closedAmt += amt; }
  };
  lending.forEach((r: any) => { const st = cli(r.code).state; if (r.stage === 'Disbursed') bumpGeo(st, 'closed', num(r.amt)); else if (!['Dropped', 'Rejected'].includes(r.stage)) bumpGeo(st, 'pipe', num(r.amt)); });
  syn.forEach((r: any) => { const st = cli(r.code).state; if (SYN_CLOSED.includes(r.status)) bumpGeo(st, 'closed', num(r.amt)); else if (!SYN_TERM.includes(r.status)) bumpGeo(st, 'pipe', num(r.amt)); });
  am.forEach((a: any) => { const st = cli(a.code).state || a.state; if (a.status === 'Closed') bumpGeo(st, 'closed', num(a.val)); else if (a.status !== 'Dropped') bumpGeo(st, 'pipe', num(a.val)); });
  const geography = Object.values(geo).sort((a, b) => (b.pipeAmt + b.closedAmt) - (a.pipeAmt + a.closedAmt)).slice(0, 15);

  // ---- syndication funnel: most advanced stage reached ----
  const order = ['Disbursed', 'Sanctioned', 'IP Received', 'Queries Received', 'IM Circulated', 'IM in Prep', 'Docs Pending', 'Deal Sourced', 'On Hold', 'Withdrawn', 'Dropped'];
  const fmap: Record<string, FunnelRow> = {};
  order.forEach((s) => { fmap[s] = { status: s, rows: 0, amt: 0 }; });
  syn.forEach((r: any) => { const s = r.status || '(unspecified)'; fmap[s] = fmap[s] || { status: s, rows: 0, amt: 0 }; fmap[s].rows++; fmap[s].amt += num(r.amt); });
  const funnel = order.filter((k) => fmap[k]).map((k) => fmap[k]).concat(Object.values(fmap).filter((x) => !order.includes(x.status)));

  // ---- deal velocity ----
  // Order + set mirrors v16 velocityRows (IM → First response is a placeholder row).
  const trans: Record<string, number[]> = { 'Lead → Deal': [], 'Deal → CAM/IM': [], 'IM → First response': [], 'IM → Sanction': [], 'Sanction → Documentation': [], 'Documentation → Disbursement': [] };
  lending.forEach((r: any) => {
    const h = r.h || [];
    const find = (needles: string[]) => h.find((x: any) => needles.some((s) => String(x.stage || '').includes(s)));
    const g = (a: string[], b: string[], label: string) => { const A = find(a), B = find(b); if (A && B) { const dd = (new Date(B.t).getTime() - new Date(A.t).getTime()) / 864e5; if (dd >= 0) trans[label].push(dd); } };
    g(['Diligence'], ['Note'], 'Deal → CAM/IM');
    g(['Note'], ['Sanction'], 'IM → Sanction');
    g(['Sanction'], ['Documentation'], 'Sanction → Documentation');
    g(['Documentation'], ['Disbursed'], 'Documentation → Disbursement');
  });
  leads.filter((l: any) => l.status === 'Converted' && l.createdAt).forEach((l: any) => {
    const d = deals.find((x: any) => x.code === l.conv);
    if (d && d.createdAt) { const diff = (new Date(d.createdAt).getTime() - new Date(l.createdAt).getTime()) / 864e5; if (diff >= 0) trans['Lead → Deal'].push(diff); }
  });
  const velocity: VelRow[] = Object.entries(trans).map(([stage, v]) => ({ stage, n: v.length, median: median(v), p75: p75(v) }));

  // ---- lead touches by recency window ----
  // From each lead's own last-touch date (the register rolls every logged
  // interaction onto it), not from an interactions list the book never loads —
  // that list held only what this tab logged, so the tile read zero (B09).
  const now = Date.now();
  const buckets: Record<string, Set<string>> = { '7': new Set(), '15': new Set(), '30': new Set(), '90': new Set() };
  leads.forEach((l: any) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(l.last || ''));
    if (!m) return;
    const days = (now - new Date(+m[1], +m[2] - 1, +m[3]).getTime()) / 864e5;
    (['7', '15', '30', '90'] as const).forEach((w) => { if (days <= +w) buckets[w].add(l.id); });
  });
  const leadTouch: BarRow[] = [
    { label: 'Last 7d', value: buckets['7'].size, note: 'leads touched' },
    { label: 'Last 15d', value: buckets['15'].size, note: 'leads touched' },
    { label: 'Last 30d', value: buckets['30'].size, note: 'leads touched' },
    { label: 'Last 90d', value: buckets['90'].size, note: 'leads touched' },
  ];

  // ---- lead sourcing ----
  const tax: string[] = D.sourceTypes || (D.ref && D.ref.Source) || ['RM', 'DSA', 'Inbound', 'Referral', 'Event', 'Other'];
  const src: Record<string, number> = {};
  tax.forEach((t) => { src[t] = 0; });
  leads.forEach((l: any) => { const raw = l.source || ''; const k = tax.includes(raw) ? raw : (raw ? 'Other' : '(unspecified)'); src[k] = (src[k] || 0) + 1; });
  const known = Object.entries(src).filter(([k]) => tax.includes(k));
  const unspecified = Object.entries(src).filter(([k]) => !tax.includes(k));
  const sourcing: BarRow[] = [...known.sort((a, b) => tax.indexOf(a[0]) - tax.indexOf(b[0])), ...unspecified].map(([label, value]) => ({ label, value }));

  // ---- RM origination ----
  // One row per roster RM, matched by handle OR full name, then a row for every
  // spelling the roster does not know ("chetan", a departed RM) and one for
  // leads with no RM at all — nothing is dropped from the table (B44).
  const rmNames: string[] = (D.ref && D.ref.RM) || [];
  const rmKey = (v: any): string => {
    const n = norm(v);
    if (!n) return '(unassigned)';
    const h = resolvePerson(v, idx);
    return h && rmNames.includes(h) ? h : `${String(v).trim()} (not on roster)`;
  };
  const rmKeys = [...rmNames, ...new Set([...leads.map((l: any) => rmKey(l.rm)), ...deals.map((d: any) => rmKey(d.rm))].filter((k) => !rmNames.includes(k)))];
  // Off-roster keys exist only because some scoped row carries them, so they
  // always have a count; roster rows stay even at zero, as they always did.
  const rm: RmRow[] = rmKeys.filter((n) => !person || n === who || !rmNames.includes(n)).map((name) => {
    const isRm = (v: any) => rmKey(v) === name;
    const al = leads.filter((l: any) => l.status === 'Active' && isRm(l.rm));
    const dl = deals.filter((d: any) => isRm(d.rm));
    return {
      name: rmNames.includes(name) ? (referenceService.getRefLabels('RM')[name] || name) : name,
      sourced: leads.filter((l: any) => isRm(l.rm)).length,
      activeLeads: al.length, hotLeads: al.filter((l: any) => l.temp === 'Hot').length,
      converted: leads.filter((l: any) => isRm(l.rm) && l.status === 'Converted').length,
      pipeline: dl.filter((d: any) => d.lend || d.syn || d.am).length,
      closed: closedLend.filter((r: any) => isRm(r.rm)).length + closedSyn.filter((r: any) => isRm(r.rm)).length + closedAm.filter((r: any) => isRm(r.rm)).length,
    };
  });

  // ---- Analyst throughput ----
  const anNames: string[] = ((D.ref && D.ref.Analyst) || []).filter((a: string) => a !== 'Grishma');
  const analyst: AnRow[] = anNames.filter((n) => !person || n === who).map((name) => {
    const isAn = (v: any) => resolvePerson(v, idx) === name || norm(v) === norm(name);
    return {
    name: referenceService.getRefLabels('Analyst')[name] || name,
    activeLend: lending.filter((r: any) => isAn(r.an) && !['Disbursed', 'Rejected', 'Dropped'].includes(r.stage)).length,
    activeSyn: syn.filter((r: any) => isAn(r.an) && !SYN_TERM.includes(r.status) && !SYN_CLOSED.includes(r.status)).length,
    activeAM: am.filter((a: any) => isAn(a.an) && !['Closed', 'Dropped'].includes(a.status)).length,
    closed: lending.filter((r: any) => isAn(r.an) && r.stage === 'Disbursed').length + syn.filter((r: any) => isAn(r.an) && SYN_CLOSED.includes(r.status)).length + am.filter((a: any) => isAn(a.an) && a.status === 'Closed').length,
    rejected: lending.filter((r: any) => isAn(r.an) && ['Rejected', 'Dropped'].includes(r.stage)).length + syn.filter((r: any) => isAn(r.an) && SYN_TERM.includes(r.status)).length,
  }; });

  // ---- bank engagement ----
  const banks: Record<string, BankRow> = {};
  syn.forEach((r: any) => (r.lenders || []).forEach((l: any) => {
    if (l.ex || !l.name) return;
    const b = banks[l.name] = banks[l.name] || { name: l.name, pursued: 0, live: 0, sanc: 0, decl: 0 };
    b.pursued++;
    if (l.st === 'Sanctioned') b.sanc++;
    else if (l.st === 'Declined') b.decl++;
    else if (l.st) b.live++;
  }));
  const bank = Object.values(banks).sort((a, b) => b.pursued - a.pursued).slice(0, 15);

  return {
    hero,
    closed: { lend: closedLend.map((r: any) => drill(r)), syn: closedSyn.map((r: any) => drill(r)), am: closedAm.map((r: any) => drill(r, 'val')) },
    sanctioned: sancRows.map((r: any) => drill(r)),
    pipe: { lend: pipeLend.map((r: any) => drill(r)), syn: pipeSyn.map((r: any) => drill(r)), am: pipeAm.map((r: any) => drill(r, 'val')) },
    sector, sectorClosed, ma, lens, geography, geoClosed, geoPipe, funnel, lendingFunnel,
    velocity, leadTouch, sourcing, rm, analyst, bank,
  };
}
