import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, withFallback, remote, toCursorParams, asRows, nextCursorOf, totalOf } from '../api/http';
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
  async list(q: TableQuery, scope?: RowScope | null) {
    return withFallback<Paged<DealRow>>(
      async () => {
        // Server-paged, exactly as /v1/leads: `limit` is the table's page size and Next
        // carries the previous page's cursor, so one page comes back and applyQuery must
        // NOT re-slice it. Search goes up as `q`; the total comes from with_total.
        const data = await api.get<any>('/deals', toCursorParams(q));
        const rows = asRows(data, 'deals').map(toDealRow).filter((d) => inScope(scope ?? null, d));
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
  update(code: string, key: keyof Deal, value: any, by: string) {
    const d = this.find(code); if (!d) return;
    remote('patch', '/deals/' + code, { [key]: value });
    (d as any)[key] = value; writeAudit(by, 'Deal updated', code, String(key));
  },
  addProduct(code: string, product: 'Lending' | 'Platform Deals' | 'Asset Monetisation', amt: number, by: string) {
    remote('post', '/deals/' + code + '/products', { product, amt });
    let d = this.find(code);
    if (!d) {
      d = { code, rm: '', an: '', lend: false, syn: false, am: false, temp: '', source: '', sourceDetail: '', createdAt: today(), remarks: '' } as Deal;
      db().deals.unshift(d); writeAudit(by, 'Deal created', code, 'via Add product');
    }
    const now = today();
    if (product === 'Lending') {
      d.lend = true;
      db().lending.unshift({ id: 'L' + Date.now(), code, amt, rm: d.rm, an: d.an, stage: 'Data Awaited', updated: now, sanc: null, pendingWith: '', h: [{ stage: 'Data Awaited', t: now, by }], createdAt: now, remarks: '' });
    }
    if (product === 'Platform Deals') {
      d.syn = true;
      db().syn.unshift({ id: 'S' + Date.now(), code, toi: '', rm: d.rm, an: d.an, lc: '', pri: 'Medium', status: 'Deal Sourced', amt, synType: '', mstat3: '', fac: '', tenor: '', im: 'Work not started', pot: '', sancL: '', ipL: '', exist: '', price: '', pendingWith: '', lenders: [], h: [{ status: 'Deal Sourced', t: now, by }], createdAt: now, remarks: '' });
    }
    if (product === 'Asset Monetisation') {
      d.am = true;
      db().am.unshift({ id: 'A' + Date.now(), code, state: clientsService.get(code).state, val: amt, mw: 0, nature: 'Seller', dtype: '', inv: '', itype: '', status: 'Teaser Prepared', teaser: null, createdAt: now, notes: '' });
    }
    writeAudit(by, 'Product added', code, `${product} ₹${amt} Cr`);
  },
  // Delete a deal row (Admin only — gated at the page). Removes the deal record; the
  // product-line registers (Lending / Syn / AM) keep their own rows.
  remove(code: string, by: string) {
    remote('del', '/deals/' + code);
    const i = db().deals.findIndex((d: Deal) => d.code === code);
    if (i > -1) { const [x] = db().deals.splice(i, 1); writeAudit(by, 'Deal deleted', code, x.code); }
  },
};
