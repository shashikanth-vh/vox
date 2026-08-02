import { db } from '../api/atlasStore';
import { delay } from '../api/queryEngine';
import { api, USE_REAL_API } from '../api/http';
import { num } from '../utils/format';
import { LEND_GREEN } from './lendingService';
import { SYN_TERM, SYN_CLOSED } from './syndicationService';
import { clientsService } from './clientsService';
import { daysSince } from '../utils/format';
import type { DashboardData } from '../pages/Dashboard/dashboard.types';

export const dashboardService = {
  async getDashboard(): Promise<DashboardData> {
    if (USE_REAL_API) {
      try { return await api.get<DashboardData>('/dashboard'); } catch (e) { console.warn('[api] dashboard fallback:', e); }
    }
    await delay();
    const D = db();
    const sum = (a: any[]) => a.reduce((x, r) => x + num(r.amt), 0);
    const ownSA = D.lending.filter((r: any) => LEND_GREEN.includes(r.stage));
    const ownPA = D.lending.filter((r: any) => ['Data Awaited', 'Diligence', 'Note Circulated'].includes(r.stage));
    const synClosed = D.syn.filter((r: any) => SYN_CLOSED.includes(r.status)).reduce((a: number, r: any) => a + num(r.amt), 0);
    const synEn = D.syn.filter((r: any) => !SYN_TERM.includes(r.status)).reduce((a: number, r: any) => a + num(r.amt), 0);
    const amLive = D.am.filter((r: any) => r.status !== 'Dropped').reduce((a: number, r: any) => a + num(r.val), 0);
    const ownS = sum(ownSA), ownP = sum(ownPA);

    // sector composition of syndication enabled book
    const bySector: Record<string, number> = {};
    D.syn.filter((r: any) => !SYN_TERM.includes(r.status)).forEach((r: any) => {
      const s = clientsService.get(r.code).sector || 'Other';
      bySector[s] = (bySector[s] || 0) + num(r.amt);
    });

    // simple attention list (stale leads + stuck lending + undisbursed)
    const T = D.th; const attention: any[] = [];
    D.leads.filter((l: any) => l.status === 'Active').forEach((l: any) => {
      const d = daysSince(l.last);
      if (d != null && d > T.staleLead) attention.push({ rule: 'Stale lead', sev: 'amber', company: l.company, owner: l.rm || '—', days: d, why: `no touch in ${d}d` });
    });
    D.lending.filter((r: any) => ['Data Awaited', 'Diligence', 'Note Circulated'].includes(r.stage)).forEach((r: any) => {
      const d = daysSince(r.updated);
      if (d != null && d > T.lendAmber) attention.push({ rule: 'Lending stuck', sev: d > T.lendRed ? 'red' : 'amber', company: clientsService.get(r.code).name, owner: r.an || r.rm || '—', days: d, why: `${r.stage} for ${d}d` });
    });
    D.lending.filter((r: any) => ['Sanctioned', 'Documentation'].includes(r.stage)).forEach((r: any) => {
      const d = daysSince(r.sanc);
      if (d != null && d > T.undisb) attention.push({ rule: 'Sanctioned, undisbursed', sev: 'red', company: clientsService.get(r.code).name, owner: r.an || '—', days: d, why: `sanctioned ${d}d ago` });
    });
    attention.sort((a, b) => (a.sev === b.sev ? (b.days || 0) - (a.days || 0) : a.sev === 'red' ? -1 : 1));

    return {
      tiles: { ownS, ownP, ownSAcount: ownSA.length, ownPAcount: ownPA.length, synClosed, synEn, amLive,
        multiplierDemo: synClosed / (ownS || 1), multiplierPipe: synEn / ((ownS + ownP) || 1) },
      sectorComposition: Object.entries(bySector).map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value).slice(0, 8),
      funnel: { activeLeads: D.leads.filter((l: any) => l.status === 'Active').length, deals: D.deals.length,
        lend: D.deals.filter((d: any) => d.lend).length, syn: D.deals.filter((d: any) => d.syn).length, am: D.deals.filter((d: any) => d.am).length },
      attention: attention.slice(0, 15),
    };
  },
};
