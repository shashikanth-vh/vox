import { db, nowStamp } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, asRows } from '../api/http';
import { emitSave } from '../utils/saveIndicator';
import type { TableQuery } from './types';

export function writeAudit(by: string, act: string, code: string, detail: string) {
  db().audit.unshift({ t: nowStamp(), by, act, code, detail });
  if (db().audit.length > 800) db().audit.pop();
  emitSave(act + (code ? ' · ' + code : ''));
}

/**
 * How many audit rows one GET /v1/audit asks for. Unlike /v1/leads and /v1/deals, this
 * endpoint answers with a BARE ARRAY — no total and no next_cursor — so there is no
 * cursor to follow and the trail is fetched in one capped batch and paged client-side.
 * 200 is the value the collection uses.
 */
export const AUDIT_LIMIT = 200;

/** An API audit entry read back as the row the trail renders. */
export function toAuditRow(r: any): { t: string; by: string; role?: string; act: string; code: string; detail: string } {
  // The trail renders `t` verbatim, and the local store writes "YYYY-MM-DD HH:MM".
  const t = String(r?.created_at || r?.at || r?.timestamp || '').replace('T', ' ').slice(0, 16);
  // `detail` may arrive as a changes object rather than a sentence — render it readably
  // rather than letting it stringify to [object Object] in the Detail column.
  const raw = r?.detail ?? r?.summary ?? r?.message ?? r?.changes;
  const detail = raw == null ? ''
    : typeof raw === 'string' ? raw
      : Object.entries(raw).map(([k, v]) => `${k} → ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`).join('; ');
  return {
    t,
    by: r?.actor_name || r?.actor || r?.actor_email || r?.user || '',
    role: r?.role,
    act: r?.action || r?.event || '',
    // The Code column wants something human; resource_id is a UUID, so it is the last
    // resort rather than the first choice.
    code: r?.resource_no || r?.code || r?.resource_type || '',
    detail,
  };
}

export const auditService = {
  async list(q: TableQuery) {
    return withFallback(
      async () => {
        const data = await api.get<any>('/audit', { limit: AUDIT_LIMIT });
        const raw = asRows(data, 'audit');
        // No silent truncation — a full batch means older entries are not shown.
        if (raw.length >= AUDIT_LIMIT) console.warn('[audit] /audit returned the %s-row cap — older entries are not listed.', AUDIT_LIMIT);
        // Paged client-side: with no total and no cursor, the batch IS the dataset.
        return applyQuery(raw.map(toAuditRow), { ...q, searchFields: ['act', 'code', 'detail', 'by'] });
      },
      async () => {
        await delay();
        return applyQuery(db().audit, { ...q, searchFields: ['act', 'code', 'detail', 'by'] });
      },
    );
  },
};
