// The Access plane — user provisioning, `{{accessUrl}}/v1/users`.
//
// Access sits behind the same NGINX door as everything else but on its own prefix, so it
// does NOT go through axiosClient (whose baseURL is VITE_API_BASE_URL). It gets its own
// instance here, sharing the one thing that matters: the identity headers.
//
// The collection sends these by hand —
//   X-Tenant / X-Actor / X-User-Email / X-User-Roles / Authorization: Bearer …
// — which is what authHeaders() emits from the live session, with one override:
// X-Actor is the fixed provisioning actor Access expects on this plane (e2e-runner),
// NOT the signed-in user. X-User-Email still carries who is actually driving, so the
// identity is not lost — see ACCESS_ACTOR below.

import axios from 'axios';
import { ACCESS_URL } from '../api/axiosClient';
import { errText } from '../api/http';
import { authHeaders } from '../auth/session';
import type { AccessUser } from './authService';
import { AuthError } from './authService';
import type { Employee } from '../pages/Employees/employee.types';

/** The actor Access attributes writes on this plane to. Overridable without a code change. */
export const ACCESS_ACTOR: string = import.meta.env.VITE_ACCESS_ACTOR || 'e2e-runner';

const access = axios.create({ timeout: 15000, headers: { 'Content-Type': 'application/json' } });

// Read per-request, not at module load, so a sign-in/sign-out takes effect immediately.
access.interceptors.request.use((cfg) => {
  Object.entries(authHeaders()).forEach(([k, v]) => cfg.headers.set(k, v));
  cfg.headers.set('X-Actor', ACCESS_ACTOR);
  return cfg;
});

/** The create body Access accepts on POST /v1/users. */
export interface AccessUserInput {
  email: string;
  full_name: string;
  short_name?: string;
  phone?: string | null;
  is_active?: boolean;
  /** Real Access field, but NOT sent on create — see toAccessUser. */
  reports_to?: string | null;
  notes?: string | null;
  /** Free-form bag for the ATLAS-only fields Access has no column for. */
  meta?: Record<string, any> | null;
  roles?: string[];
}

function requireAccessUrl(): string {
  if (!ACCESS_URL) {
    throw new AuthError('Access service URL is not configured — set VITE_PRISM_BASE_URL (or VITE_ACCESS_URL) in .env.');
  }
  return ACCESS_URL;
}

// Access carries first-class columns for email/full_name/short_name/phone/is_active/
// notes/roles (see GET /v1/users). The remaining ATLAS form fields — geography, sector
// specialisation, started-on, username — have no column, so they ride in `meta` rather
// than being dropped on the floor.
//
// reports_to is deliberately NOT sent. It exists on the Access record, but the form holds
// a display name ("Priya") while Access keys users by id, so asserting it here would be a
// guess. The reporting line stays on the local employee record until there is a
// name -> id resolution step (accessService.findUsers is the seam).
export function toAccessUser(e: Partial<Employee>): AccessUserInput {
  const roles = (e.role || '').split(',').map((s) => s.trim()).filter(Boolean);
  const meta: Record<string, any> = {};
  if (e.geography?.trim()) meta.geography = e.geography.trim();
  if (e.sectors?.trim()) meta.sectors = e.sectors.trim();
  if (e.startedOn?.trim()) meta.started_on = e.startedOn.trim();
  if (e.username?.trim()) meta.username = e.username.trim();

  return {
    email: (e.email || '').trim(),
    full_name: (e.full || '').trim(),
    short_name: (e.name || '').trim() || undefined,
    phone: e.phone?.trim() || null,
    is_active: !e.inactive,
    notes: e.notes?.trim() || null,
    meta: Object.keys(meta).length ? meta : null,
    roles,
  };
}

// The inverse of toAccessUser — an Access record read back as an ATLAS employee row.
// `roles` is re-joined into the comma-separated `role` string the grid and the dialog's
// multi-select both speak, and `meta` is unpacked back into its four columns.
export function fromAccessUser(u: AccessUser & Record<string, any>): Employee {
  const meta = (u.meta || {}) as Record<string, any>;
  return {
    accessId: u.id,
    // Access allows a null short_name; the grid keys on `name`, so fall back the same way
    // sign-in does (AuthContext.userFromSession) rather than rendering a blank row.
    name: u.short_name || u.full_name || (u.email || '').split('@')[0],
    full: u.full_name || u.email || '',
    role: (u.roles || []).join(', '),
    email: u.email || '',
    phone: u.phone || '',
    inactive: u.is_active === false,
    notes: u.notes || '',
    reportsTo: u.reports_to || '',
    geography: meta.geography || '',
    sectors: meta.sectors || '',
    startedOn: meta.started_on || '',
    username: meta.username || '',
  };
}

