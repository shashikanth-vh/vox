/**
 * Turning an audit row's `changes` JSON into something a person can read.
 *
 * The register records what changed as structured JSON — that is right for a permanent
 * trail, because a sentence cannot be queried and cannot be re-rendered later. But the
 * screen was joining every key with " → " and printing the result, so an evidence row
 * arrived as nine fields of UUIDs and workflow ids, and a stage change read
 *   label → null; fields → ["stage"]; values → {"stage":{"to":"Note Circulated", …}}
 * when what happened was: Stage: Diligence → Note Circulated.
 *
 * So there are two renderings here, not one:
 *   * `summary()` — the one line the Detail column shows: what happened, no identifiers.
 *   * `fields()`  — every field, labelled, for the dialog. Nothing is hidden, because an
 *                   audit trail whose detail you cannot get at is not an audit trail.
 *
 * Keys that are absent or null are dropped from BOTH: "supersedes_id → null" tells the
 * reader nothing except that a column exists.
 */

/** snake_case / camelCase → "Sentence case". */
export function humanKey(key: string): string {
  const spaced = key
    .replace(/_/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim();
  if (!spaced) return key;
  // Keep well-known acronyms shouting rather than title-casing them into nonsense.
  // Applied across the WHOLE string, including the first word — "sha256" is a key in
  // its own right, not only a suffix.
  const fixed = spaced
    .replace(/\bsha256\b/gi, 'SHA-256')
    .replace(/\bids?\b/gi, (m) => (m.length === 2 ? 'ID' : 'IDs'))
    .replace(/\bcp cs\b/gi, 'CP/CS')
    .replace(/\brm\b/gi, 'RM');
  return fixed[0].toUpperCase() + fixed.slice(1);
}

/** A stored value in human form: enum-ish strings unsnake, everything else stringifies. */
export function humanValue(value: any): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) return value.map(humanValue).filter(Boolean).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  const s = String(value);
  // credit_committee_approval → Credit committee approval. Left alone when it already
  // reads as prose, carries a reference (CC/2026/244154), or is an identifier.
  if (/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/.test(s)) return humanKey(s);
  return s;
}

/** Identifiers: real evidence, but noise in a one-line summary. */
const IDENTIFIER = /(^|_)(id|ids|ref|sha256|run_id|workflow_id|request_id)$/i;
const isIdentifier = (key: string) => IDENTIFIER.test(key) || /_id$/i.test(key);

/** A UUID or a workflow id — long, opaque, and never worth a summary line. */
const isOpaque = (v: any) =>
  typeof v === 'string' &&
  (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v) || v.length > 60);

/** What leads a summary line, when present — the rest follows in recorded order. */
const SUMMARY_ORDER = ['label', 'evidence_kind', 'status', 'from', 'to', 'reference',
                       'checklist_version', 'subject_type', 'note'];

/** Already shown by another column, so repeating it in Detail only crowds the line. */
const SUMMARY_SKIP = new Set(['by', 'actor', 'checker', 'prepared_by']);

export interface DetailField { label: string; value: string; }

/**
 * Every field worth showing, labelled, in a stable order. Nulls and empty strings are
 * dropped; the `fields` array of an update is dropped too, because `values` already
 * carries the same names WITH their before/after.
 */
export function fields(changes: any): DetailField[] {
  if (changes == null) return [];
  if (typeof changes !== 'object') return [{ label: 'Detail', value: String(changes) }];
  const out: DetailField[] = [];
  const values = changes.values && typeof changes.values === 'object' ? changes.values : null;
  if (values) {
    for (const [name, pair] of Object.entries<any>(values)) {
      const from = humanValue(pair?.from);
      const to = humanValue(pair?.to);
      out.push({ label: humanKey(name), value: from ? `${from} → ${to || '—'}` : (to || '—') });
    }
  }
  for (const [key, raw] of Object.entries<any>(changes)) {
    if (key === 'values') continue;
    // `fields` is the pre-`values` shape kept for compatibility — redundant once
    // `values` is rendered, and unreadable on its own.
    if (key === 'fields' && values) continue;
    const value = humanValue(raw);
    if (!value) continue;                       // null / '' / empty array
    out.push({ label: humanKey(key), value });
  }
  return out;
}

/**
 * The one line the grid shows. Identifiers and opaque values are left for the dialog —
 * the column has to answer "what happened", not "against which UUID".
 */
export function summary(changes: any, action = ''): string {
  if (changes == null) return '';
  if (typeof changes !== 'object') return String(changes);
  const parts: string[] = [];

  // A field change is the most common row and has the most readable shape.
  const values = changes.values && typeof changes.values === 'object' ? changes.values : null;
  if (values) {
    for (const [name, pair] of Object.entries<any>(values)) {
      const from = humanValue(pair?.from);
      const to = humanValue(pair?.to);
      parts.push(from ? `${humanKey(name)}: ${from} → ${to || '—'}` : `${humanKey(name)}: ${to || '—'}`);
    }
  } else if (Array.isArray(changes.fields) && changes.fields.length) {
    // Older rows carry only the names of what changed.
    parts.push(`Changed ${changes.fields.map((f: string) => humanKey(f).toLowerCase()).join(', ')}`);
  }

  const lead: string[] = [];
  const rest = Object.entries<any>(changes).sort(
    ([a], [b]) => (SUMMARY_ORDER.indexOf(a) + 1 || 99) - (SUMMARY_ORDER.indexOf(b) + 1 || 99));
  for (const [key, raw] of rest) {
    if (key === 'values' || key === 'fields') continue;
    if (SUMMARY_SKIP.has(key)) continue;
    if (isIdentifier(key) || isOpaque(raw)) continue;
    const value = humanValue(raw);
    if (!value) continue;
    // `label` names the row rather than describing the change — it leads, unprefixed.
    if (key === 'label') lead.push(value);
    else parts.push(`${humanKey(key)}: ${value}`);
  }
  parts.unshift(...lead);

  // Nothing survived the filter (an evidence row is all identifiers): say what kind of
  // thing it was rather than showing an empty cell.
  if (!parts.length) {
    const kind = humanValue(changes.evidence_kind || changes.subject_type || '');
    return kind || (action ? humanKey(action) : '');
  }
  return parts.join(' · ');
}
