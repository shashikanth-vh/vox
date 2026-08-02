export const num = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

export const fmt = (v: unknown, d = 2): string =>
  num(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });

export const today = () => new Date().toISOString().slice(0, 10);

export const daysSince = (d?: string | null): number | null => {
  if (!d) return null;
  return Math.max(0, Math.round((Date.now() - new Date(d).getTime()) / 864e5));
};

// Normalised company-name matcher, mirrors template normName().
export const normName = (s?: string) =>
  String(s || '').toLowerCase()
    .replace(/\b(private|pvt|limited|ltd|llp|india|energy|energies|solutions?|services?|technologies|technology|ventures?|renewables?)\b/g, '')
    .replace(/[^a-z0-9]/g, '');
