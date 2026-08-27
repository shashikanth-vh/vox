import { db, today } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, errText, remote, remoteDebounced, withFallback, USE_REAL_API } from '../api/http';
import { writeAudit } from './auditService';
import { entitiesService, entityError } from './entitiesService';
import { mintCode } from './conversionService';
import { normName } from '../utils/format';
import type { Paged, TableQuery } from './types';
import type { Client, ClientRow } from '../pages/Clients/client.types';

// The rest of the app reads a client synchronously out of the local store, keyed by
// code (CompanyDrawer, the dashboard rollups, every product grid). So a live list is
// also written back into that store — a read-through cache, keyed by the Register's
// own code. Rows already present are updated in place, and nothing is deleted: a
// client the Register doesn't return may still be referenced by a local deal row.
function hydrate(rows: ClientRow[]) {
  rows.forEach(({ code, ...rest }) => {
    if (!code) return;
    db().clients[code] = { ...(db().clients[code] || {}), ...rest };
  });
}

export const clientsService = {
  /** The WHOLE client registry into the store (live mode), replacing stale keys — a
   *  replace-mode import mid-session must not leave ghost companies in the tiles. */
  async hydrateAll(): Promise<void> {
    const rows = await entitiesService.list();
    const store = db().clients as Record<string, any>;
    Object.keys(store).forEach((k) => delete store[k]);
    hydrate(rows);
  },

  get(code: string): Client { return db().clients[code] ?? { name: code, sector: '', lens: '', state: '', about: '', toi: '' }; },
  // A client IS a Register entity, so the read is GET /v1/entities — there is no
  // /clients route. The Register neither pages nor sorts for us (its filtering is
  // fail-closed, so sending page/size/sortBy would 400 the whole list), hence the
  // rows come back whole and applyQuery does the paging locally — the same engine,
  // and the same {rows,total} contract, as the mock path.
  async list(q: TableQuery): Promise<Paged<ClientRow>> {
    const page = (rows: ClientRow[]) => applyQuery(rows, { ...q, searchFields: ['code', 'name', 'sector'] });
    return withFallback(
      async () => {
        const rows = await entitiesService.list();
        hydrate(rows);
        return page(rows);
      },
      async () => {
        await delay();
        return page(Object.entries(db().clients).map(([code, v]: any) => ({ code, ...v })));
      },
    );
  },
  // Manually onboard a client (normally they arrive via lead conversion).
  // v19 mAdd('clients'): only the name is asked for — the Group Code is minted
  // from it, and duplicates are caught on the normalised name, not the code.
  //
  // In PRISM a client IS an entity, so the write is POST /v1/entities (there is no
  // /clients route). Awaited, not fire-and-forget: a rejection (duplicate code/CIN,
  // unknown sector) has to reach the user instead of console.warn. The local-store
  // insert stays optimistic so the grid updates instantly, and mock mode skips
  // straight to it.
  async create(input: { name: string; sector?: string; lens?: string; state?: string; toi?: string; about?: string }, by: string): Promise<{ ok: boolean; error?: string; code?: string }> {
    const name = (input.name || '').trim();
    if (!name) return { ok: false, error: 'Company name is required' };
    // The duplicate check asks the REGISTER, not the browser's cache. That cache holds
    // the prototype's sample companies and is never pruned when a row leaves the
    // register, so it refused real names on a fresh deployment and went on citing a
    // company by a code the database no longer had.
    if (USE_REAL_API) {
      try {
        const existing = await entitiesService.findByName(name);
        if (existing) return { ok: false, error: `Already on the register as ${existing.code}` };
      } catch (e) {
        // A search that cannot run must not block onboarding: the register's own unique
        // constraint is the real guard, and a 409 from it lands in the alert below.
        console.warn('[register] duplicate-name search failed; letting the register decide:', e);
      }
    } else {
      const dup = Object.entries(db().clients).find(([, v]: any) => normName(v.name) === normName(name));
      if (dup) return { ok: false, error: `Already on the register as ${dup[0]}` };
    }
    const code = mintCode(name);
    const client: any = {
      name, sector: input.sector || 'Other', lens: input.lens || 'Mitigation',
      state: input.state || '', about: input.about || '', toi: input.toi || '',
      notes: `Added ${today()}`, lifecycle: 'Prospect',
    };
    if (USE_REAL_API) {
      try {
        const entity = await entitiesService.create({
          company: name, sector: client.sector, lens: client.lens,
          state: client.state, notes: client.about,
        });
        client.entityId = entity?.id;
        client.entityCode = entity?.code;
      } catch (e: any) {
        return { ok: false, error: entityError(e) };
      }
    }
    db().clients[code] = client;
    writeAudit(by, 'Client created', code, name);
    return { ok: true, code };
  },
  // UI field → the EntityUpdate wire name. `/clients/{code}` never existed on the
  // register (a client IS an entity, addressed by UUID), so every client edit and
  // delete 404ed silently for as long as remote() has been fire-and-forget — the
  // same defect Add-employee had, in its next hiding place.
  update(code: string, patch: Partial<Client>, by: string) {
    const row: any = db().clients[code] || {};
    const wire: Record<string, string> = {
      name: 'legal_name', sector: 'sector', lens: 'lens', state: 'state',
      toi: 'toi', about: 'about', lifecycle: 'lifecycle',
    };
    const body: Record<string, any> = {};
    Object.entries(patch).forEach(([k, v]) => { if (wire[k]) body[wire[k]] = v; });
    if (row.entityId && Object.keys(body).length) {
      remoteDebounced('patch', '/entities/' + row.entityId, body);
    }
    Object.assign(db().clients[code], patch);
    writeAudit(by, 'Client updated', code, Object.keys(patch).join(','));
  },
  // v12 DEL.clients: a client carrying deal or product rows can't be removed —
  // those have to go first, otherwise they'd be orphaned.
  inUse(code: string): boolean {
    const d: any = db();
    return [d.deals, d.lending, d.syn, d.am].some((arr: any[]) => (arr || []).some((x: any) => x.code === code));
  },
  /**
   * The REGISTER is the referee on whether a company may go: its delete refuses (409,
   * naming what the company still carries) when live leads/deals/lines reference it.
   * The local inUse() check only sees rows this tab happened to load — on a fresh tab
   * it is empty, which is exactly how a company was once deleted out from under its
   * lending line, leaving the line's Data Register answering "Entity … not found".
   */
  async remove(code: string, by: string): Promise<{ ok: boolean; error?: string }> {
    const name = db().clients[code]?.name || code;
    const eid = (db().clients[code] as any)?.entityId;
    if (USE_REAL_API && eid) {
      try {
        await api.del('/entities/' + eid);
      } catch (e: any) {
        return { ok: false, error: errText(e?.response?.data) || 'The register refused the delete.' };
      }
    } else if (this.inUse(code)) {
      return { ok: false, error: 'Remove this client’s deal & product rows first' };
    }
    delete db().clients[code];
    writeAudit(by, 'Deleted', code, `Client row — ${name}`);
    return { ok: true };
  },
};
