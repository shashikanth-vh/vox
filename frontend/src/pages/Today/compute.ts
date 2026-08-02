// Today worklist — ports the ATLAS v11 vToday(): stage bottlenecks (attention engine),
// contact staleness, follow-ups due, and the "park / get back later" list.
import { db, today } from '../../api/atlasStore';
import { daysSince } from '../../utils/format';
import { SYN_TERM, SYN_CLOSED } from '../../services/syndicationService';

export interface AttnRow { rule: string; sev: 'red' | 'amber'; code: string; owner: string; days: number | null; why: string; isLead?: string; }
export interface ContactRow { code: string; co: string; days: number; owner: string; sev: 'red' | 'amber'; }
export interface DueRow { id: string; name: string; code: string; nextAction: string; nextActionDate: string; }
export interface SnoozeRow { key: string; label: string; when: string; by: string; }

const cli = (code: string) => (db().clients && db().clients[code]) || { name: code };
const dealOf = (code: string): any => (db().deals || []).find((d: any) => d.code === code) || {};

export function interactionsFor(refId: string): any[] {
  return (db().interactions || []).filter((i: any) => i.refId === refId)
    .sort((a: any, b: any) => ((b.occurredAt || '') + (b.loggedAt || '')).localeCompare((a.occurredAt || '') + (a.loggedAt || '')));
}
export function lastContact(refId: string): string | null {
  const ints = interactionsFor(refId);
  return ints.length ? ints[0].occurredAt : null;
}

// The full BN-01…BN-08 attention engine from the template.
export function attention(): AttnRow[] {
  const T = db().th || {} as any;
  const out: AttnRow[] = [];
  const push = (rule: string, sev: 'red' | 'amber', code: string, owner: string, days: number | null, why: string, isLead?: string) =>
    out.push({ rule, sev, code, owner, days, why, isLead });

  (db().leads || []).filter((l: any) => l.status === 'Active').forEach((l: any) => {
    const d = daysSince(l.last);
    if (d != null && d > T.staleLead) push('BN-01 Stale lead', 'amber', l.id, l.rm || '—', d, `no touch in ${d}d`, l.company);
  });
  (db().lending || []).filter((r: any) => ['Data Awaited', 'Diligence', 'Note Circulated'].includes(r.stage)).forEach((r: any) => {
    const d = daysSince(r.updated);
    if (d != null && d > T.lendAmber) push('BN-02 Lending stuck', d > T.lendRed ? 'red' : 'amber', r.code, r.an || r.rm || '—', d, `${r.stage} for ${d}d`);
  });
  Object.entries(db().matrix || {}).forEach(([c, cells]: [string, any]) => Object.entries(cells).forEach(([l, o]: [string, any]) => {
    const d = daysSince(o.since);
    if (d == null) return;
    if (o.s === 1 && d > T.showcase) push('BN-03 Showcase pending', 'amber', c, dealOf(c).rm || '—', d, `${l} identified ${d}d ago, IM not sent`);
    if (o.s === 2 && d > T.lenderSilent) push('BN-04 Lender silent', 'amber', c, dealOf(c).rm || '—', d, `${l}: IM out ${d}d, no response`);
    if (o.s === 3 && d > T.ballUs) push('BN-05 Ball with us', 'red', c, dealOf(c).an || '—', d, `${l}: queries pending our reply ${d}d`);
  }));
  (db().lending || []).filter((r: any) => ['Sanctioned', 'Documentation'].includes(r.stage)).forEach((r: any) => {
    const d = daysSince(r.sanc);
    if (d != null && d > T.undisb) push('BN-06 Sanctioned, undisbursed', 'red', r.code, r.an || '—', d, `sanctioned ${d}d ago`);
  });
  const lit = new Set(Object.entries(db().matrix || {}).filter(([, v]: [string, any]) => Object.keys(v).length).map(([c]) => c));
  Array.from(new Set((db().syn || []).filter((r: any) => !SYN_TERM.includes(r.status) && !SYN_CLOSED.includes(r.status)).map((r: any) => r.code)))
    .forEach((c: any) => { if (!lit.has(c)) push('BN-07 Mandate, no outreach', 'amber', c, dealOf(c).rm || '—', null, 'live ask, zero lenders lit'); });
  const touched: Record<string, string> = {};
  (db().audit || []).forEach((a: any) => { if (a.code && !touched[a.code]) touched[a.code] = a.t; });
  [...(db().lending || []), ...(db().syn || [])].filter((r: any) => r.pendingWith).forEach((r: any) => {
    const t = touched[r.code]; const d = t ? daysSince(t.slice(0, 10)) : 99;
    if (d != null && d > (T.pendAge ?? 10)) push('BN-08 Pending-with ageing', 'amber', r.code, r.pendingWith, d, `ball with ${r.pendingWith}, quiet ${d}d`);
  });
  out.sort((a, b) => (a.sev === b.sev ? (b.days || 0) - (a.days || 0) : a.sev === 'red' ? -1 : 1));
  return out;
}

