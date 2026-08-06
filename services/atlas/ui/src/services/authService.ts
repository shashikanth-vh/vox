// Login against PRISM, built from api.json (folders 00b/00c + 01) and env.json.
//
// PRISM exposes NO /auth/login endpoint. Signing in is two steps:
//
//   1. Obtain an identity.
//      · prod posture — POST {dexUrl}/dex/token, an OIDC *password grant*. The gateway
//        validates the ID TOKEN, so id_token (not access_token) is what we keep.
//      · Google-issuer deployments — an id_token from Google; folder 00c gets one with a
//        refresh grant, which is a server-side flow (it needs the client SECRET and must
//        never run in a browser). In the app, hand a Google-issued id_token to
//        signInWithIdToken() instead.
//      · dev posture — dexUrl is empty; there is no sign-in request at all and the
//        gateway trusts X-User-Email / X-User-Roles. The password is not checked by
//        anyone, so this posture is for local development only.
//
//   2. Resolve who that identity IS, from Access:
//        GET /access/v1/users?q=<email>   -> { id, email, full_name, roles[] }
//        GET /access/v1/resolve?email=<email> -> effective view matrix { views: {...} }
//      Access role names are the same vocabulary as rbac.ts ROLES, so they need no
//      translation.

import axios, { type AxiosRequestConfig } from 'axios';
import {
  ACCESS_URL, DEX_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
  GOOGLE_TOKEN_URL, OIDC_CLIENT_ID, TENANT, USE_REAL_API,
} from '../api/axiosClient';
import { authHeaders, clearSession, getSession, setSession, type PrismSession } from '../auth/session';

/** A user row as Access returns it (`GET /access/v1/users`). */
export interface AccessUser {
  id: string;
  email: string;
  full_name?: string;
  short_name?: string;
  phone?: string;
  is_active?: boolean;
  roles?: string[];
}

/** The effective matrix from `GET /access/v1/resolve` — e.g. { views: { leads: 'SCOPED' } }. */
export interface ResolvedAccess {
  views?: Record<string, string>;
  [k: string]: any;
}

/** Sign-in failures the UI can render verbatim. */
export class AuthError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = 'AuthError';
  }
}

/** True when the deployment expects a real OIDC sign-in rather than header trust. */
export const isOidcPosture = (): boolean => Boolean(DEX_URL);

// Access and Dex sit on other origins than VITE_API_BASE_URL, so they get a bare client
// rather than axiosClient (whose baseURL and identity interceptor do not apply).
const raw = axios.create({ timeout: 15000 });

function requireAccessUrl(): string {
  if (!ACCESS_URL) {
    throw new AuthError('Access service URL is not configured — set VITE_PRISM_BASE_URL (or VITE_ACCESS_URL) in .env.');
  }
  return ACCESS_URL;
}

/**
 * Step 1 (prod posture): OIDC password grant against Dex.
 * Mirrors "POST /dex/token — sign in as ADMIN" exactly, including the urlencoded body
 * and the openid/email/profile scope.
 */
