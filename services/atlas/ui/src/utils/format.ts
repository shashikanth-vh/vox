export const num = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

export const fmt = (v: unknown, d = 2): string =>
  num(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });

// The VIEWER'S calendar day — never the UTC day (an IST evening is already
// tomorrow in UTC terms after 18:30, and every date stamped from here is
// "the day the user acted").
export const today = () => {
  const d = new Date(); const p2 = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
};

export const daysSince = (d?: string | null): number | null => {
  // CALENDAR days in the viewer's timezone — "logged this morning" is 0d, not 1d.
  // The old 24h-block rounding called anything past local noon "1d ago" and, worse,
  // parsed a bare date as UTC midnight, so a same-day chase read a day old in IST.
  if (!d) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d).slice(0, 10));
  if (!m) return null;
  const then = new Date(+m[1], +m[2] - 1, +m[3]);
  const now = new Date();
  const today0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.max(0, Math.round((today0.getTime() - then.getTime()) / 864e5));
};

// Normalised company-name matcher, mirrors template normName().
export const normName = (s?: string) =>
  String(s || '').toLowerCase()
    .replace(/\b(private|pvt|limited|ltd|llp|india|energy|energies|solutions?|services?|technologies|technology|ventures?|renewables?)\b/g, '')
    .replace(/[^a-z0-9]/g, '');
