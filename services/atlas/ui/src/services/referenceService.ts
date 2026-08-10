import { db } from '../api/atlasStore';
import { delay } from '../api/queryEngine';
import { api, withFallback, USE_REAL_API } from '../api/http';
import { ROLE_TABS } from '../auth/rbac';

/**
 * Every dropdown in ATLAS comes from ONE place: the Register's `ref_values` table, served
 * by `GET /v1/ref`. The bundled seed JSON is a fallback for mock/offline mode only.
 *
 * That mattered more than it looked. While the lists were read straight out of the bundle,
 * a vocabulary fix (the overlapping Tenor buckets, the duplicated Line of Lending entry,
 * the Counterparty Type split) could only reach a user by rebuilding the browser bundle —
 * and the person-name lists were worse still: they offered five prototype names, so an RM
 * picked someone the Register had never heard of and the mismatch only surfaced much later,
 * when the conversion was refused. `/v1/ref` merges the role-driven name lists in live from
 * the people directory, so what the form offers is what the Register will accept.
 */

/** `/v1/ref` answers {category: [{value,label}]}; the app reads plain string lists. */
function toLists(payload: any): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  Object.entries(payload || {}).forEach(([category, values]) => {
    if (!Array.isArray(values)) return;
    const list: string[] = [];
    const labels: Record<string, string> = {};
    values.forEach((v: any) => {
      const value = typeof v === 'string' ? v : v?.value;
      if (typeof value !== 'string' || value === '') return;
      list.push(value);
      const label = typeof v === 'string' ? '' : String(v?.label || '');
      if (label && label !== value) labels[value] = label;
    });
    if (list.length) {
      out[category] = list;
      // THE LABEL WAS BEING THROWN AWAY. The register answers each reference value as
      // {value, label} — and for the person lists the value is the SHORT HANDLE the
      // platform stores in rm/analyst ("AT", "PS") while the label is the full name
      // ("Arun Tiwari"). Keeping only the value left the desk picking from a column of
      // initials, with the odd full name mixed in wherever a handle happened to be a
      // name — which is exactly what made the Allot analyst list unreadable.
      REF_LABELS[category] = labels;
    }
  });
  return out;
}

/** category → { storedValue: humanLabel }, for every value whose label differs. */
const REF_LABELS: Record<string, Record<string, string>> = {};

/** Categories `/v1/ref` actually served this session — the Register's answer wins over
 *  anything derived locally (see employeesService.hydrateRoster). */
export const servedByRegister = new Set<string>();

export const referenceService = {
  async getRef() {
    return withFallback(() => api.get<any>('/ref'), async () => { await delay(); return db().ref; });
  },

  /**
   * Pull the live vocabularies into the store that `getRefSync` reads, so every existing
   * dialog picks them up without changing a single `<SelectFld>`.
   *
   * Merged, not replaced: a category the Register does not serve keeps its seeded values
   * rather than becoming an empty select. Fail-soft for the same reason — an unreachable
   * Register must not leave the user with forms they cannot fill in.
   */
  async hydrate(): Promise<void> {
    if (!USE_REAL_API) return;
    try {
      const lists = toLists(await api.get<any>('/ref'));
      Object.assign(db().ref, lists);
      Object.keys(lists).forEach((c) => servedByRegister.add(c));
    } catch (e) {
      console.warn('[api] /v1/ref unavailable — keeping the seeded dropdowns:', e);
    }
  },

  // requirement 23: roles come from the backend
  async getRoles() {
    // The role→tab mapping is a frontend concern; the role CATALOGUE ships in /v1/ref
    // ("RBAC Role"). No legacy /roles endpoint exists on the platform.
    await delay();
    return { roles: Object.keys(ROLE_TABS), roleTabs: ROLE_TABS };
  },
  getRefSync(key: string): string[] { return db().ref[key] ?? []; },
  /** How a stored value should READ. Empty when the value is already the label. */
  getRefLabels(key: string): Record<string, string> { return REF_LABELS[key] ?? {}; },
  getThresholds() { return db().th; },
  exportData() {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(db(), null, 1)], { type: 'application/json' }));
    a.download = 'evam_atlas_export.json'; a.click();
  },
  reset() { /* session store resets on reload since it clones the seed */ },
};
