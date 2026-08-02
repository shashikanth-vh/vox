import { db } from '../api/atlasStore';
import { writeAudit } from './auditService';

export interface DocEntry { name: string; size: number; type: string; when: string; by: string; label: string; data?: string; inline?: boolean; }

// The required-document checklist, per section (ports v11's REQ_DOCS).
export const REQ_DOCS: { k: string; t: string; d: { k: string; n: string; req: number }[] }[] = [
  { k: 'kyc', t: 'KYC & Constitutional', d: [
    { k: 'coi', n: 'Certificate of Incorporation', req: 1 }, { k: 'moa', n: 'MOA & AOA', req: 1 },
    { k: 'pan', n: 'Company PAN', req: 1 }, { k: 'gst', n: 'GST Registration', req: 1 },
    { k: 'shp', n: 'Shareholding pattern', req: 1 }, { k: 'dirkyc', n: 'Promoter / Director KYC', req: 1 }] },
  { k: 'fin', t: 'Financials', d: [
    { k: 'af3', n: 'Audited financials — last 3 FYs', req: 1 }, { k: 'prov', n: 'Provisional financials — current FY', req: 1 },
    { k: 'itr', n: 'ITR acknowledgements — 3 years', req: 1 }, { k: 'cma', n: 'Projections / CMA data', req: 1 }] },
  { k: 'bank', t: 'Banking & Debt', d: [
    { k: 'bs12', n: 'Bank statements — 12 months', req: 1 }, { k: 'sanc', n: 'Existing loan sanction letters', req: 1 },
    { k: 'soa', n: 'Loan outstanding / SOA', req: 1 }, { k: 'rtr', n: 'Repayment track record', req: 0 }] },
  { k: 'comp', t: 'Compliance & Bureau', d: [
    { k: 'gstr', n: 'GST returns — 12 months', req: 1 }, { k: 'cibc', n: 'CIBIL consent letter', req: 1 },
    { k: 'stat', n: 'Statutory dues confirmation', req: 0 }] },
  { k: 'proj', t: 'Project & Technical', d: [
    { k: 'dpr', n: 'Project report / DPR', req: 1 }, { k: 'ppa', n: 'PPA / offtake agreement', req: 0 },
    { k: 'land', n: 'Land documents', req: 0 }, { k: 'env', n: 'Environmental clearances', req: 0 }] },
  { k: 'deal', t: 'Deal Documents', d: [
    { k: 'mand', n: 'Mandate letter', req: 1 }, { k: 'im', n: 'Information Memorandum', req: 0 }, { k: 'ts', n: 'Term sheet', req: 0 }] },
];

const INLINE_MAX = 400 * 1024; // keep bytes offline up to this size

function store(code: string): Record<string, Record<string, DocEntry>> {
  const d: any = db(); if (!d.docs) d.docs = {}; if (!d.docs[code]) d.docs[code] = {}; return d.docs[code];
}

export const documentsService = {
  maxInline: INLINE_MAX,
  section(code: string, sec: string): Record<string, DocEntry> { return store(code)[sec] || {}; },
  get(code: string, sec: string, key: string): DocEntry | undefined { return store(code)[sec]?.[key]; },
  put(code: string, sec: string, key: string, entry: DocEntry, by: string, sectionTitle: string) {
    const s = store(code); s[sec] = s[sec] || {}; s[sec][key] = entry;
    writeAudit(by, 'Document uploaded', code, `${sectionTitle} · ${entry.label} (${entry.name})`);
  },
  remove(code: string, sec: string, key: string, by: string, sectionTitle: string) {
    const s = store(code); if (s[sec]?.[key]) { const e = s[sec][key]; delete s[sec][key]; writeAudit(by, 'Document removed', code, `${sectionTitle} · ${e.label} (${e.name})`); }
  },
};
