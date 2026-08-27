import { db, nowStamp } from '../api/atlasStore';
import { localMinute, localDay } from '../api/time';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, asRows } from '../api/http';
import { emitSave } from '../utils/saveIndicator';
import { summary } from './auditDetail';
import type { TableQuery } from './types';

/** One row of the audit trail. `changes` is the register's raw JSON, kept for the dialog. */
export interface AuditRow {
  t: string; by: string; role?: string; act: string; code: string; detail: string;
  changes?: Record<string, any>; resourceId?: string; requestId?: string;
}

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
export function toAuditRow(r: any): AuditRow {
  // The trail renders `t` verbatim, and the local store writes "YYYY-MM-DD HH:MM".
  const t = localMinute(String(r?.created_at || r?.at || r?.timestamp || ''));
  const act = r?.action || r?.event || '';
  // `detail` may arrive as a structured `changes` object rather than a sentence. The
  // grid gets the one-line summary; the raw object rides along so the row dialog can
  // show every field, labelled, without a second fetch.
  const raw = r?.detail ?? r?.summary ?? r?.message ?? r?.changes;
  const detail = raw == null ? '' : typeof raw === 'string' ? raw : summary(raw, act);
  return {
    t,
    by: r?.actor_name || r?.actor || r?.actor_email || r?.user || '',
    role: r?.role,
    act,
    // The Code column wants something human; resource_id is a UUID, so it is the last
    // resort rather than the first choice.
    code: r?.resource_no || r?.code || r?.resource_type || '',
    detail,
    changes: raw && typeof raw === 'object' ? raw : undefined,
    resourceId: r?.resource_id || undefined,
    requestId: r?.request_id || undefined,
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
