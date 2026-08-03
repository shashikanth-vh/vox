import { db } from '../api/atlasStore';
import { api, errText, listAll, USE_REAL_API } from '../api/http';
import { writeAudit } from './auditService';

/**
 * The Data Register — the company's document file.
 *
 * This used to write to `db().docs` and an audit line and nothing else: no HTTP call of
 * any kind. The dialog looked and behaved exactly as it does now — ticks, progress bar,
 * "Replace" — while the register never received a single file, and anything over 400 KB
 * was not even stored locally, just named. Every document was lost on refresh.
 *
 * It is now the register's own document plane. Documents belong to the COMPANY (the
 * entity), which is what the drawer this dialog opens from is showing, so the subject is
 * the entity and the checklist's section/item become `section` and `slot_key` — the same
 * coordinates `GET /v1/entities/{id}/data-register` reports completeness against.
 */

export interface DocEntry {
  /** Register document id — present for anything actually on file. */
  id?: string;
  name: string; size: number; type: string; when: string; by: string; label: string;
  status?: string;
  verifiedBy?: string | null;
  /** Mock mode only: a data: URI kept in the session. */
  data?: string; inline?: boolean;
}

// The required-document checklist, per section (ports v11's REQ_DOCS). `k` is the
// register's `section`, and each item's `k` its `slot_key` — so what the UI calls a
// checklist row and what the register calls a document slot are the same thing.
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

const INLINE_MAX = 400 * 1024; // mock mode only: keep bytes in the session up to this size

/** section -> slot_key -> entry. The shape the dialog renders, from either source. */
export type DocIndex = Record<string, Record<string, DocEntry>>;

function mockStore(code: string): DocIndex {
  const d: any = db(); if (!d.docs) d.docs = {}; if (!d.docs[code]) d.docs[code] = {}; return d.docs[code];
}

/** A register document row as the dialog's entry. */
function toEntry(r: any): DocEntry {
  return {
    id: r.id,
    name: r.original_filename || r.title || 'document',
    size: Number(r.size_bytes) || 0,
    type: r.content_type || 'file',
    when: String(r.uploaded_at || r.created_at || '').slice(0, 16).replace('T', ' '),
    by: r.uploaded_by || r.created_by || '',
    label: r.title || '',
    status: r.status,
    verifiedBy: r.verified_by ?? null,
  };
}

export const documentsService = {
  maxInline: INLINE_MAX,

  /**
   * Everything on file for this company, indexed by section and slot.
   *
   * SUPERSEDED documents are skipped: a replaced file stays on the register (the chain is
   * the audit trail) but the checklist shows what is current, not the history.
   */
  async load(code: string, entityId?: string): Promise<DocIndex> {
    if (!USE_REAL_API || !entityId) return mockStore(code);
    const index: DocIndex = {};
    try {
      const rows = await listAll(`/entities/${entityId}/documents`, { key: 'documents' });
      rows.forEach((r: any) => {
        if (!r?.section || !r?.slot_key) return;
        if (String(r.status) === 'Superseded') return;
        (index[r.section] = index[r.section] || {})[r.slot_key] = toEntry(r);
      });
    } catch (e) {
      console.warn('[register] could not read the data register:', e);
    }
    return index;
  },

  /**
   * Put a file on the register against one checklist slot. Multipart, because the
   * register stores the BYTES — which is the whole point of the exercise.
   */
  async upload(args: {
    code: string; entityId?: string; section: string; slotKey: string;
    title: string; required: boolean; file: File; by: string; sectionTitle: string;
  }): Promise<{ ok: boolean; error?: string }> {
    const { code, entityId, section, slotKey, title, required, file, by, sectionTitle } = args;
    if (USE_REAL_API && entityId) {
      const form = new FormData();
      form.append('file', file);
      form.append('section', section);
      form.append('slot_key', slotKey);
      form.append('title', title);
      form.append('is_required', String(required));
      try {
        await api.post(`/entities/${entityId}/documents/upload`, form);
      } catch (e: any) {
        console.warn('[register] document upload refused:', e?.response?.data ?? e);
        return { ok: false, error: errText(e?.response?.data)
          || `The register refused the upload (HTTP ${e?.response?.status ?? '?'}).` };
      }
      writeAudit(by, 'Document uploaded', code, `${sectionTitle} · ${title} (${file.name})`);
      return { ok: true };
    }
    // Mock mode: the session store, exactly as the prototype behaved.
    const entry: DocEntry = { name: file.name, size: file.size, type: file.type || 'file',
      when: new Date().toISOString().slice(0, 16).replace('T', ' '), by, label: title };
    if (file.size <= INLINE_MAX) {
      entry.data = await new Promise<string>((res) => {
        const r = new FileReader();
        r.onload = () => res(String(r.result)); r.onerror = () => res(''); r.readAsDataURL(file);
      });
      entry.inline = !!entry.data;
    }
    const s = mockStore(code); (s[section] = s[section] || {})[slotKey] = entry;
    writeAudit(by, 'Document uploaded', code, `${sectionTitle} · ${title} (${file.name})`);
    return { ok: true };
  },

  /**
   * Verify a document — the checker half of the maker/checker pair.
   *
   * The register REFUSES a validation by the person who uploaded it. That refusal is
   * surfaced rather than hidden: four-eyes is the control, and a user who hits it should
   * be told what it is, not shown a button that quietly does nothing.
   */
  async validate(docId: string, note: string, by: string, code: string): Promise<{ ok: boolean; error?: string }> {
    if (!USE_REAL_API) return { ok: true };
    try {
      await api.post(`/documents/${docId}/validate`, note ? { note } : {});
    } catch (e: any) {
      return { ok: false, error: errText(e?.response?.data)
        || `The register refused the verification (HTTP ${e?.response?.status ?? '?'}).` };
    }
    writeAudit(by, 'Document verified', code, docId);
    return { ok: true };
  },

  /** The stored bytes, as a download. */
  async download(entry: DocEntry): Promise<void> {
    if (entry.data) {                       // mock mode
      const a = document.createElement('a');
      a.href = entry.data; a.download = entry.name;
      document.body.appendChild(a); a.click(); a.remove();
      return;
    }
    if (!entry.id) return;
    const { default: axiosClient } = await import('../api/axiosClient');
    const res = await axiosClient.get(`/documents/${entry.id}/content`, { responseType: 'blob' });
    const url = URL.createObjectURL(res.data as Blob);
    const a = document.createElement('a');
    a.href = url; a.download = entry.name;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  },

  async remove(code: string, section: string, slotKey: string, entry: DocEntry,
               by: string, sectionTitle: string): Promise<{ ok: boolean; error?: string }> {
    if (USE_REAL_API && entry.id) {
      try {
        await api.del(`/documents/${entry.id}`);
      } catch (e: any) {
        return { ok: false, error: errText(e?.response?.data)
          || `The register refused the removal (HTTP ${e?.response?.status ?? '?'}).` };
      }
    } else {
      const s = mockStore(code);
      if (s[section]?.[slotKey]) delete s[section][slotKey];
    }
    writeAudit(by, 'Document removed', code, `${sectionTitle} · ${entry.label} (${entry.name})`);
    return { ok: true };
  },
};
