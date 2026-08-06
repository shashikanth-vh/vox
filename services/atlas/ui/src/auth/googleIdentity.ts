// Google Identity Services (GIS) — the ONE place PRISM touches Google's browser SDK.
//
// Everything the login screen and the session layer need lives behind four functions,
// so no component carries GIS wiring of its own:
//   renderGoogleButton  — the official account-picker button (login screen)
//   silentReauth        — re-mint the id_token without interaction (Google id_tokens
//                         live ~1h; the session layer calls this before expiry so a
//                         working afternoon never sees a surprise sign-out)
//   googleSignOutHint   — drop Google's auto-select for this site on sign-out, so a
//                         shared machine cannot One-Tap the last user straight back in
//   isGoogleToken       — whether a JWT was issued by accounts.google.com

let gisLoad: Promise<void> | null = null;

function loadGis(): Promise<void> {
  if ((window as any).google?.accounts?.id) return Promise.resolve();
  if (gisLoad) return gisLoad;
  gisLoad = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://accounts.google.com/gsi/client';
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => { gisLoad = null; reject(new Error(
      'Could not load Google sign-in (accounts.google.com blocked — check ad-blockers).')); };
    document.head.appendChild(s);
  });
  return gisLoad;
}

const gis = () => (window as any).google?.accounts?.id;

export async function renderGoogleButton(
  clientId: string, el: HTMLElement, onCredential: (credential: string) => void,
): Promise<void> {
  await loadGis();
  gis().initialize({
    client_id: clientId,
    auto_select: true,
    callback: (r: any) => onCredential(String(r?.credential || '')),
  });
  gis().renderButton(el, { theme: 'outline', size: 'large', width: 320, text: 'continue_with' });
}

/** A fresh id_token with no user interaction (auto-select), or null if Google wants a
 *  click — the caller then simply lets the next 401 route back to the login screen. */
export async function silentReauth(clientId: string): Promise<string | null> {
  try {
    await loadGis();
  } catch {
    return null;
  }
  return new Promise((resolve) => {
    let settled = false;
    const done = (v: string | null) => { if (!settled) { settled = true; resolve(v); } };
    const timer = setTimeout(() => done(null), 8000);
    gis().initialize({
      client_id: clientId,
      auto_select: true,
      callback: (r: any) => { clearTimeout(timer); done(String(r?.credential || '') || null); },
    });
    gis().prompt((n: any) => {
      // Not displayed / skipped = no silent path available; give up quietly.
      if (n?.isNotDisplayed?.() || n?.isSkippedMoment?.()) { clearTimeout(timer); done(null); }
    });
  });
}

export function googleSignOutHint(): void {
  try { gis()?.disableAutoSelect(); } catch { /* GIS never loaded — nothing to drop */ }
}

export function isGoogleToken(idToken: string | undefined | null): boolean {
  try {
    const payload = JSON.parse(atob(String(idToken).split('.')[1]
      .replace(/-/g, '+').replace(/_/g, '/')));
    return String(payload.iss || '').includes('accounts.google.com');
  } catch { return false; }
}

/** Seconds until this JWT expires (negative = already expired; null = unreadable). */
export function tokenSecondsLeft(idToken: string | undefined | null): number | null {
  try {
    const payload = JSON.parse(atob(String(idToken).split('.')[1]
      .replace(/-/g, '+').replace(/_/g, '/')));
    return Number(payload.exp) - Math.floor(Date.now() / 1000);
  } catch { return null; }
}
