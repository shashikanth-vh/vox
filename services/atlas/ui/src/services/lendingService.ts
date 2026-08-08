import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, listAll, withFallback, remote, errText, toCursorParams, asRows, nextCursorOf, totalOf, isRegisterId, USE_REAL_API } from '../api/http';
import { fillFromDeal } from './nameResolver';
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
    entityId: r?.entity_id,
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

/**
 * Read-through cache: the company drawer reads a company's lending lines out of the
 * shared store (`byCode`), so the store has to hold the REGISTER's rows.
 *
 * It did not. The rows there came from the Push-to-Deals dialog's optimistic insert,
 * which mints a local id like `L1754…` — so the drawer's stage dropdown PATCHed
 * `/v1/lending/L1754…`, a path the register could only answer 422 to. The grid worked
 * (real rows, real UUIDs) while the drawer failed on the same control.
 */
function hydrate(rows: LendingRow[]): void {
  const store = db().lending as LendingRow[];
  rows.forEach((row) => {
    const i = store.findIndex((r) => r.id === row.id);
    if (i >= 0) store[i] = { ...store[i], ...row };
    else store.unshift(row);
  });
  // Drop the optimistic locals now that the real rows for that company have arrived: a
  // local row and its register twin would otherwise both render.
  const codes = new Set(rows.map((r) => r.code).filter(Boolean));
  for (let i = store.length - 1; i >= 0; i--) {
    const r = store[i];
    if (r && codes.has(r.code) && !rows.some((x) => x.id === r.id)) store.splice(i, 1);
  }
}

export const lendingService = {
  /** The WHOLE lending book into the store (live mode) — the dashboard must sum every
   *  line, not just the grid pages this session happened to load. */
  async hydrateAll(): Promise<void> {
    if (!USE_REAL_API) return;
    const rows = (await listAll('/lending', { key: 'lending' })).map(toLendingRow);
    await fillFromDeal(rows);
    const store = db().lending as LendingRow[];
    store.length = 0;
    store.push(...rows);
  },

  async list(q: TableQuery, scope?: RowScope | null) {
    return withFallback<Paged<LendingRow>>(
      async () => {
        // Server-paged like /v1/leads and /v1/deals: `limit` is the table's page size and
        // Next carries the previous page's cursor, so one page comes back and applyQuery
        // must NOT re-slice it. The collection proves `limit` (and `deal_id`) here; the
        // cursor and `q` are only sent once the user pages or searches, so the first
        // load is exactly the request the collection makes.
        const data = await api.get<any>('/lending', toCursorParams(q));
        // No inScope here: the register already scoped this list (see auth/rbac.ts).
        const rows = asRows(data, 'lending').map(toLendingRow);
        // The wire row carries deal_id only — join the deal number + company in.
        await fillFromDeal(rows);
        hydrate(rows);
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
  /**
   * Move a lending line to another stage.
   *
   * AWAITED, and the local row is only updated once the register has accepted it. This
   * used to PATCH `/v1/lending/{id}/stage` — a route that does not exist — fire-and-
   * forget, then move the row on screen regardless. Every stage change a user made from
   * the UI was therefore cosmetic: the grid advanced, the database did not, and the
   * failure went no further than a console warning.
   *
   * The GOVERNED stages (Sanctioned, CP/CS Completed, Ready for Disbursement, Disbursed)
   * are refused by the register on a direct PATCH by design — they are reached through
   * committee approval, an approved CP/CS checklist and the Advaya attestation lane. The
   * refusal now reaches the caller instead of being swallowed, so the screen and the
   * register cannot disagree.
   */
  async updateStage(id: string, stage: string, by: string): Promise<{ ok: boolean; error?: string }> {
    const r = this.find(id);
    const from = r?.stage;
    if (USE_REAL_API && !isRegisterId(id)) {
      return { ok: false, error: 'This row has not finished saving to the register yet — '
        + 'refresh the page and try again.' };
    }
    if (USE_REAL_API) {
      try {
        await api.patch('/lending/' + id, { stage });
      } catch (e: any) {
        const msg = errText(e?.response?.data)
          || `The register refused the stage change (HTTP ${e?.response?.status ?? '?'}).`;
        console.warn('[register] lending stage change refused:', e?.response?.data ?? e);
        return { ok: false, error: msg };
      }
    }
    if (r) {
      r.stage = stage; r.updated = today();
      (r.h = r.h || []).push({ stage, t: today(), by });
      if (LEND_GREEN.includes(stage) && !r.sanc) r.sanc = today();
      writeAudit(by, 'Lending stage', r.code, `${from} → ${stage}`);
    }
    return { ok: true };
  },
  // UI field → the LendingUpdate wire name: {amt: 5} is not a register field, so the
  // old body 422ed even when the id was right. isRegisterId guards the rows a local
  // optimistic insert minted before its register row existed.
  update(id: string, key: keyof LendingRow, value: any, by: string) {
    const r = this.find(id); if (!r) return;
    const wire: Record<string, string> = {
      amt: 'amount_cr', rm: 'rm', an: 'analyst', pendingWith: 'pending_with',
      sanc: 'sanction_date', remarks: 'remarks',
      proposedAmt: 'proposed_disbursement_amount', proposedDate: 'proposed_disbursement_date',
    };
    if (isRegisterId(id) && wire[key as string]) {
      remote('patch', '/lending/' + id, { [wire[key as string]]: value === '' ? null : value });
    }
    (r as any)[key] = value; writeAudit(by, 'Lending updated', r.code, String(key));
  },
  remove(id: string, by: string) {
    if (isRegisterId(id)) remote('del', '/lending/' + id);
    const i = db().lending.findIndex((r: LendingRow) => r.id === id);
    if (i > -1) { const [x] = db().lending.splice(i, 1); writeAudit(by, 'Lending deleted', x.code, x.id); }
  },
};
