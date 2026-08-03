import { getSession } from '../../auth/session';

/**
 * The RM name VocX files a capture under.
 *
 * VocX keys reports, drafts and audio by RM, and the rest of PRISM addresses a person by
 * their SHORT HANDLE — the `name` column on the people roster, which is what leads, deals
 * and trackers store. So the handle is what goes on the wire; the full name and the
 * e-mail local part are fallbacks for a session that never resolved one.
 *
 * Taken from the signed-in session rather than typed into a box: a capture attributed to
 * whoever the user last typed is not attribution.
 */
export function currentRm(): string {
  const s = getSession();
  if (!s) return '';
  return (s.shortName || s.fullName || s.email.split('@')[0] || '').trim();
}
