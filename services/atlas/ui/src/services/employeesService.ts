import { db } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { withFallback, remote, listAll, USE_REAL_API } from '../api/http';
import { accessService, toAccessUser, fromAccessUser } from './accessService';
import { servedByRegister } from './referenceService';
import { writeAudit } from './auditService';
import type { TableQuery } from './types';
import type { Employee, BookRollup } from '../pages/Employees/employee.types';

// Book size = number of live records owned (as RM or Analyst) across the pipeline.
// Mirrors the template's empBookRollup(name).
export function bookRollup(name: string): BookRollup {
  const d = db();
  const r: BookRollup = { leads: 0, activeLeads: 0, deals: 0, lend: 0, syn: 0, am: 0, total: 0 };
  const mine = (x: any) => x.rm === name || x.an === name;
  (d.leads || []).forEach((l: any) => { if (mine(l)) { r.leads++; if (l.status === 'Active') r.activeLeads++; } });
  (d.deals || []).forEach((x: any) => { if (mine(x)) r.deals++; });
  (d.lending || []).forEach((x: any) => { if (mine(x)) r.lend++; });
  (d.syn || []).forEach((x: any) => { if (mine(x)) r.syn++; });
  (d.am || []).forEach((x: any) => { if (mine(x)) r.am++; });
  r.total = r.leads + r.deals + r.lend + r.syn + r.am;
  return r;
}

// Which roster bucket a role belongs in. The Register's Person.role is free-ish text
// ("BDRM", "BD Head", "Deal Analyst", "Credit Head", "Admin, Management"), so it is
// matched rather than switched on.
// No word boundaries: the commonest role of all, "BDRM", has neither "BD" nor "RM"
// standing alone in it.
const IS_RM = /(RM|BD|Relationship)/i;
const IS_ANALYST = /(Analyst|Credit|Ops)/i;

/**
 * Replace the RM / Analyst option lists — and the local people directory — with the
 * Register's OWN roster (GET /v1/people).
 *
 * These lists were seeded from the prototype JSON, so every dialog offered a name that
 * existed only in the browser. A conversion then died on the Register's people check
 * ("Unknown rm 'Shubh' — not a person on record"), because the roster the user picked
 * from was never the roster the Register validates against. Now it is the same list.
 *
 * Names are the SHORT HANDLE (Person.name), which is what leads, deals and the book
 * rollups key on throughout ATLAS. Fail-soft: an unreachable Register leaves the seeded
 * lists in place rather than emptying every dropdown.
 */
async function hydrateRoster(): Promise<void> {
  if (!USE_REAL_API) return;
  let rows: any[];
  try {
    rows = await listAll('/people', { key: 'people' });
  } catch (e) {
    console.warn('[api] people roster unavailable — keeping the seeded name lists:', e);
    return;
  }
  const live = rows.filter((p: any) => !p.inactive);
  if (!live.length) return;                      // an empty roster is not an improvement

  db().people = live.map((p: any): Employee => ({
    name: p.name || p.full_name, full: p.full_name || p.name, role: p.role || '',
    username: '', email: p.email || '', phone: p.phone || '', geography: p.geography || '',
    sectors: p.sectors || '', startedOn: p.started_on || '', reportsTo: p.reports_to || '',
    inactive: false, notes: p.notes || '', registerId: p.id,
  } as Employee));

  const named = (test: RegExp) => db().people
    .filter((p: any) => test.test(p.role || ''))
    .map((p: any) => p.name);
  const all = db().people.map((p: any) => p.name);
  // A bucket that matches nobody falls back to the whole roster: better to offer every
  // real name than an empty select the user cannot get past. Skipped entirely when
  // /v1/ref already served the list — the Register derives it from the same directory
  // and knows the role catalogue better than a regex here does.
  if (!servedByRegister.has('RM')) db().ref.RM = named(IS_RM).length ? named(IS_RM) : all;
  if (!servedByRegister.has('Analyst')) {
    db().ref.Analyst = named(IS_ANALYST).length ? named(IS_ANALYST) : all;
  }
}

