import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, errText, withFallback, remote, toCursorParams, asRows, nextCursorOf, totalOf, USE_REAL_API, listAll } from '../api/http';
import { fillCompanyFromEntity } from './nameResolver';
import { writeAudit } from './auditService';
import { clientsService } from './clientsService';
import type { TableQuery, Paged } from './types';
import type { Deal, DealRow } from '../pages/Deals/deal.types';
import { inScope, type RowScope } from '../auth/rbac';

/**
 * An API deal read back as the row the grid renders. The wire is snake_case and uses the
 * API's own names (is_lending, analyst, note), none of which match the grid's accessor
 * keys — unmapped, every column but the raw id renders blank.
 */
export function toDealRow(r: any): DealRow {
  return {
    // The grid's "Group Code" column: the human deal number, never the UUID.
    code: r?.deal_no || r?.code || '',
    apiId: r?.id,
    entityId: r?.entity_id,
    _name: r?.company || r?.entity_name || r?.display_name || '',
    rm: r?.rm || '',
    an: r?.analyst || '',
    lend: !!r?.is_lending,
    syn: !!r?.is_syndication,
    am: !!r?.is_asset_mon,
    temp: r?.temperature || '',
    lens: r?.lens || '',
    source: r?.source || '',
    sourceDetail: r?.source_name || '',
    createdAt: (r?.created_at || '').slice(0, 10),
    remarks: r?.note || r?.remarks || '',
    stage: r?.stage || '',
    productType: r?.product_type || '',
    amountCr: Number(r?.amount_cr) || 0,
  };
}

