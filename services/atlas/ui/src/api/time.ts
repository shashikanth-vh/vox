/**
 * Wire timestamps are UTC ISO strings ("2026-08-27T14:35:12Z"). Slicing them for
 * display printed UTC wall-time to a user living in IST — a sign-in made at 20:05
 * read "14:35", and anything logged after 18:30 IST showed YESTERDAY's date. These
 * helpers parse properly (the Z makes new Date() convert) and format in the
 * BROWSER's timezone; a string that fails to parse falls back to the old slice so
 * nothing renders blank.
 */
const p = (n: number) => String(n).padStart(2, '0');

/** "2026-08-27 20:05" in the viewer's timezone. */
export function localMinute(iso?: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).replace('T', ' ').slice(0, 16);
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** "2026-08-27" in the viewer's timezone — never the UTC day. */
export function localDay(iso?: string | null): string {
  if (!iso) return '';
  // A bare date ("2026-08-27") has no time to convert — pass it through.
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(iso))) return String(iso);
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
