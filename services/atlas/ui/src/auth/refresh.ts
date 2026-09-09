// Session recovery — the difference between "quietly broken" and honest.
//
// Google id_tokens live ~an hour. The proactive renewal in AuthContext covers the
// common case, but a laptop asleep at the renewal moment, a network blip, or a
// blocked accounts.google.com leaves the tab holding an EXPIRED token — and before
// this module, every live call then 401'd into the mock fallback: grids silently
// served the stale local book while the app still looked signed in.
//
// The contract now: any 401 on a live call triggers ONE silent re-mint (shared
// across every concurrent 401 — a burst of grid loads costs one Google round trip),
// the failed request is retried with the fresh token, and if Google will not renew
// silently the session is cleared and the app lands on the login screen. Two honest
// outcomes; no third state where the UI pretends.
import { clearSession, getSession } from './session';

const SIGNED_OUT_EVENT = 'prism:session-expired';
let inflight: Promise<string | null> | null = null;

/** AuthContext subscribes so a forced sign-out flips React state to the login screen. */
export function onForcedSignOut(fn: () => void): () => void {
  window.addEventListener(SIGNED_OUT_EVENT, fn);
  return () => window.removeEventListener(SIGNED_OUT_EVENT, fn);
}

export function forceSignOut(): void {
  clearSession();
  window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
}

/**
 * Re-mint the session's id_token without interaction. Single-flight; resolves null
 * when there is nothing to renew (header-trust posture, non-Google token) or Google
 * declines. A success re-runs the full sign-in, so roles/views are re-resolved and
 * the stored session — which every request's auth header reads — is replaced.
 */
export async function refreshIdToken(): Promise<string | null> {
  const s = getSession();
  const clientId = import.meta.env.VITE_GOOGLE_SSO_CLIENT_ID || '';
  if (!s?.idToken || !clientId) return null;
  if (!inflight) {
    inflight = (async () => {
      try {
        const g = await import('./googleIdentity');
        if (!g.isGoogleToken(s.idToken)) return null;
        const fresh = await g.silentReauth(clientId, s.email);
        if (!fresh) return null;
        const { authService } = await import('../services/authService');
        const ns = await authService.signInWithGoogleCredential(fresh);
        return ns.idToken || null;
      } catch {
        return null;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

/**
 * Attach the 401 lane to an axios client that carries the session's auth headers.
 * Not for the sign-in clients themselves — a wrong password's 401 must stay a
 * wrong password, not trigger a recovery loop.
 */
export function attach401Recovery(client: {
  interceptors: { response: { use: (ok: any, bad: any) => void } };
  request: (cfg: any) => Promise<any>;
}): void {
  client.interceptors.response.use(
    (res: unknown) => res,
    async (err: any) => {
      const cfg = err?.config;
      if (err?.response?.status === 401 && cfg && !cfg.__authRetried
          && getSession()?.idToken) {
        const token = await refreshIdToken();
        if (token) {
          cfg.__authRetried = true;   // one recovery per request, never a loop
          return client.request(cfg); // request interceptor re-stamps the new bearer
        }
        forceSignOut();
      }
      return Promise.reject(err);
    },
  );
}
