import { useEffect, createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { ROLES, primaryRole, type Role } from './rbac';
import { db } from '../api/atlasStore';
import { GOOGLE_SSO_CLIENT_ID } from '../api/axiosClient';
import { onForcedSignOut } from './refresh';
import { authService } from '../services/authService';
import { getSession, type PrismSession } from './session';
import { api, USE_REAL_API } from '../api/http';

export interface AppUser {
  name: string;
  full: string;
  // `roles` is the full stack (a user may hold several); `role` is the highest-privilege
  // one, used for labels and defaults. Permission checks pass `roles`.
  roles: Role[];
  role: Role;
}

// Employee.role may carry several comma-separated roles (role stacking). Parse + validate.
function parseRoles(p: any): Role[] {
  const raw: string[] = Array.isArray(p.roles) ? p.roles : String(p.role || '').split(',');
  const valid = raw.map((s) => s.trim()).filter((s) => (ROLES as readonly string[]).includes(s)) as Role[];
  return valid.length ? valid : ['BDRM'];
}

// A PRISM session is already carrying Access role names, which use the same vocabulary as
// ROLES. `name` is the short handle the row-scoping rules match on (rbac.scopeFor), so it
// falls back to the local part of the e-mail when Access has no short_name.
function userFromSession(s: PrismSession): AppUser {
  const roles = parseRoles(s);
  return {
    name: s.shortName || s.fullName || s.email.split('@')[0],
    full: s.fullName || s.email,
    roles,
    role: primaryRole(roles),
  };
}

interface AuthCtx {
  user: AppUser;
  users: AppUser[];
  authed: boolean;
  /** The live PRISM session — null in mock mode. */
  session: PrismSession | null;
  setUserByFull: (full: string) => void;
  /** Credentialed sign-in. Rejects with an AuthError whose message is user-facing. */
  signIn: (email?: string, password?: string) => Promise<void>;
  /** Sign in with an id_token from Google Identity Services / another trusted issuer. */
  signInWithIdToken: (idToken: string) => Promise<void>;
  /** Sign in via folder 00c's Google refresh grant. No credentials are involved. */
  signInWithGoogle: () => Promise<void>;
  signInWithGoogleCredential: (credential: string) => Promise<void>;
  signOut: () => void;
}

const Ctx = createContext<AuthCtx | null>(null);

/** Tell the register a person signed in.
 *
 * Tokens are issued by the IdP (Dex / Google), OUTSIDE PRISM — so a sign-in leaves no
 * trace in the register and the Activity Log could never show "Signed in to ATLAS", the
 * one row that says who was even here. The register exposes /v1/session-events for
 * exactly this, and records it as the CALLER's own audited row (you cannot file one for
 * someone else). Fire-and-forget: a failed bookkeeping call must never fail a login. */
function recordSignIn(): void {
  if (!USE_REAL_API) return;
  void api.post('/session-events', { event: 'signin' }).catch(() => { /* bookkeeping only */ });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const users: AppUser[] = db().people.map((p: any) => {
    const roles = parseRoles(p);
    return { name: p.name, full: p.full, roles, role: primaryRole(roles) };
  });
  // Login always starts as Admin (Kannan) in mock mode. A live build's store has no
  // people until sign-in, so this must still yield a real AppUser — the context promises
  // `user` is never undefined, and pre-login renders do read it.
  const adminUser: AppUser = users.find((u) => u.roles.includes('Admin')) ?? users[0]
    ?? { name: '', full: '', roles: ['BDRM'], role: 'BDRM' };

  // A session surviving in sessionStorage (same tab, e.g. a reload) signs straight back in.
  const restored = authService.isLive() ? getSession() : null;
  const [session, setSession] = useState<PrismSession | null>(restored);
  const [user, setUser] = useState<AppUser>(restored ? userFromSession(restored) : adminUser);
  const [authed, setAuthed] = useState(Boolean(restored));

  // Google id_tokens live ~1 hour. Five minutes before expiry, quietly re-mint one
  // (Google auto-selects the signed-in account — no interaction) so a working
  // afternoon never sees a surprise sign-out. One attempt used to be all it got —
  // a laptop asleep at that moment, or one network blip, and the tab ran on an
  // expired token for good. Now it retries a couple of times a minute apart; if
  // Google still will not renew, the 401 lane (auth/refresh.ts) recovers on the
  // next call or signs out honestly.
  useEffect(() => {
    if (!session?.idToken || !GOOGLE_SSO_CLIENT_ID) return;
    let alive = true;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const later = (fn: () => void, ms: number) => { timers.push(setTimeout(fn, ms)); };
    void (async () => {
      const g = await import('./googleIdentity');
      if (!alive || !g.isGoogleToken(session.idToken)) return;
      const left = g.tokenSecondsLeft(session.idToken);
      if (left == null) return;
      const attempt = async (triesLeft: number) => {
        if (!alive) return;
        const fresh = await g.silentReauth(GOOGLE_SSO_CLIENT_ID, session.email);
        if (!alive) return;
        if (fresh) {
          try {
            const s = await authService.signInWithGoogleCredential(fresh);
            if (alive) { setSession(s); setUser(userFromSession(s)); }
            return;   // success re-arms via the [session.idToken] dependency
          } catch { /* fall through to a retry */ }
        }
        if (triesLeft > 0) later(() => void attempt(triesLeft - 1), 60_000);
      };
      later(() => void attempt(2), Math.max(5, left - 300) * 1000);
    })();
    return () => { alive = false; timers.forEach(clearTimeout); };
  }, [session?.idToken]);

  // The 401 lane clears the stored session when Google will not renew silently;
  // this flips the React state so the login screen actually appears.
  useEffect(() => onForcedSignOut(() => { setSession(null); setAuthed(false); }), []);

  const value = useMemo<AuthCtx>(() => ({
    user,
    users,
    authed,
    session,
    setUserByFull: (full) => setUser(users.find((u) => u.full === full) ?? users[0]),
    signIn: async (email, password) => {
      // Mock mode keeps the offline behaviour: no network, land on the Admin default so
      // login never resumes a previously-selected role.
      if (!authService.isLive()) {
        setUser(adminUser); setSession(null); setAuthed(true);
        return;
      }
      const s = await authService.signIn(email ?? '', password ?? '');
      setSession(s); setUser(userFromSession(s)); setAuthed(true); recordSignIn();
    },
    signInWithIdToken: async (idToken) => {
      if (!authService.isLive()) { setUser(adminUser); setSession(null); setAuthed(true); return; }
      const s = await authService.signInWithIdToken(idToken);
      setSession(s); setUser(userFromSession(s)); setAuthed(true); recordSignIn();
    },
    signInWithGoogle: async () => {
      if (!authService.isLive()) { setUser(adminUser); setSession(null); setAuthed(true); return; }
      const s = await authService.signInWithGoogle();
      setSession(s); setUser(userFromSession(s)); setAuthed(true); recordSignIn();
    },
    signInWithGoogleCredential: async (credential) => {
      if (!authService.isLive()) { setUser(adminUser); setSession(null); setAuthed(true); return; }
      const s = await authService.signInWithGoogleCredential(credential);
      setSession(s); setUser(userFromSession(s)); setAuthed(true); recordSignIn();
    },
    signOut: () => { authService.signOut(); setSession(null); setAuthed(false); },
  }), [user, users, authed, session, adminUser]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth() {
  const c = useContext(Ctx);
  if (!c) throw new Error('useAuth must be used within AuthProvider');
  return c;
}
