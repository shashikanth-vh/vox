import type { VocxCapabilities } from '../../services/vocxService';

/**
 * What is missing from a report, and whether that should stop it being filed.
 *
 * The rules are the SERVER'S. `GET /v1/capabilities` returns a `completeness` list for
 * the core report fields and a `required` flag on individual template fields, so the bar
 * can be raised in VocX's config without shipping a new UI — exactly how the templates
 * themselves already work. A client that decided for itself what mattered would drift
 * from the service the moment either changed.
 *
 * Two tiers, because they mean different things to the person approving:
 *   * REQUIRED — the report is not really a report without it. Named, and the default
 *     action becomes "go back and fill" rather than "file".
 *   * OPTIONAL — worth having and often auto-filled from what was said. Named, and
 *     waivable: intel captured in a car park should not be blocked because nobody
 *     mentioned a ticket size.
 */

export interface Rule {
  key: string;
  label: string;
  required?: boolean;
  /** 'list' means a non-empty array; anything else means a non-empty scalar. */
  kind?: string;
  /** Set for template fields so the dialog can say which template asked for it. */
  from?: string;
}

export interface Gap { key: string; label: string; from?: string; }
export interface Completeness {
  missingRequired: Gap[];
  missingOptional: Gap[];
  /** Filled / total across both tiers — the progress the card shows. */
  filled: number;
  total: number;
}

const isEmpty = (v: any, kind?: string): boolean => {
  if (kind === 'list') return !Array.isArray(v) || v.filter((x) => {
    if (x == null) return false;
    if (typeof x === 'string') return x.trim() !== '';
    // A next-step row with no action, or an attendee with no name, is not an entry.
    if (typeof x === 'object') return Object.values(x).some((f) => String(f ?? '').trim() !== '');
    return true;
  }).length === 0;
  if (v == null) return true;
  if (typeof v === 'string') return v.trim() === '';
  if (typeof v === 'number') return Number.isNaN(v);
  return false;
};

/** The core rules plus the required fields of whichever templates are active. */
export function rulesFor(caps: VocxCapabilities | null, activeTemplateIds: string[]): Rule[] {
  const core: Rule[] = Array.isArray(caps?.completeness) ? caps!.completeness : [];
  const tpl: Rule[] = [];
  for (const t of (caps?.report_templates || []) as any[]) {
    if (!activeTemplateIds.includes(t.id)) continue;
    for (const f of t.fields || []) {
      // Only the fields the template MARKS as required become gates; the rest are
      // ordinary optional extras and would drown the dialog.
      tpl.push({ key: `extra.${f.key}`, label: f.label || f.key,
                 required: !!f.required, from: t.label });
    }
  }
  return [...core, ...tpl];
}

/**
 * `report` is the extraction's report object; template values live under `report.extra`.
 * A rule keyed `extra.<k>` reads there, anything else reads the report itself.
 */
export function check(report: any, rules: Rule[]): Completeness {
  const missingRequired: Gap[] = [];
  const missingOptional: Gap[] = [];
  let filled = 0;
  for (const r of rules) {
    const value = r.key.startsWith('extra.')
      ? (report?.extra || {})[r.key.slice(6)]
      : report?.[r.key];
    if (isEmpty(value, r.kind)) {
      (r.required ? missingRequired : missingOptional).push(
        { key: r.key, label: r.label, from: r.from });
    } else {
      filled += 1;
    }
  }
  return { missingRequired, missingOptional, filled, total: rules.length };
}
