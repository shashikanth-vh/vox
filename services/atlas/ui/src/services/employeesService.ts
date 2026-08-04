import { db } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { api, errText, withFallback, remote, listAll, USE_REAL_API } from '../api/http';
import { accessService, toAccessUser, fromAccessUser } from './accessService';
import { referenceService, servedByRegister } from './referenceService';
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

/** Employee → the Register's PersonCreate/Update shape (only the fields it accepts). */
function toPersonBody(e: Partial<Employee>): Record<string, any> {
  const out: Record<string, any> = {};
  if (e.name !== undefined) out.name = e.name;
  if (e.full !== undefined) out.full_name = e.full || e.name;
  if (e.role !== undefined) out.role = e.role;
  if (e.email !== undefined) out.email = e.email || null;
  if (e.phone !== undefined) out.phone = e.phone || null;
  if (e.geography !== undefined) out.geography = e.geography || null;
  if (e.sectors !== undefined) out.sectors = e.sectors || null;
  if (e.startedOn !== undefined) out.started_on = e.startedOn || null;
  if (e.reportsTo !== undefined) out.reports_to = e.reportsTo || null;
  if (e.inactive !== undefined) out.inactive = !!e.inactive;
  if (e.notes !== undefined) out.notes = e.notes || null;
  return out;
}

/**
 * The ROSTER half of provisioning: the register people row, created if the register
 * does not already hold this person (matched by e-mail via /v1/people/resolve, so a
 * retry completes rather than colliding with the one-mailbox rule). Returns the row id.
 */
async function ensurePersonRow(emp: Employee): Promise<string | undefined> {
  const email = (emp.email || '').trim();
  if (email) {
    try {
      const got = await api.get<any>('/people/resolve', { name: email });
      if (got?.resolved) return undefined; // already on the roster — nothing to create
    } catch { /* resolve unavailable → fall through to the create, which validates */ }
  }
  try {
    const created = await api.post<any>('/people', {
      role: 'BDRM', ...toPersonBody(emp), name: emp.name, full_name: emp.full || emp.name,
    });
    return created?.id;
  } catch (e: any) {
    const detail = (e?.response?.data ? errText(e.response.data) : '') || e?.message || '';
    throw new Error(
      `The sign-in identity was created, but the person could not be added to the `
      + `register roster${detail ? ` — ${detail}` : ''}. Until they are on the roster, `
      + `no dropdown can offer them. Fix the field and save again to finish.`);
  }
}

/**
 * Reconcile the roster FROM Access (register endpoint): a user created through Postman
 * or a script — any path that never wrote the people table — becomes a roster row the
 * next time anyone opens the Employees screen. Fail-quiet by design: an unreachable
 * register must not break a page that is otherwise reading fine; the register audits
 * every run it does apply.
 */
async function syncFromAccess(): Promise<void> {
  if (!USE_REAL_API) return;
  try {
    const out = await api.post<any>('/internal/people/sync-access');
    if (out?.created?.length || out?.updated?.length) {
      await hydrateRoster();
      void referenceService.hydrate();
    }
  } catch (e) {
    console.warn('[api] roster sync from Access skipped:', e);
  }
}

/**
 * Every ACTIVE person on the roster, as picker strings — for "Reports to" and any other
 * field that may name ANYONE, not just an RM/Analyst bucket. The role-bucketed ref lists
 * deliberately exclude Management/Admin (nobody assigns a deal to the CEO), which made
 * them wrong for the reporting line: a Head reporting to Management found the dropdown
 * empty. Short handle unless two people share it — then the full name, same rule the
 * Register's own pickers use.
 */
function rosterNames(): string[] {
  const people = (db().people as Employee[]).filter((p) => !p.inactive);
  const dupes = new Map<string, number>();
  people.forEach((p) => {
    const h = (p.name || '').trim().toLowerCase();
    if (h) dupes.set(h, (dupes.get(h) || 0) + 1);
  });
  return people
    .map((p) => ((dupes.get((p.name || '').trim().toLowerCase()) || 0) > 1
      ? (p.full || p.name) : (p.name || p.full)))
    .filter((n): n is string => !!n)
    .sort((a, b) => a.localeCompare(b));
}

/** Is this (Access-sourced) employee on the register roster? Matched by e-mail first —
 *  the join key — then by either name, for rows created before e-mails were kept. */