// ---- snooze / park (session-scoped in the store, like the template's S.snz) ----
function snz(): Record<string, { when: string; by: string; label: string }> {
  const d: any = db(); if (!d.snz) d.snz = {}; return d.snz;
}
export const isSnz = (key: string): boolean => !!snz()[key];
export function park(key: string, label: string, by: string) {
  snz()[key] = { when: new Date().toISOString().slice(0, 16).replace('T', ' '), by, label };
}
export function unpark(key: string) { delete snz()[key]; }

export interface TodayData {
  reds: number; ambers: number;
  due: DueRow[]; contactRed: ContactRow[]; contactAmber: ContactRow[];
  stageRed: AttnRow[]; stageAmber: AttnRow[]; snoozed: SnoozeRow[];
  nameOf: (code: string) => string;
}

export function computeToday(person?: string): TodayData {
  const T = db().th || {} as any;
  const mine = (owner: string) => !person || owner === person || owner === '—';

  // contact staleness from the interaction ledger
  const contactStale: ContactRow[] = [];
  (db().deals || []).forEach((d: any) => {
    if (person && d.rm !== person && d.an !== person) return;
    const lc = lastContact(d.code); if (!lc) return;
    const days = daysSince(lc); if (days == null || days < (T.contact ?? 7)) return;
    contactStale.push({ code: d.code, co: cli(d.code).name, days, owner: d.rm || '—', sev: days >= 14 ? 'red' : 'amber' });
  });

  // follow-ups due today or earlier
  const due: DueRow[] = (db().interactions || [])
    .filter((i: any) => i.nextActionDate && i.nextActionDate <= today() && !isSnz('int' + i.interactionId))
    .filter((i: any) => {
      if (!person) return true;
      const d = dealOf(i.refId); const l = (db().leads || []).find((x: any) => x.id === i.refId);
      return (d.rm || (l && l.rm)) === person;
    })
    .map((i: any) => {
      const d = dealOf(i.refId); const l = (db().leads || []).find((x: any) => x.id === i.refId);
      return { id: i.interactionId, code: d.code || '', name: d.code ? cli(d.code).name : (l ? l.company : i.refId), nextAction: i.nextAction || 'follow up', nextActionDate: i.nextActionDate };
    });

  // stage bottlenecks from the attention engine
  let items = attention().filter((a) => !isSnz(a.rule + a.code));
  if (person) items = items.filter((a) => mine(a.owner));

  const stageRed = items.filter((a) => a.sev === 'red');
  const stageAmber = items.filter((a) => a.sev !== 'red');
  const contactRed = contactStale.filter((a) => a.sev === 'red' && !isSnz('cs' + a.code));
  const contactAmber = contactStale.filter((a) => a.sev === 'amber' && !isSnz('cs' + a.code));

  const snoozed: SnoozeRow[] = Object.entries(snz()).map(([key, v]) => ({ key, label: v.label, when: v.when, by: v.by }));

  return {
    reds: stageRed.length + contactRed.length,
    ambers: stageAmber.length + contactAmber.length,
    due, contactRed, contactAmber, stageRed, stageAmber, snoozed,
    nameOf: (code: string) => cli(code).name,
  };
}
