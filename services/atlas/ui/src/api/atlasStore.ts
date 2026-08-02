import seed from './mockData/atlasData.json';

// Requirement 9: all data is served from here (as if from the API). When the
// real backend is ready, each service swaps its store call for an axios call.
export interface AtlasData {
  asOf: string;
  clients: Record<string, any>;
  leads: any[];
  deals: any[];
  lending: any[];
  syn: any[];
  am: any[];
  lenders: any[];
  ref: Record<string, string[]>;
  people: any[];
  lenders37: string[];
  matrix: Record<string, any>;
  audit: any[];
  interactions: any[];
  th: Record<string, number>;
  core33: any[];
  adapt10: any[];
  [k: string]: any;
}

// Deep clone so edits during a session don't mutate the imported module.
const store: AtlasData = JSON.parse(JSON.stringify(seed));

// Lender column order for the syndication matrix (seeded from the FI list).
if (!store.lenderOrder) store.lenderOrder = (store.lenders37 || []).slice();

// v11's DATA object seeds these at runtime (its seed()), not in the JSON — mirror that
// so the Audit trail / Activity Log have their opening entry and every write can log.
if (!store.audit) store.audit = [{
  t: new Date().toISOString().slice(0, 16).replace('T', ' '), by: 'System', act: 'Seeded', code: '',
  detail: 'Register seeded from ATLAS v11 (full live register)',
}];
if (!store.notes) store.notes = {};

// Demo follow-ups so Today's "Follow-ups due" has content on first load (v16-1 relies
// on logged interactions carrying a due date; the seed ships none). Dates are relative
// to today, so the items are always current/overdue.
if (!store.interactions || store.interactions.length === 0) {
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  const daysAgo = (n: number) => { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); };
  const mk = (refId: string, refType: string, interactionType: string, notes: string, nextAction: string, dueAgo: number) => ({
    interactionId: 'INT-SEED-' + refId, refId, refType,
    occurredAt: daysAgo(dueAgo + 3), loggedAt: new Date().toISOString(),
    person: 'Shubh', interactionType, direction: null, lenderName: null,
    notes, nextAction, nextActionDate: daysAgo(dueAgo),
  });
  store.interactions = [
    mk('AESPL', 'Platform Deals', 'Phone Call', 'Discussed revised pricing; lender to revert.', 'Send revised term sheet', 0),
    mk('AADHYA', 'Lending', 'Email / Written Correspondence', 'Sanction letter pending signature.', 'Chase sanction letter', 3),
    mk('LD-002', 'Lead', 'Virtual Meeting / Video Call', 'Walked EcoSoch through the proposal.', 'Call back on their decision', 1),
  ];
}

export function db(): AtlasData { return store; }
export const today = () => new Date().toISOString().slice(0, 10);
export const nowStamp = () => new Date().toISOString().slice(0, 16).replace('T', ' ');
