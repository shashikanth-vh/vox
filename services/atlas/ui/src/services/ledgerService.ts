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

export interface LedgerImportResult {
  counts: Record<string, number>;
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
