import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, remote, toCursorParams, asRows, nextCursorOf, totalOf } from '../api/http';
import { writeAudit } from './auditService';
import { clientsService } from './clientsService';
import type { TableQuery, Paged } from './types';
import type { LendingRow } from '../pages/Lending/lending.types';
import { inScope, type RowScope } from '../auth/rbac';

export const LEND_GREEN = ['Sanctioned', 'Documentation', 'Disbursed'];

/**
 * An API lending line read back as the row the grid renders. The wire is snake_case
 * (amount_cr, analyst, pending_with); unmapped, every column but the id renders blank.
 *
 * `id` here IS the API's UUID — unlike leads and deals, the grid never shows it, and
 * PATCH /v1/lending/{id} addresses it, so there is no second identifier to keep.
 */
export function toLendingRow(r: any): LendingRow {
  return {
    id: r?.id || '',
    dealId: r?.deal_id,
    // The "Group Code" column: the deal's human number, not a UUID.
    code: r?.deal_no || r?.code || '',
    _name: r?.company || r?.entity_name || r?.display_name || '',
    amt: Number(r?.amount_cr) || 0,
    rm: r?.rm || '',
    an: r?.analyst || '',
    stage: r?.stage || '',
    updated: (r?.stage_updated_at || r?.updated_at || '').slice(0, 10),
    sanc: r?.sanctioned_at ? String(r.sanctioned_at).slice(0, 10) : null,
    pendingWith: r?.pending_with || '',
    createdAt: (r?.created_at || '').slice(0, 10),
    remarks: r?.remarks || '',
    proposedAmt: Number(r?.proposed_disbursement_amount) || 0,
    proposedDate: r?.proposed_disbursement_date || null,
  };
}

export const lendingService = {
  async list(q: TableQuery, scope?: RowScope | null) {
    return withFallback<Paged<LendingRow>>(
      async () => {
        // Server-paged like /v1/leads and /v1/deals: `limit` is the table's page size and
        // Next carries the previous page's cursor, so one page comes back and applyQuery
        // must NOT re-slice it. The collection proves `limit` (and `deal_id`) here; the
        // cursor and `q` are only sent once the user pages or searches, so the first
        // load is exactly the request the collection makes.
        const data = await api.get<any>('/lending', toCursorParams(q));
        const rows = asRows(data, 'lending').map(toLendingRow).filter((r) => inScope(scope ?? null, r));
        return { rows, total: totalOf(data, rows.length), nextCursor: nextCursorOf(data) };
      },
      async () => {
        await delay();
        const rows = db().lending.map((r: LendingRow) => ({ ...r, _name: clientsService.get(r.code).name })).filter((r: any) => inScope(scope ?? null, r));
        return applyQuery(rows, { ...q, searchFields: ['code', '_name', 'stage', 'rm', 'an'] });
      },
    );
  },
  byCode(code: string): LendingRow[] { return db().lending.filter((r: LendingRow) => r.code === code); },
  find(id: string): LendingRow | undefined { return db().lending.find((r: LendingRow) => r.id === id); },
  updateStage(id: string, stage: string, by: string) {
    const r = this.find(id); if (!r) return;
    remote('patch', '/lending/' + id + '/stage', { stage });
    const old = r.stage; r.stage = stage; r.updated = today();
    (r.h = r.h || []).push({ stage, t: today(), by });
    if (LEND_GREEN.includes(stage) && !r.sanc) r.sanc = today();
    writeAudit(by, 'Lending stage', r.code, `${old} → ${stage}`);
  },
  update(id: string, key: keyof LendingRow, value: any, by: string) {
    const r = this.find(id); if (!r) return;
    remote('patch', '/lending/' + id, { [key]: value });
    (r as any)[key] = value; writeAudit(by, 'Lending updated', r.code, String(key));
  },
  remove(id: string, by: string) {
    remote('del', '/lending/' + id);
    const i = db().lending.findIndex((r: LendingRow) => r.id === id);
    if (i > -1) { const [x] = db().lending.splice(i, 1); writeAudit(by, 'Lending deleted', x.code, x.id); }
  },
};
