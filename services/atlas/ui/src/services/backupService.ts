import { db, today } from '../api/atlasStore';
import { writeAudit } from './auditService';

// Port of v12 AUGMENT 17 — full-state JSON out / in. Both paths are audited.
export const backupService = {
  backup(by: string) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(db())], { type: 'application/json' }));
    a.download = 'ATLAS_backup_' + today() + '.json';
    document.body.appendChild(a); a.click(); a.remove();
    writeAudit(by, 'Exported', '', 'full state backup');
  },

  // Replaces the store in place — same reason as v12's delete/assign dance: every
  // service holds the object returned by db(), so the identity has to survive.
  restore(text: string, by: string): { ok: boolean; error?: string } {
    let d: any;
    try { d = JSON.parse(text); } catch { return { ok: false, error: 'That file is not a valid ATLAS backup' }; }
    if (!d || typeof d !== 'object' || !d.clients || !d.deals)
      return { ok: false, error: 'That file is not a valid ATLAS backup' };
    const store: any = db();
    Object.keys(store).forEach((k) => { delete store[k]; });
    Object.assign(store, d);
    writeAudit(by, 'Imported', '', 'state restored from backup file');
    return { ok: true };
  },
};
