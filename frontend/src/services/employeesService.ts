import { db } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { withFallback, remote, USE_REAL_API } from '../api/http';
import { accessService, toAccessUser, fromAccessUser } from './accessService';
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

export const employeesService = {
  bookRollup,
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
  find(name: string): Employee | undefined { return db().people.find((p: Employee) => p.name === name); },

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