/** Turn an Access failure into a message the dialog can render verbatim. */
function accessError(e: any, what: string): AuthError {
  if (e instanceof AuthError) return e;
  const status = e?.response?.status;
  const asText = errText(e?.response?.data);
  // The full body always reaches devtools — the alert only has room for the summary.
  if (e?.response) console.warn('[access] %s failed (%s):', what, status, e.response.data);
  if (status === 401 || status === 403) return new AuthError(asText || `Not permitted to ${what}.`, status);
  if (status === 409) return new AuthError(asText || 'A user with that email already exists.', status);
  if (status === 422 || status === 400) return new AuthError(asText || `Access rejected the ${what} request.`, status);
  if (status) return new AuthError(asText || `Access returned ${status} on ${what}.`, status);
  return new AuthError(`Cannot reach the Access service at ${ACCESS_URL}.`);
}

export const accessService = {
  /** POST {{accessUrl}}/v1/users — provision a user. Throws AuthError on failure. */
  async createUser(input: AccessUserInput): Promise<AccessUser> {
    try {
      const { data } = await access.post<AccessUser>(`${requireAccessUrl()}/v1/users`, input);
      return data;
    } catch (e) {
      throw accessError(e, 'create this user');
    }
  },

  /** PATCH {{accessUrl}}/v1/users/:id — Access is keyed by id, not by short name.
   *  Identity fields ONLY: the update schema deliberately forbids `roles` (a role
   *  change bumps the permissions epoch and is audited per grant) — use setRoles. */
  async updateUser(id: string, patch: Partial<AccessUserInput>): Promise<AccessUser> {
    try {
      const { data } = await access.patch<AccessUser>(`${requireAccessUrl()}/v1/users/${id}`, patch);
      return data;
    } catch (e) {
      throw accessError(e, 'update this user');
    }
  },

  /**
   * Reconcile a user's role set through Access's dedicated role endpoints
   * (POST /v1/users/:id/roles, DELETE /v1/users/:id/roles/:role) — grants what is
   * missing, revokes what is no longer held. 409 on grant ("already holds") and 404
   * on revoke ("does not hold") are SUCCESS for a reconcile, so a retry after a
   * partial failure completes instead of failing on the half that already landed.
   */
  async setRoles(id: string, want: string[], current: string[]): Promise<void> {
    const wantSet = new Set(want);
    const haveSet = new Set(current);
    for (const r of want) {
      if (haveSet.has(r)) continue;
      try {
        await access.post(`${requireAccessUrl()}/v1/users/${id}/roles`, { role: r });
      } catch (e: any) {
        if (e?.response?.status !== 409) throw accessError(e, `grant the role '${r}'`);
      }
    }
    for (const r of current) {
      if (wantSet.has(r)) continue;
      try {
        await access.delete(`${requireAccessUrl()}/v1/users/${id}/roles/${encodeURIComponent(r)}`);
      } catch (e: any) {
        if (e?.response?.status !== 404) throw accessError(e, `revoke the role '${r}'`);
      }
    }
  },

  /**
   * GET {{accessUrl}}/v1/users — the same endpoint the sign-in flow searches, listed.
   * `q` is optional: omitted it returns the tenant's users, which is what the Employees
   * grid wants. The response is a bare ARRAY (no {rows,total} envelope), so paging and
   * sorting stay client-side in employeesService.
   */
  async listUsers(q?: string): Promise<AccessUser[]> {
    try {
      const { data } = await access.get<AccessUser[]>(`${requireAccessUrl()}/v1/users`, {
        params: q ? { q } : undefined,
      });
      return Array.isArray(data) ? data : [];
    } catch (e) {
      throw accessError(e, 'load users');
    }
  },

  /** GET {{accessUrl}}/v1/users?q= — a targeted search (e.g. resolving a name to an id). */
  async findUsers(q: string): Promise<AccessUser[]> {
    try {
      const { data } = await access.get<AccessUser[]>(`${requireAccessUrl()}/v1/users`, { params: { q } });
      return Array.isArray(data) ? data : [];
    } catch (e) {
      throw accessError(e, 'look up users');
    }
  },
};
