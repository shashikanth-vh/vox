/**
 * "The current one" — picked by comparing the rows, never by their position.
 *
 * Several screens chose the row that matters with `rows.filter(…).pop()`, which is only
 * correct if the list arrives oldest-first. The register's lists do not: documents come
 * back `uploaded_at DESC, created_at DESC` and CP/CS checklists `created_at DESC`, so
 * `.pop()` returned the OLDEST match every time. Nothing errored — the screen simply
 * showed, and acted on, a superseded row:
 *
 *   * upload a wrong sanction letter, then upload the right one beside it, and the terms
 *     were extracted from the WRONG one, while the panel named the right one;
 *   * rework a CP/CS checklist and the checker was shown the previous version's
 *     conditions, and Disburse listed unmet CPs from a stale list.
 *
 * These helpers compare the field that actually orders the rows, so no change to a
 * server-side ORDER BY can invert a caller again.
 */

/** The row with the highest numeric `key` (e.g. `checklist_version`). Null if empty. */
export function latestBy<T extends Record<string, any>>(rows: T[], key: string): T | null {
  if (!rows.length) return null;
  const n = (r: T) => Number(r?.[key] ?? 0) || 0;
  // Reduce, not sort: `>` keeps the FIRST row on a tie, which is the head of the
  // register's own newest-first order — the same row a human would call current.
  return rows.reduce((best, r) => (n(r) > n(best) ? r : best));
}

/** The row with the most recent of `fields` (first present wins). Null if empty. */
export function newestByTime<T extends Record<string, any>>(
  rows: T[], fields: string[] = ['uploaded_at', 'created_at'],
): T | null {
  if (!rows.length) return null;
  const t = (r: T) => {
    for (const f of fields) if (r?.[f]) return Date.parse(String(r[f])) || 0;
    return 0;
  };
  return rows.reduce((best, r) => (t(r) > t(best) ? r : best));
}