export const dealsService = {
  /** The WHOLE deal book into the store (live mode) — the dashboard derives its
   *  funnel and per-product splits from here, so partial pages must never feed it. */
  async hydrateAll(): Promise<void> {
    if (!USE_REAL_API) return;
    const rows = (await listAll('/deals', { key: 'deals' })).map(toDealRow);
    await fillCompanyFromEntity(rows);
    const store = db().deals as Deal[];
    store.length = 0;
    store.push(...rows);
  },
  async list(q: TableQuery, scope?: RowScope | null) {
    return withFallback<Paged<DealRow>>(
      async () => {
        // Server-paged, exactly as /v1/leads: `limit` is the table's page size and Next
        // carries the previous page's cursor, so one page comes back and applyQuery must
        // NOT re-slice it. Search goes up as `q`; the total comes from with_total.
        const data = await api.get<any>('/deals', toCursorParams(q));
        // No inScope here: the register already scoped this list (see auth/rbac.ts).
        const rows = asRows(data, 'deals').map(toDealRow);
        // The wire row carries entity_id, not a company name — join it in for the grid.
        await fillCompanyFromEntity(rows);
        return { rows, total: totalOf(data, rows.length), nextCursor: nextCursorOf(data) };
      },
      async () => {
        await delay();
        const rows = db().deals.map((d: Deal) => ({ ...d, _name: clientsService.get(d.code).name })).filter((d: any) => inScope(scope ?? null, d));
        return applyQuery(rows, { ...q, searchFields: ['code', '_name'] });
      },
    );
  },
  find(code: string): Deal | undefined { return db().deals.find((d: Deal) => d.code === code); },
  // UI field → the DealUpdate wire name. The old write PATCHed /deals/{groupCode}
  // with UI field names — the register addresses deals by UUID and forbids unknown
  // fields, so every inline deal edit died silently. `apiId` is the register's id,
  // carried on every hydrated row.
  update(code: string, key: keyof Deal, value: any, by: string) {
    const d = this.find(code); if (!d) return;
    const wire: Record<string, string> = {
      rm: 'rm', an: 'analyst', temp: 'temperature', source: 'source',
      sourceDetail: 'source_detail', remarks: 'remarks',
    };
    if ((d as any).apiId && wire[key as string]) {
      remote('patch', '/deals/' + (d as any).apiId, { [wire[key as string]]: value || null });
    }
    (d as any)[key] = value; writeAudit(by, 'Deal updated', code, String(key));
  },
  /**
   * Add a product line — as a REGISTER fact first, then the local mirror.
   *
   * The old write POSTed /deals/{code}/products, a route that has never existed on the
   * register: the tracker row lived only in the browser, so it vanished on reload, no
   * other user ever saw it, and its edits PATCHed a minted local id the register could
   * only 422. Now the deal (created here if the company has none), the deal's product
   * flag, and the tracker row are all register writes, awaited, and the local insert
   * carries the register's OWN uuid so every later edit on the row lands.
   */
  async addProduct(code: string, product: 'Lending' | 'Platform Deals' | 'Asset Monetisation', amt: number, by: string): Promise<{ ok: boolean; error?: string }> {
    let d = this.find(code);
    const now = today();
    let apiId: string | undefined = (d as any)?.apiId;
    let entityId: string | undefined = (d as any)?.entityId || (clientsService.get(code) as any)?.entityId;
    let lineId = '';
    if (USE_REAL_API) {
      if (!entityId) {
        return { ok: false, error: `${code} is not a register company yet — open it under Clients first, then add the product line.` };
      }
      try {
        if (!apiId) {
          const created = await api.post<any>('/deals', {
            entity_id: entityId, stage: 'In Pipeline', source: 'RM',
            source_detail: 'Added from the Deals register', rm: d?.rm || null, analyst: d?.an || null,
          });
          apiId = created?.id;
        }
        const flag = product === 'Lending' ? 'is_lending' : product === 'Platform Deals' ? 'is_syndication' : 'is_asset_mon';
        await api.patch<any>('/deals/' + apiId, { [flag]: true });
        if (product === 'Lending') {
          const row = await api.post<any>('/lending', {
            entity_id: entityId, deal_id: apiId, amount_cr: amt || null,
            stage: 'Data Awaited', rm: d?.rm || null, analyst: d?.an || null,
          });
          lineId = row?.id || '';
        } else if (product === 'Platform Deals') {
          const row = await api.post<any>('/syndication', {
            entity_id: entityId, deal_id: apiId, amount_cr: amt || null,
            status: 'Deal Sourced', priority: 'Medium', rm: d?.rm || null, analyst: d?.an || null,
          });
          lineId = row?.id || '';
        } else {
          const row = await api.post<any>('/asset-monetisation', {
            entity_id: entityId, deal_id: apiId, indicative_value_cr: amt || null,
            state: clientsService.get(code).state || null, nature: 'Seller', status: 'Teaser Prepared',
          });
          lineId = row?.id || '';
        }
      } catch (e: any) {
        return { ok: false, error: errText(e?.response?.data) || e?.message || 'The register refused the product line.' };
      }
    }
    if (!d) {
      d = { code, rm: '', an: '', lend: false, syn: false, am: false, temp: '', source: '', sourceDetail: '', createdAt: now, remarks: '', apiId, entityId } as any as Deal;
      db().deals.unshift(d); writeAudit(by, 'Deal created', code, 'via Add product');
    }
    (d as any).apiId = apiId; (d as any).entityId = entityId;
    if (product === 'Lending') {
      d.lend = true;
      db().lending.unshift({ id: lineId || 'L' + Date.now(), code, amt, rm: d.rm, an: d.an, stage: 'Data Awaited', updated: now, sanc: null, pendingWith: '', h: [{ stage: 'Data Awaited', t: now, by }], createdAt: now, remarks: '' });
    }
    if (product === 'Platform Deals') {
      d.syn = true;
      db().syn.unshift({ id: lineId || 'S' + Date.now(), apiId: lineId || undefined, code, toi: '', rm: d.rm, an: d.an, lc: '', pri: 'Medium', status: 'Deal Sourced', amt, synType: '', mstat3: '', fac: '', tenor: '', im: 'Work not started', pot: '', sancL: '', ipL: '', exist: '', price: '', pendingWith: '', lenders: [], h: [{ status: 'Deal Sourced', t: now, by }], createdAt: now, remarks: '' });
    }
    if (product === 'Asset Monetisation') {
      d.am = true;
      db().am.unshift({ id: lineId || 'A' + Date.now(), code, state: clientsService.get(code).state, val: amt, mw: 0, nature: 'Seller', dtype: '', inv: '', itype: '', status: 'Teaser Prepared', teaser: null, createdAt: now, notes: '' });
    }
    writeAudit(by, 'Product added', code, `${product} ₹${amt} Cr`);
    return { ok: true };
  },
  /**
   * What still blocks this deal from closing — open EWS cases, unresolved covenant
   * observations, product lines that have not reached a terminal stage. Read BEFORE the
   * close so the dialog can name each item, rather than offering a button that fails.
   */
  async openItems(apiId: string): Promise<{
    blocked: boolean; ews_cases: any[]; covenants: any[]; lines: any[];
  } | null> {
    if (!USE_REAL_API || !apiId) return null;
    try {
      return await api.get<any>('/deals/' + apiId + '/open-items');
    } catch {
      // Advisory only: a failed pre-check must not stop someone closing a deal. The
      // register runs the same validation again and is the one that decides.
      return null;
    }
  },

  /**
   * Close the deal, recording HOW it ended.
   *
   * The three outcomes are not decoration. 'lost' is a deal Evam wanted and did not get;
   * 'dropped' is one Evam walked away from. Collapsing them would leave the book able to
   * say how many deals did not close but not how many of those were our own decision —
   * and that is the question the funnel exists to answer.
   *
   * AWAITED, never fire-and-forget: the register refuses a close while the deal still
   * owes answers, and that refusal is the whole point of the endpoint. A silent failure
   * would show the desk a closed deal that is still open on the book.
   */
  async close(code: string, outcome: 'won' | 'lost' | 'dropped', note: string, by: string):
      Promise<{ ok: boolean; error?: string; stage?: string }> {
    const d = this.find(code);
    const apiId = (d as any)?.apiId;
    const STAGE = { won: 'Closed Won', lost: 'Closed Lost', dropped: 'Dropped' } as const;
    if (USE_REAL_API) {
      if (!apiId) {
        return { ok: false, error: `${code} is not a register deal yet — nothing to close.` };
      }
      try {
        const r = await api.post<any>('/deals/' + apiId + '/close', { outcome, note });
        if (d) (d as any).stage = r?.stage || STAGE[outcome];
        writeAudit(by, 'Deal closed', code, `${STAGE[outcome]} — ${note}`);
        return { ok: true, stage: r?.stage || STAGE[outcome] };
      } catch (e: any) {
        return { ok: false, error: errText(e?.response?.data) || 'The register refused the close.' };
      }
    }
    if (d) (d as any).stage = STAGE[outcome];
    writeAudit(by, 'Deal closed', code, `${STAGE[outcome]} — ${note}`);
    return { ok: true, stage: STAGE[outcome] };
  },

  // Delete a deal row (Admin only — gated at the page). Removes the deal record; the
  // product-line registers (Lending / Syn / AM) keep their own rows.
  remove(code: string, by: string) {
    const d = this.find(code);
    if ((d as any)?.apiId) remote('del', '/deals/' + (d as any).apiId);
    const i = db().deals.findIndex((d: Deal) => d.code === code);
    if (i > -1) { const [x] = db().deals.splice(i, 1); writeAudit(by, 'Deal deleted', code, x.code); }
  },
};
