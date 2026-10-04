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
  /** The company the row concerns + the same plain-English sentence the Activity
   *  log shows — resolved server-side so the two screens can never disagree. */
  company?: string;
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
  // The server's plain-English sentence (same renderer as the Activity log) wins as
  // the grid's Detail; the raw changes object still rides along for the dialog.
  const sentence = typeof r?.summary === 'string' ? r.summary : '';
  const raw = r?.detail ?? r?.message ?? r?.changes;
  const detail = sentence
    || (raw == null ? '' : typeof raw === 'string' ? raw : summary(raw, act));
  return {
    t,
    by: r?.actor_name || r?.actor || r?.actor_email || r?.user || '',
    role: r?.role,
    act,
    // The Code column wants something human; resource_id is a UUID, so it is the last
    // resort rather than the first choice.
    code: r?.resource_no || r?.code || r?.resource_type || '',
    detail,
    company: typeof r?.company === 'string' ? r.company : '',
    changes: (r?.changes && typeof r.changes === 'object' ? r.changes : undefined)
      ?? (raw && typeof raw === 'object' ? raw : undefined),
    resourceId: r?.resource_id || undefined,
    requestId: r?.request_id || undefined,
  };
}

export const auditService = {
  /** The recent trail of several records at once (an entity, its deal, its lines) —
   *  what the company drawer's "Recent audit" shows (B35). */
  async forRecords(ids: string[], limit = 8): Promise<AuditRow[]> {
    const clean = ids.filter(Boolean);
    if (!clean.length) return [];
    return withFallback<AuditRow[]>(
      async () => asRows(await api.get<any>('/audit', { resource_ids: clean.join(','), limit }), 'audit').map(toAuditRow),
      async () => (db().audit || []).filter((a: any) => clean.includes(a.resourceId)).slice(0, limit),
    );
  },
  async list(q: TableQuery) {
    return withFallback(
      async () => {
        // Paged at the register (B37): the grid's page size, an offset cursor, an
        // exact total — the trail is no longer cut at the first 200 rows.
        const size = Math.min(Math.max(1, q.pageSize || 25), 1000);
        const offset = q.cursor ? Math.max(0, parseInt(q.cursor, 10) || 0) : (q.pageIndex || 0) * size;
        const data = await api.get<any>('/audit', { limit: size, offset, with_total: 'true' });
        const rows = asRows(data, 'audit').map(toAuditRow);
        const total = Number(data?.total) || rows.length;
        const next = offset + rows.length;
        return { rows, total, nextCursor: next < total && rows.length === size ? String(next) : null };
      },
      async () => {
        await delay();
        return applyQuery(db().audit, { ...q, searchFields: ['act', 'code', 'detail', 'by'] });
      },
    );
  },
};