export async function dexPasswordGrant(email: string, password: string): Promise<string> {
  const body = new URLSearchParams({
    grant_type: 'password',
    client_id: OIDC_CLIENT_ID,
    scope: 'openid email profile',
    username: email,
    password,
  });
  try {
    const { data } = await raw.post<{ id_token?: string; access_token?: string }>(
      `${DEX_URL}/dex/token`,
      body,
      { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
    );
    // The gateway validates the ID token; an access_token alone will be rejected.
    if (!data?.id_token) throw new AuthError('Sign-in succeeded but returned no id_token — check the Dex client configuration.');
    return data.id_token;
  } catch (e: any) {
    if (e instanceof AuthError) throw e;
    const status = e?.response?.status;
    if (status === 400 || status === 401) throw new AuthError('Incorrect email or password.', status);
    throw new AuthError(`Cannot reach the sign-in service at ${DEX_URL}.`, status);
  }
}

/**
 * Step 1 (Google-issuer deployments): folder 00c's REFRESH GRANT, run from the browser.
 * POSTs exactly what the collection posts —
 *   grant_type=refresh_token, client_id, client_secret, refresh_token
 * — and keeps the id_token, which is what the gateway validates.
 *
 * The refresh token belongs to ONE account (00c's adminRefreshToken), so this is a fixed
 * identity, not a per-user Google sign-in: whoever clicks the button becomes that user.
 * The client_secret rides along in the bundle, which is why the collection keeps it in a
 * vault. Both are deliberate here.
 *
 * Kept a CORS-simple request on purpose: urlencoded body, no custom headers, so the
 * browser sends no preflight that Google would have to answer.
 */
export async function googleRefreshGrant(): Promise<string> {
  if (!GOOGLE_CLIENT_ID || !GOOGLE_CLIENT_SECRET || !GOOGLE_REFRESH_TOKEN) {
    throw new AuthError(
      'Google sign-in is not configured — set VITE_GOOGLE_CLIENT_ID, VITE_GOOGLE_CLIENT_SECRET and VITE_GOOGLE_REFRESH_TOKEN in .env (googleClientId / googleClientSecret / adminRefreshToken from env.json).',
    );
  }
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    client_id: GOOGLE_CLIENT_ID,
    client_secret: GOOGLE_CLIENT_SECRET,
    refresh_token: GOOGLE_REFRESH_TOKEN,
  });
  try {
    const { data } = await raw.post<{ id_token?: string; access_token?: string }>(
      GOOGLE_TOKEN_URL,
      body,
      { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
    );
    // A refresh grant returns an id_token only when the ORIGINAL consent asked for the
    // openid scope; without it Google answers 200 with an access_token and nothing we
    // can sign in with.
    if (!data?.id_token) {
      throw new AuthError('Google returned no id_token — the consent that minted this refresh token did not include the "openid" scope. Re-consent with scope=openid email profile.');
    }
    return data.id_token;
  } catch (e: any) {
    if (e instanceof AuthError) throw e;
    const status: number | undefined = e?.response?.status;
    const code: string | undefined = e?.response?.data?.error;
    const detail: string | undefined = e?.response?.data?.error_description;
    if (code === 'invalid_grant') {
      throw new AuthError('Google rejected the refresh token (invalid_grant) — it has expired, been revoked, or was issued to a different client. A Testing-mode OAuth consent screen expires refresh tokens after 7 days.', status);
    }
    if (code === 'invalid_client') {
      throw new AuthError('Google rejected the client (invalid_client) — check VITE_GOOGLE_CLIENT_ID and VITE_GOOGLE_CLIENT_SECRET.', status);
    }
    if (code) throw new AuthError(`Google refused the token request: ${code}${detail ? ` — ${detail}` : ''}`, status);
    // No response at all: network, a content blocker, or the browser refusing the origin.
    if (!status) throw new AuthError(`Could not reach ${GOOGLE_TOKEN_URL} from the browser — check the network, an ad/tracker blocker, or set VITE_GOOGLE_TOKEN_URL to the dev-server proxy path to keep the call same-origin.`);
    throw new AuthError(`Google sign-in failed (HTTP ${status}).`, status);
  }
}

/** Step 2a: find the Access user for an e-mail. Exact-matched, since ?q= is a search. */
export async function fetchAccessUser(email: string, idToken: string): Promise<AccessUser> {
  const cfg: AxiosRequestConfig = { headers: identityFor(email, idToken), params: { q: email } };
  let rows: AccessUser[];
  try {
    const { data } = await raw.get<AccessUser[]>(`${requireAccessUrl()}/v1/users`, cfg);
    rows = Array.isArray(data) ? data : [];
  } catch (e: any) {
    const status = e?.response?.status;
    if (status === 401 || status === 403) throw new AuthError('That identity is not permitted to sign in.', status);
    throw new AuthError(`Cannot reach the Access service at ${ACCESS_URL}.`, status);
  }
  const user = rows.find((r) => (r.email || '').toLowerCase() === email.toLowerCase());
  if (!user) throw new AuthError(`No ATLAS user is provisioned for ${email}.`, 404);
  if (user.is_active === false) throw new AuthError(`The account for ${email} is deactivated.`, 403);
  return user;
}

/** Step 2b: the effective view matrix. Advisory — a failure here must not block sign-in. */
export async function fetchResolved(email: string, idToken: string, roles: string[]): Promise<ResolvedAccess | null> {
  try {
    const { data } = await raw.get<ResolvedAccess>(`${requireAccessUrl()}/v1/resolve`, {
      headers: identityFor(email, idToken, roles),
      params: { email },
    });
    return data ?? null;
  } catch (e) {
    console.warn('[auth] /access/v1/resolve unavailable — falling back to the local RBAC matrix:', e);
    return null;
  }
}

