import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { ROLES, primaryRole, type Role } from './rbac';
import { db } from '../api/atlasStore';
import { authService } from '../services/authService';
import { getSession, type PrismSession } from './session';

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

export function AuthProvider({ children }: { children: ReactNode }) {
  const users: AppUser[] = db().people.map((p: any) => {
    const roles = parseRoles(p);
    return { name: p.name, full: p.full, roles, role: primaryRole(roles) };
  });
  // Login always starts as Admin (Kannan) in mock mode.
  const adminUser = users.find((u) => u.roles.includes('Admin')) ?? users[0];

  // A session surviving in sessionStorage (same tab, e.g. a reload) signs straight back in.
  const restored = authService.isLive() ? getSession() : null;
  const [session, setSession] = useState<PrismSession | null>(restored);
  const [user, setUser] = useState<AppUser>(restored ? userFromSession(restored) : adminUser);
  const [authed, setAuthed] = useState(Boolean(restored));

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
      setSession(s); setUser(userFromSession(s)); setAuthed(true);
    },
    signInWithIdToken: async (idToken) => {
      if (!authService.isLive()) { setUser(adminUser); setSession(null); setAuthed(true); return; }
      const s = await authService.signInWithIdToken(idToken);
      setSession(s); setUser(userFromSession(s)); setAuthed(true);
    },
    signInWithGoogle: async () => {
      if (!authService.isLive()) { setUser(adminUser); setSession(null); setAuthed(true); return; }
      const s = await authService.signInWithGoogle();
      setSession(s); setUser(userFromSession(s)); setAuthed(true);
    },
    signInWithGoogleCredential: async (credential) => {
      if (!authService.isLive()) { setUser(adminUser); setSession(null); setAuthed(true); return; }
      const s = await authService.signInWithGoogleCredential(credential);
      setSession(s); setUser(userFromSession(s)); setAuthed(true);
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