function onRoster(e: Employee): boolean {
  const mail = (e.email || '').trim().toLowerCase();
  return (db().people as Employee[]).some((p: any) =>
    (mail && (p.email || '').trim().toLowerCase() === mail)
    || (p.name || '').trim().toLowerCase() === (e.name || '').trim().toLowerCase()
    || (p.full || '').trim().toLowerCase() === (e.full || '').trim().toLowerCase());
}

/**
 * A leaver's whole book — every lead/deal/tracker naming them (by either of their
 * names) plus their active line assignments — moves to the successor in ONE register
 * call, which answers with counts. Called from the delete dialog before the person is
 * removed, so nothing they owned is ever left orphaned.
 */
async function handover(from: Employee, to: Employee): Promise<Record<string, number>> {
  const out = await api.post<any>('/internal/people/handover', {
    from_person: from.email || from.full || from.name,
    to_person: to.email || to.full || to.name,
    from_user_id: (from as any).accessId || undefined,
    to_user_id: (to as any).accessId || undefined,
  });
  return { ...(out?.moved || {}), assignments: out?.assignments_moved || 0 };
}

export const employeesService = {
  bookRollup,
  hydrateRoster,
  rosterNames,
  syncFromAccess,
  onRoster,
  handover,
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
        const rows: Employee[] = users.map(fromAccessUser);
        // The UNION of both halves. A person can exist as sign-in only (Access, no
        // roster row) or roster only (created via POST /v1/people, no sign-in) — and a
        // grid that lists just one half makes the other invisible until a dropdown or
        // a login mysteriously fails. Roster-only people join the list here, flagged.
        try {
          const roster = await listAll('/people', { key: 'people' });
          const byMail = new Set(rows.map((r) => (r.email || '').toLowerCase()).filter(Boolean));
          const byName = new Set(rows.flatMap((r) => [
            (r.full || '').trim().toLowerCase(), (r.name || '').trim().toLowerCase(),
          ]).filter(Boolean));
          roster.forEach((per: any) => {
            const mail = (per.email || '').trim().toLowerCase();
            if (mail && byMail.has(mail)) return;
            if (byName.has((per.full_name || '').trim().toLowerCase())
                || byName.has((per.name || '').trim().toLowerCase())) return;
            rows.push({
              name: per.name || per.full_name, full: per.full_name || per.name,
              role: per.role || '', username: '', email: per.email || '',
              phone: per.phone || '', geography: per.geography || '',
              sectors: per.sectors || '', startedOn: per.started_on || '',
              reportsTo: per.reports_to || '', inactive: !!per.inactive,
              notes: per.notes || '', registerId: per.id, noSignIn: true,
            } as any as Employee);
          });
        } catch (e) {
          console.warn('[api] roster read for the employees union skipped:', e);
        }
        return applyQuery(rows, { ...q, searchFields });
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
  /**
   * Edit BOTH halves, awaited — in production this screen is the only way people are
   * managed, so an edit here must be the whole truth. The roster patch carries the
   * business fields; the ACCESS patch carries identity and permissions — and above all
   * the Active toggle: deactivating a leaver must revoke their SIGN-IN, not just tidy
   * a directory row. (Edits used to reach the roster only, which left a "deactivated"
   * person still able to log in.)
   */
  async update(name: string, patch: Partial<Employee>, by: string): Promise<void> {
    const p = this.find(name); if (!p) return;
    if (USE_REAL_API) {
      if (p.registerId) {
        await api.patch('/people/' + p.registerId, toPersonBody(patch))
          .catch((e) => { throw new Error('The roster refused the edit: '
            + ((e?.response?.data && errText(e.response.data)) || e?.message || e)); });
      }
      // Resolve the Access identity by id or e-mail. Identity fields travel on the
      // user PATCH; ROLES do not — Access's update schema forbids them (each grant/
      // revoke bumps the permissions epoch and is audited individually), so the role
      // set is reconciled through the dedicated role endpoints instead.
      const accessPatch: Record<string, any> = {};
      const wantRoles = patch.role !== undefined
        ? String(patch.role).split(',').map((r) => r.trim()).filter(Boolean) : undefined;
      if (patch.full !== undefined) accessPatch.full_name = patch.full;
      if (patch.name !== undefined) accessPatch.short_name = patch.name;
      if (patch.phone !== undefined) accessPatch.phone = patch.phone;
      if (patch.inactive !== undefined) accessPatch.is_active = !patch.inactive;
      if (Object.keys(accessPatch).length || wantRoles) {
        let id = (p as any).accessId as string | undefined;
        let heldRoles: string[] | undefined;
        const mail = (p.email || patch.email || '').trim().toLowerCase();
        if (mail && (!id || wantRoles)) {
          const hits = await accessService.findUsers(mail).catch(() => []);
          const hit = hits.find((u) => (u.email || '').toLowerCase() === mail);
          id = id || hit?.id;
          heldRoles = hit?.roles;
        }
        if (id) {
          if (Object.keys(accessPatch).length) await accessService.updateUser(id, accessPatch);
          if (wantRoles) await accessService.setRoles(id, wantRoles, heldRoles ?? []);
        } else if (accessPatch.is_active === false) {
          // Refusing to pretend: a deactivation that cannot reach the sign-in is not done.
          throw new Error('Could not find this person\'s sign-in identity in Access — '
            + 'their access was NOT revoked. Check their e-mail and try again.');
        }
      }
    }
    Object.assign(p, patch); writeAudit(by, 'Employee updated', name, Object.keys(patch).join(','));
  },
  // Adding an employee is a PROVISIONING act with TWO halves, both awaited:
  //
  //   Access  — the sign-in identity; owns the roles that drive RBAC.
  //   Register people — the roster every name resolves against. The BDRM/RM/Analyst
  //     dropdowns are served live from it (/v1/ref), conversions validate rm/analyst
  //     against it, and VocX binds captures through it. This half was simply MISSING:
  //     an added employee could sign in, but no dropdown offered them and no lead
  //     could name them — the "unable to select BDRM" dead end.
  //
  // Await both; a duplicate e-mail or rejected role must reach the user, not
  // console.warn. Each half is idempotent-by-lookup so a retry after a partial
  // failure completes the other half instead of failing on "already exists".
  async create(input: Partial<Employee>, by: string): Promise<Employee> {
    const emp: Employee = { name: input.name || 'New', full: input.full || input.name || 'New', role: input.role || 'BDRM', username: '', email: '', phone: '', geography: '', sectors: '', startedOn: '', reportsTo: '', inactive: false, notes: '', ...input } as Employee;
    if (USE_REAL_API) {
      const email = (emp.email || '').trim().toLowerCase();
      try {
        const created = await accessService.createUser(toAccessUser(emp));
        emp.accessId = created.id;
      } catch (e: any) {
        // Retry after a partial failure: if Access already holds this mailbox, attach
        // to that identity rather than refusing to finish the roster half.
        const existing = email
          ? (await accessService.findUsers(email).catch(() => []))
              .find((u) => (u.email || '').toLowerCase() === email)
          : undefined;
        if (!existing) throw e;
        emp.accessId = existing.id;
      }
      emp.registerId = await ensurePersonRow(emp);
      // The new name reaches the BDRM/RM dropdowns through /v1/ref — refresh them now,
      // so the person is offerable the moment the dialog closes, not after a reload.
      void referenceService.hydrate();
    }
    db().people.push(emp); writeAudit(by, 'Employee added', emp.name, emp.full); return emp;
  },
  /**
   * "Delete" an employee = REVOKE their sign-in and retire their roster row — never a
   * hard erase. Approvals, decisions and assignments cite the Access user id, and
   * "who approved this?" must still resolve years after the person left; the register
   * soft-deletes for the same reason. The Access revocation comes FIRST and is
   * awaited: removing a leaver whose door stays open is the failure that matters.
   */
  async remove(name: string, by: string): Promise<void> {
    const p = this.find(name);
    if (USE_REAL_API && p) {
      const mail = (p.email || '').trim().toLowerCase();
      let id = (p as any).accessId as string | undefined;
      if (!id && mail) {
        const hits = await accessService.findUsers(mail).catch(() => []);
        id = hits.find((u) => (u.email || '').toLowerCase() === mail)?.id;
      }
      if (id) await accessService.updateUser(id, { is_active: false });
      else if (mail) {
        throw new Error('Could not find this person\'s sign-in identity in Access — '
          + 'their access was NOT revoked, so nothing was removed.');
      }
      if (p.registerId) await api.del('/people/' + p.registerId);
    }
    const i = db().people.findIndex((x: Employee) => x.name === name);
    if (i > -1) { db().people.splice(i, 1); writeAudit(by, 'Employee deleted', name, ''); }
  },
};