// Identity headers for the login calls themselves. Roles are deliberately omitted on the
// first lookup: they are what we are asking Access for, and asserting a guess (the
// collection hard-codes "Admin" because it runs as the admin) would be a privilege claim
// the app has no business making.
function identityFor(email: string, idToken: string, roles: string[] = []): Record<string, string> {
  return authHeaders({ email, tenant: TENANT, idToken, roles });
}

/** Turn a verified identity into a session by resolving it against Access. */
async function establish(email: string, idToken: string): Promise<PrismSession> {
  const user = await fetchAccessUser(email, idToken);
  const roles = user.roles ?? [];
  const resolved = await fetchResolved(user.email, idToken, roles);
  const session: PrismSession = {
    email: user.email,
    tenant: TENANT,
    idToken,
    userId: user.id,
    fullName: user.full_name,
    shortName: user.short_name,
    roles,
    views: resolved?.views,
  };
  setSession(session);
  return session;
}

export const authService = {
  /**
   * Credentialed sign-in. In the OIDC posture the password is verified by Dex; in the
   * dev posture there is nothing to verify it against, and identity rests on the Access
   * lookup plus header trust.
   */
  async signIn(email: string, password: string): Promise<PrismSession> {
    const id = email.trim();
    if (!id || !password) throw new AuthError('Username / email and password are both required.');
    const idToken = isOidcPosture() ? await dexPasswordGrant(id, password) : '';
    return establish(id, idToken);
  },

  /**
   * Sign in with an id_token minted elsewhere — Google Identity Services in the browser,
   * or any OIDC issuer the gateway trusts. The e-mail is read from the token's claims.
   */
  async signInWithIdToken(idToken: string): Promise<PrismSession> {
    const email = emailFromIdToken(idToken);
    if (!email) throw new AuthError('That sign-in token carries no e-mail claim — request the "email" scope.');
    return establish(email, idToken);
  },

  /**
   * "Continue with Google": folder 00c's refresh grant straight to Google, then the same
   * Access resolution as every other posture. No username/password is involved or checked
   * — the refresh token IS the credential.
   */
  async signInWithGoogle(): Promise<PrismSession> {
    const idToken = await googleRefreshGrant();
    const email = emailFromIdToken(idToken);
    if (!email) throw new AuthError('The Google id_token carries no e-mail claim — re-consent with the "email" scope.');
    return establish(email, idToken);
  },

  /**
   * PER-USER Google sign-in: the GIS button hands over the id_token Google minted for
   * whoever just picked their account. The gateway verifies it (accounts.google.com in
   * the accepted issuers) and Access decides their roles — a valid Google account that
   * was never provisioned signs in to nothing.
   */
  async signInWithGoogleCredential(credential: string): Promise<PrismSession> {
    if (!credential) throw new AuthError('Google returned no credential — try again.');
    const email = emailFromIdToken(credential);
    if (!email) throw new AuthError('The Google credential carries no e-mail claim.');
    return establish(email, credential);
  },

  /** True when a client_id, secret and refresh token are all present. */
  isGoogleConfigured: (): boolean =>
    Boolean(GOOGLE_CLIENT_ID && GOOGLE_CLIENT_SECRET && GOOGLE_REFRESH_TOKEN),

  /** Re-resolve roles/views for the current session (e.g. after an Access change). */
  async refresh(): Promise<PrismSession | null> {
    const s = getSession();
    return s ? establish(s.email, s.idToken) : null;
  },

  current: getSession,
  signOut(): void {
    clearSession();
  },
  /** Real sign-in is attempted only when the app is pointed at a live backend. */
  isLive: (): boolean => USE_REAL_API,
};

/** Read the `email` claim out of a JWT payload. No signature check — the gateway verifies. */
function emailFromIdToken(idToken: string): string {
  try {
    const payload = idToken.split('.')[1];
    const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
    const claims = JSON.parse(decodeURIComponent(escape(json)));
    return claims.email || claims.preferred_username || '';
  } catch {
    return '';
  }
}
