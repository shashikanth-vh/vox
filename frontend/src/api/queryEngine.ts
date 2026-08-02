import type { TableQuery, Paged } from '../services/types';

export function applyQuery<T extends Record<string, any>>(rows: T[], q: TableQuery): Paged<T> {
  let out = rows.slice();

  // Global search
  if (q.globalFilter) {
    const needle = q.globalFilter.toLowerCase();
    out = out.filter((r) =>
      (q.searchFields ?? Object.keys(r)).some((f) =>
        String(r[f] ?? '').toLowerCase().includes(needle),
      ),
    );
  }

  // Column filters. v12's popover value is { q, vals }: a row must contain the
  // text `q` (substring) AND, if any checkboxes are ticked, match one of `vals`
  // (exact). Plain string/array values are still handled for other callers.
  (q.columnFilters ?? []).forEach((cf) => {
    const v: any = cf.value;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const needle = String(v.q ?? '').toLowerCase();
      if (needle) out = out.filter((r) => String(r[cf.id] ?? '').toLowerCase().includes(needle));
      const vals: string[] = v.vals ?? [];
      if (vals.length) { const set = new Set(vals.map(String)); out = out.filter((r) => set.has(String(r[cf.id] ?? ''))); }
      return;
    }
    if (Array.isArray(v)) {
      if (!v.length) return;
      const picked = new Set(v.map((x) => String(x).toLowerCase()));
      // Multi-value cells (e.g. Role = "Admin, Management", Sectors = "Solar, EV") match
      // if the whole value OR any comma-separated token is picked.
      out = out.filter((r) => {
        const cell = String(r[cf.id] ?? '').toLowerCase();
        if (picked.has(cell)) return true;
        return cell.split(/,\s*/).some((tok) => picked.has(tok.trim()));
      });
      return;
    }
    const val = String(v ?? '').toLowerCase();
    if (!val) return;
    out = out.filter((r) => String(r[cf.id] ?? '').toLowerCase().includes(val));
  });

  // Sorting
  if (q.sorting && q.sorting.length) {
    const { id, desc } = q.sorting[0];
    out.sort((a, b) => {
      const x = a[id], y = b[id];
      const na = typeof x === 'number' ? x : String(x ?? '').toLowerCase();
      const nb = typeof y === 'number' ? y : String(y ?? '').toLowerCase();
      return (na < nb ? -1 : na > nb ? 1 : 0) * (desc ? -1 : 1);
    });
  }

  const total = out.length;
  const start = q.pageIndex * q.pageSize;
  return { rows: out.slice(start, start + q.pageSize), total };
}

// Simulate network latency so loading states are visible in the mock.
export const delay = (ms = 150) => new Promise((r) => setTimeout(r, ms));
