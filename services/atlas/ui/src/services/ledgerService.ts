import axiosClient from '../api/axiosClient';

// The desk's Excel ledger, in and out of PRISM (Admin-only on the server too:
// both routes sit behind the backup/restore operation gate).
//
//   import — POST /v1/import/atlas-xlsx: reads BOTH generations of the workbook
//            (the old consolidated MIS and the live "Evam Dashboard" ledger) and
//            reports exactly what happened: quarantined rows, vocabulary
//            translations, derivations, reconciliation items.
//   export — GET /v1/export/ledger-xlsx: the whole register as a Dashboard-shaped
//            workbook that re-imports with nothing lost (the round trip is the
//            contract, covered by server tests).

export interface LedgerBook {
  entities: number;
  leads: { total: number; by_status: Record<string, number> };
  deals: { total: number; lending: number; syndication: number; asset_mon: number };
  lending: { lines: number; amount_cr: number; by_stage: Record<string, number> };
  syndication: {
    trackers: number; ask_cr: number; partnership_trackers: number;
    lenders: number; allocation_cr: number;
    lenders_by_status: Record<string, number>; mandate_statuses: number;
  };
  asset_monetisation: { mandates: number; indicative_cr: number; size_mw: number };
  counterparties: { total: number; active: number };
}

export interface LedgerImportResult {
  counts: Record<string, number>;
  /** What the whole book holds AFTER this import — desk-language totals. */
  book?: LedgerBook;
  report: {
    quarantined: any[]; quarantined_count: number;
    reconciliation: any[]; reconciliation_count: number;
    translated: any[]; translated_count: number;
    derived: any[]; derived_count: number;
  };
  filename?: string;
  checksum?: string;
  mode?: string;
}

export const ledgerService = {
  async importLedger(file: File, mode: 'merge' | 'replace', reason: string,
                     retainIncomplete: boolean): Promise<LedgerImportResult> {
    const form = new FormData();
    form.append('file', file);
    const res = await axiosClient.post('/import/atlas-xlsx', form, {
      params: { mode, reason, retain_incomplete: retainIncomplete },
    });
    return res.data as LedgerImportResult;
  },

  async exportLedger(): Promise<void> {
    const res = await axiosClient.get('/export/ledger-xlsx', { responseType: 'blob' });
    const dispo = String(res.headers?.['content-disposition'] || '');
    const name = /filename="?([^";]+)"?/.exec(dispo)?.[1] || 'prism-ledger.xlsx';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(res.data as Blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  },
};
