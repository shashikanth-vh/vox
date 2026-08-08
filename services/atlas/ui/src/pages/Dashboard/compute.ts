// Dashboard computation — ports the ATLAS v11 template dashboard (vDashV4 + hero)
// onto the live store. All figures derive from db(); pass a `person` to scope every
// section to that RM/Analyst's book (mirrors the template's dashScopeS).
import { db } from '../../api/atlasStore';
import { num } from '../../utils/format';
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

export function computeDashboard(person?: string): DashboardV11 {
  const D = db();
  const cli = (code: string) => (D.clients && D.clients[code]) || { name: code, sector: 'Other', lens: '', state: '' };
  const mine = (r: any) => !person || r.rm === person || r.an === person;

  const leads = (D.leads || []).filter(mine);
  const deals = (D.deals || []).filter(mine);
  const lending = (D.lending || []).filter(mine);
  const syn = (D.syn || []).filter(mine);
  const am = (D.am || []).filter(mine);

  const drill = (r: any, valKey = 'amt'): DrillRow => ({ code: r.code, name: cli(r.code).name, sector: cli(r.code).sector || 'Other', amt: num(r[valKey]) });

  // ---- closed / pipeline buckets ----
  const closedLend = lending.filter((r: any) => r.stage === 'Disbursed');
  const closedSyn = syn.filter((r: any) => SYN_CLOSED.includes(r.status));
  const closedAm = am.filter((a: any) => a.status === 'Closed');
  const pipeLend = lending.filter((r: any) => !['Disbursed', 'Dropped', 'Rejected'].includes(r.stage));
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
    clients: Object.values(D.clients || {}).filter((c: any) => !c?._shadow).length,
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
  const sector = cntBars([pipeLend, pipeSyn, pipeAm], secOf);
  const sectorClosed = cntBars([closedLend, closedSyn, closedAm], secOf);

  // ---- geography split: active pipeline + closed (counts) ----
  const geoPipe = cntBars([pipeLend, pipeSyn, pipeAm], geoOf);
  const geoClosed = cntBars([closedLend, closedSyn, closedAm], geoOf);

  // ---- Mitigation & Adaptation breakup (all pipe + closed rows) ----
  const maMap = { Mitigation: 0, Adaptation: 0 };
  [...pipeLend, ...pipeSyn, ...pipeAm, ...closedLend, ...closedSyn, ...closedAm]
    .forEach((r: any) => { maMap[cli(r.code).lens === 'Adaptation' ? 'Adaptation' : 'Mitigation']++; });
  const ma: BarRow[] = [
    { label: 'Mitigation', value: maMap.Mitigation, note: maMap.Mitigation === 1 ? 'deal' : 'deals' },
    { label: 'Adaptation', value: maMap.Adaptation, note: maMap.Adaptation === 1 ? 'deal' : 'deals' },
  ];

  // ---- sanctioned lending (Sanctioned / Documentation / Disbursed) ----
  const sancRows = lending.filter((r: any) => ['Sanctioned', 'Documentation', 'Disbursed'].includes(r.stage));

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
  pipeLend.forEach((r: any) => bumpLens(cli(r.code).lens, 'lending'));
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
  const leadIds = new Set(leads.map((l: any) => l.id));
  const now = Date.now();
  const buckets: Record<string, Set<string>> = { '7': new Set(), '15': new Set(), '30': new Set(), '90': new Set() };
  (D.interactions || []).forEach((i: any) => {
    const isLead = leadIds.has(i.refId) || i.refType === 'Lead' || !!i.portedFromLead;
    if (!isLead || !i.occurredAt) return;
    const days = (now - new Date(i.occurredAt).getTime()) / 864e5;
    const key = leadIds.has(i.refId) ? i.refId : (i.portedFromLead || i.refId);
    (['7', '15', '30', '90'] as const).forEach((w) => { if (days <= +w) buckets[w].add(key); });
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
  const rmNames: string[] = (D.ref && D.ref.RM) || [];
  const rm: RmRow[] = rmNames.filter((n) => !person || n === person).map((name) => {
    const al = leads.filter((l: any) => l.status === 'Active' && l.rm === name);
    const dl = deals.filter((d: any) => d.rm === name);
    return {
      name, sourced: leads.filter((l: any) => l.rm === name).length,
      activeLeads: al.length, hotLeads: al.filter((l: any) => l.temp === 'Hot').length,
      converted: leads.filter((l: any) => l.rm === name && l.status === 'Converted').length,
      pipeline: dl.filter((d: any) => d.lend || d.syn || d.am).length,
      closed: closedLend.filter((r: any) => r.rm === name).length + closedSyn.filter((r: any) => r.rm === name).length + closedAm.filter((r: any) => r.rm === name).length,
    };
  });

  // ---- Analyst throughput ----
  const anNames: string[] = ((D.ref && D.ref.Analyst) || []).filter((a: string) => a !== 'Grishma');
  const analyst: AnRow[] = anNames.filter((n) => !person || n === person).map((name) => ({
    name,
    activeLend: lending.filter((r: any) => r.an === name && !['Disbursed', 'Rejected', 'Dropped'].includes(r.stage)).length,
    activeSyn: syn.filter((r: any) => r.an === name && !SYN_TERM.includes(r.status) && !SYN_CLOSED.includes(r.status)).length,
    activeAM: am.filter((a: any) => a.an === name && !['Closed', 'Dropped'].includes(a.status)).length,
    closed: lending.filter((r: any) => r.an === name && r.stage === 'Disbursed').length + syn.filter((r: any) => r.an === name && SYN_CLOSED.includes(r.status)).length + am.filter((a: any) => a.an === name && a.status === 'Closed').length,
    rejected: lending.filter((r: any) => r.an === name && ['Rejected', 'Dropped'].includes(r.stage)).length + syn.filter((r: any) => r.an === name && SYN_TERM.includes(r.status)).length,
  }));

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
