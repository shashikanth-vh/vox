import axiosClient, { gwClient, USE_REAL_API } from './axiosClient';
import type { TableQuery } from '../services/types';

export { USE_REAL_API };

// Map the table query into backend query params (server-side paging — req 16).
export function toParams(q: TableQuery): Record<string, any> {
  const p: Record<string, any> = { page: q.pageIndex, size: q.pageSize };
  if (q.globalFilter) p.q = q.globalFilter;
  if (q.sorting?.length) { p.sortBy = q.sorting[0].id; p.sortDir = q.sorting[0].desc ? 'desc' : 'asc'; }
  (q.columnFilters ?? []).forEach((c) => { if (c.value != null && c.value !== '') p['filter.' + c.id] = c.value; });
  return p;
}

// ---------------------------------------------------------------------------
// Cursor-paged list endpoints (/v1/leads, /v1/deals): they take q, limit, cursor,
// include_deleted, include_reconciliation and with_total — and NOT the page/size/sortBy
// that toParams() builds. Their filters fail closed, so an unrecognised param rejects
// the whole list rather than being ignored.
// ---------------------------------------------------------------------------

/**
 * The query param that carries the cursor for the next page. The RESPONSE field is
 * `next_cursor`; the REQUEST param is `cursor` — the register fails closed on unknown
 * params and 422s `next_cursor`, which the mock fallback then papered over as "page 2
 * shows page 1 again" the first day the pager could actually reach page 2.
 */
export const CURSOR_PARAM = 'cursor';

/**
 * Ceiling on `limit`. The table asks for a huge page size when it builds filter-option
 * lists (it wants "everything"); this stops that becoming a request for 100000 rows.
 */
export const LIST_MAX_LIMIT = 200;

/**
 * Query params for a cursor-paged list: one page's worth, with the exact dataset count
 * (with_total) so the pager can say "1-10 of 112" and keep Next alive. Every param here
 * is one the endpoint accepts, which matters when unrecognised ones fail the whole
 * request.
 */
export function toCursorParams(q: TableQuery): Record<string, any> {
  const p: Record<string, any> = {
    limit: Math.min(Math.max(1, q.pageSize || 25), LIST_MAX_LIMIT),
    // The register computes the exact dataset count only when asked. Never asking is
    // why every pager read "1-10 of 10" whatever the book held — totalOf fell back to
    // the page length, and MRT greyed Next on a "complete" page.
    with_total: 'true',
  };
  if (q.cursor) p[CURSOR_PARAM] = q.cursor;
  const search = q.globalFilter?.trim();
  if (search) p.q = search;
  // The committed facet selections, already translated to the register's param names
  // (CommonTable builds serverFilters from each column's meta.filterParam). Multi-select
  // joins to a comma IN-list, which the register's filter layer splits. Dropping these
  // on the floor is how every grid's checkbox filters were silently dead in live mode.
  for (const f of q.serverFilters ?? []) {
    if (f.param && f.values.length) p[f.param] = f.values.join(',');
  }
  return p;
}

/** List payloads differ by service; accept a bare array or the usual envelopes. */
export function asRows(data: any, key?: string): any[] {
  if (Array.isArray(data)) return data;
  return data?.items ?? data?.results ?? data?.data ?? (key ? data?.[key] : undefined) ?? [];
}

/** The cursor for the page after this one — null when there isn't one. */
export function nextCursorOf(data: any): string | null {
  return data?.next_cursor ?? data?.nextCursor ?? data?.cursor ?? null;
}

/** The server's total row count, falling back to what this page actually holds. */
export function totalOf(data: any, fallback: number): number {
  const t = data?.total ?? data?.total_count ?? data?.count;
  return Number.isFinite(Number(t)) ? Number(t) : fallback;
}

/**
 * Is this a REGISTER-issued id (a UUID), or a leftover local one?
 *
 * The prototype minted ids like `L`+timestamp for optimistically-inserted rows. A screen
 * that renders one of those addresses a row the register has never had, so every write
 * comes back `422 uuid_parsing on path.obj_id`. Anything reading the shared store must be
 * able to tell the two apart.
 */
export const isRegisterId = (id?: string): boolean =>
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(id || ''));

// Reads: hit the API when enabled, otherwise fall back to mock. Whether a FAILED real
// call may also fall back to mock is a build choice: 'false' (the platform image) lets
// failures surface as failures — silent mock-on-error twice masqueraded as "the UI
// shows mock data" when the true problem was auth. Demo builds keep the soft fallback.
const MOCK_ON_ERROR = import.meta.env.VITE_MOCK_FALLBACK !== 'false';

export async function withFallback<T>(real: () => Promise<T>, mock: () => T | Promise<T>): Promise<T> {
  if (!USE_REAL_API) return mock();
  try { return await real(); } catch (e) {
    if (!MOCK_ON_ERROR) throw e;
    console.warn('[api] falling back to mock:', e);
    return mock();
  }
}

// Origin-rooted calls (gateway prefixes such as /atlas/v1/*).
export const gwApi = {
  get: <T>(url: string, params?: Record<string, any>) => gwClient.get<T>(url, { params }).then((r) => r.data),
};