export const employeesService = {
  bookRollup,
  hydrateRoster,
  // Employees ARE Access users — the same records the sign-in flow resolves against — so
  // the grid reads GET /access/v1/users rather than the Register's /employees route.
  // Access answers with a bare array (no {rows,total}), so paging/sorting/column filters
  // run through applyQuery here instead of server-side. withFallback still drops to the
  // seed JSON when the app is offline or in mock mode.
  async list(q: TableQuery) {
    const searchFields = ['name', 'full', 'role', 'username', 'email'];
    return withFallback(
      async () => {
        // Global search goes to Access as ?q= AND is re-applied locally: ?q= is a
        // server-side search over the fields Access indexes, and the local pass covers
        // the ATLAS-only ones (role, username) it cannot see.
        const users = await accessService.listUsers(q.globalFilter || undefined);
        return applyQuery(users.map(fromAccessUser), { ...q, searchFields });
      },
      async () => {
        await delay();
        return applyQuery(db().people as Employee[], { ...q, searchFields });
      },
    );
  },
  // Matched on EITHER name — grids and dialogs carry the short handle ("Shubh"), while an
  // imported row or an Access-sourced value may carry the full one ("Shubh Dave"). The
  // Register accepts both on conversion, so the directory lookup must too.
  find(name: string): Employee | undefined {
    const want = (name || '').trim().toLowerCase();
    if (!want) return undefined;
    return db().people.find((p: Employee) =>
      (p.name || '').trim().toLowerCase() === want || (p.full || '').trim().toLowerCase() === want);
  },

  /**
   * GET /access/v1/users?q=<email> — the authoritative record for ONE user, read fresh
   * when the edit dialog opens so the form never edits a stale grid row.
   *
   * ?q= is a search, not a key lookup, so the result is exact-matched on e-mail exactly
   * as sign-in does (authService.fetchAccessUser). Returns null in mock mode, when the
   * row has no e-mail, or when Access holds no such user — all cases where the caller
   * should keep the row it already has.
   */
  async fetchByEmail(email?: string): Promise<Employee | null> {
    const id = (email || '').trim();
    if (!USE_REAL_API || !id) return null;
    const rows = await accessService.findUsers(id);
    const hit = rows.find((r) => (r.email || '').toLowerCase() === id.toLowerCase());
    return hit ? fromAccessUser(hit) : null;
  },
  update(name: string, patch: Partial<Employee>, by: string) {
    const p = this.find(name); if (!p) return;
    remote('patch', '/employees/' + name, patch);
    Object.assign(p, patch); writeAudit(by, 'Employee updated', name, Object.keys(patch).join(','));
  },
  // Adding an employee is a PROVISIONING act, not a row insert: the person needs an
  // Access identity before they can sign in, and Access owns the roles that drive RBAC.
  // So this one write is awaited rather than fire-and-forget — a duplicate email or a
  // rejected role has to reach the user, not console.warn. The local store is only
  // updated once Access has accepted.
  async create(input: Partial<Employee>, by: string): Promise<Employee> {
    const emp: Employee = { name: input.name || 'New', full: input.full || input.name || 'New', role: input.role || 'BDRM', username: '', email: '', phone: '', geography: '', sectors: '', startedOn: '', reportsTo: '', inactive: false, notes: '', ...input } as Employee;
    if (USE_REAL_API) {
      const created = await accessService.createUser(toAccessUser(emp));
      emp.accessId = created.id;
    }
    db().people.push(emp); writeAudit(by, 'Employee added', emp.name, emp.full); return emp;
  },
  remove(name: string, by: string) {
    remote('del', '/employees/' + name);
    const i = db().people.findIndex((p: Employee) => p.name === name);
    if (i > -1) { db().people.splice(i, 1); writeAudit(by, 'Employee deleted', name, ''); }
  },
};
