export interface DashboardData {
  tiles: {
    ownS: number; ownP: number; ownSAcount: number; ownPAcount: number;
    synClosed: number; synEn: number; amLive: number;
    multiplierDemo: number; multiplierPipe: number;
  };
  sectorComposition: { name: string; value: number }[];
  funnel: { activeLeads: number; deals: number; lend: number; syn: number; am: number };
  attention: { rule: string; sev: 'red' | 'amber'; company: string; owner: string; days: number | null; why: string }[];
}
