// The signed-in PRISM session — the single source of truth for the identity that
// every outbound request carries.
//
// PRISM (see api.json) accepts identity in two postures and the app must satisfy both:
//   dev  — "header trust": the gateway believes X-Tenant / X-User-Email / X-User-Roles
//          and no bearer is required (env.json ships dexUrl = "" for exactly this).
//   prod — REQUIRE_AUTH + an OIDC issuer: identity comes ONLY from a verified bearer,
//          and the gateway validates the ID TOKEN (not the access token).
// Carrying both at once is safe: the prod gateway ignores the headers it does not trust.

import { ROLE_RANK, primaryRole, type Role } from './rbac';

export interface PrismSession {
  email: string;
  tenant: string;
  /** OIDC id_token. Empty string in the dev posture, where header trust applies. */
  idToken: string;
  /** Access user id (`GET /access/v1/users`), used as the actor on writes. */
  userId?: string;
  fullName?: string;
  shortName?: string;
  /** Raw role names as Access returns them — the same vocabulary as rbac.ts ROLES. */
  roles: string[];
  /** Effective view matrix from `GET /access/v1/resolve` (e.g. { leads: 'SCOPED' }). */
  views?: Record<string, string>;
}

// sessionStorage, not localStorage: a bearer in localStorage outlives the browsing
// session and is readable by any injected script on the origin. Per-tab and cleared on
// close is the weaker-blast-radius default; move this to an httpOnly cookie the day the
// gateway issues one.
const KEY = 'atlas.session';

let current: PrismSession | null = null;

export function getSession(): PrismSession | null {
  if (current) return current;
  try {
    const raw = sessionStorage.getItem(KEY);
    current = raw ? (JSON.parse(raw) as PrismSession) : null;
  } catch {
    current = null;
  }
  return current;
}

export function setSession(s: PrismSession | null): void {
  current = s;
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* private mode / storage disabled — the in-memory copy still serves this tab */
  }
}

export function clearSession(): void {
  setSession(null);
}

/**
 * The single highest-privilege role held, by ROLE_RANK — the gateway takes ONE role, not
 * a comma-separated stack. An Admin+Management user asserts "Admin".
 *
 * This narrows only what we ASSERT on the wire. In-app permission checks still use the
 * full `roles` array (rbac.ts unions them), which is the same result whenever the highest
 * role dominates — and it does across the whole matrix except `audit`/`activity`, where
 * Admin is the higher role anyway.
 *
 * Roles Access returns that aren't in the local vocabulary are unrankable, so they fall
 * back to first-listed rather than being dropped.
 */
export function topRole(roles: string[]): string {
  const known = roles.filter((r) => r in ROLE_RANK) as Role[];
  return known.length ? primaryRole(known) : roles[0];
}

/**
 * The identity headers every PRISM request carries, exactly as the collection sends them.
 * Returns {} when signed out so unauthenticated calls stay anonymous rather than
 * asserting a half-built identity.
 */
export function authHeaders(s: PrismSession | null = getSession()): Record<string, string> {
  if (!s) return {};
  const h: Record<string, string> = {
    'X-Tenant': s.tenant,
    // 'X-Actor': s.email,
    // 'X-User-Email': s.email,
  };
  // if (s.roles.length) h['X-User-Roles'] = topRole(s.roles);
  if (s.idToken) h.Authorization = `Bearer ${s.idToken}`;
  return h;
}