/**
 * Read EVERY row of a keyset-paged list endpoint, following `next_cursor`.
 *
 * One request returns one page. Treating that page as the whole list is how a register
 * holding two companies rendered a one-row grid, and the register's `limit` is capped
 * server-side (`max_page_size`), so asking for a bigger number is a 422 rather than a
 * bigger page. Every list read that wants "all of it" goes through here.
 *
 * `key` names the envelope field for services that answer `{<key>: [...]}`; `params`
 * carries anything else the endpoint accepts (filters that are known-good — the register
 * fails CLOSED on an unrecognised query param and rejects the whole list).
 */
export async function listAll(
  url: string, opts: { key?: string; params?: Record<string, any>; max?: number } = {},
): Promise<any[]> {
  const { key, params, max = 5000 } = opts;
  const rows: any[] = [];
  let cursor: string | undefined;
  do {
    const limit = Math.min(LIST_MAX_LIMIT, max - rows.length);
    if (limit < 1) break;
    const page: any = await api.get<any>(url, { ...params, limit, ...(cursor ? { cursor } : {}) });
    rows.push(...asRows(page, key));
    cursor = nextCursorOf(page) ?? undefined;
  } while (cursor && rows.length < max);
  // Never silently short: a truncated list that LOOKS complete is the bug this exists for.
  if (cursor) console.warn('[api] %s stopped at the %s-row ceiling — later rows are not listed.', url, max);
  return rows;
}

export const api = {
  get: <T>(url: string, params?: Record<string, any>) => axiosClient.get<T>(url, { params }).then((r) => r.data),
  post: <T>(url: string, data?: any) => axiosClient.post<T>(url, data).then((r) => r.data),
  patch: <T>(url: string, data?: any) => axiosClient.patch<T>(url, data).then((r) => r.data),
  del: <T>(url: string, data?: any) => axiosClient.delete<T>(url, { data }).then((r) => r.data),
};

// The gateway wraps failures as {"error":{"type","title","detail"}}; the services behind
// it use FastAPI's bare {"detail":[…]} for validation. Read both, and keep the `loc` path
// from Pydantic entries so a 422 names the offending FIELD rather than saying "invalid".
export function errText(data: any): string {
  const d = data?.error?.detail ?? data?.detail ?? data?.error?.title ?? data?.message;
  // A register 422 puts a summary in `detail` and the ACTIONABLE part — which field, and
  // why — in `errors[]`. Reading only the summary turns "obj_id is not a valid UUID" into
  // "One or more fields are invalid", which is what a user then reports, and it costs an
  // afternoon to work back from.
  const fields: string[] = (data?.error?.errors || []).map((x: any) => {
    const loc = Array.isArray(x?.loc) ? x.loc.filter((p: any) => p !== 'body').join('.') : '';
    const msg = x?.msg || x?.type || '';
    return loc ? `${loc}: ${msg}` : String(msg);
  }).filter(Boolean);
  if (fields.length && typeof d === 'string') return `${d} (${fields.join('; ')})`;
  if (Array.isArray(d)) {
    return d.map((x: any) => {
      const loc = Array.isArray(x?.loc) ? x.loc.filter((p: any) => p !== 'body').join('.') : '';
      const msg = x?.msg || x?.type || JSON.stringify(x);
      return loc ? `${loc}: ${msg}` : msg;
    }).join('; ');
  }
  if (typeof d === 'string') return d;
  return d ? JSON.stringify(d) : '';
}

// The EDGE's own failures carry no envelope. When nginx cannot reach the gateway — the
// container is down, restarting, or was recreated mid-request — it answers its own HTML
// error page, `errText` finds nothing in it, and the caller falls back to axios's
// "Request failed with status code 502". That sentence names no service, no lane and no
// next step, and it is indistinguishable from an application refusal: a desk reports it,
// and the actual cause (a service that was not running) has to be guessed at from
// scratch. So when the body is NOT a problem envelope, say what the status means.
const _EDGE: Record<number, string> = {
  502: 'the service behind it did not answer, so it may be down or restarting',
  503: 'the service is unavailable, either starting up or stopped',
  504: 'the service took too long to answer and the edge gave up waiting',
};

/** The message for a failed request: the server's own words when it sent any, otherwise
 *  a plain account of the infrastructure failure. `what` completes "Could not <what>". */
export function apiErr(e: any, what: string): string {
  const detail = errText(e?.response?.data);
  if (detail) return detail;
  const status: number | undefined = e?.response?.status;
  if (status && _EDGE[status]) {
    return `Could not ${what} — ${_EDGE[status]}. `
      + `Nothing was changed; try again once it is back (HTTP ${status}).`;
  }
  // No response at all: the request never reached a server (offline, DNS, TLS).
  if (!e?.response) return `Could not ${what} — PRISM could not be reached. Check the connection.`;
  return e?.message || `Could not ${what}.`;
}

// Writes: dispatch to the backend when enabled (fire-and-forget so the optimistic
// local-store mutation keeps the UI responsive). No-op in mock mode.
export function remote(method: 'post' | 'patch' | 'del', url: string, data?: any): void {
  if (!USE_REAL_API) return;
  api[method](url, data).catch((e) => console.warn('[api] write failed:', method, url, e));
}
