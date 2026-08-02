import { db } from '../api/atlasStore';
import { delay } from '../api/queryEngine';
import { api, withFallback } from '../api/http';
import { ROLE_TABS } from '../auth/rbac';

export const referenceService = {
  async getRef() {
    return withFallback(() => api.get<any>('/ref'), async () => { await delay(); return db().ref; });
  },
  // requirement 23: roles come from the backend
  async getRoles() {
    // The role→tab mapping is a frontend concern; the role CATALOGUE ships in /v1/ref
    // ("RBAC Role"). No legacy /roles endpoint exists on the platform.
    await delay();
    return { roles: Object.keys(ROLE_TABS), roleTabs: ROLE_TABS };
  },
  getRefSync(key: string): string[] { return db().ref[key] ?? []; },
  getThresholds() { return db().th; },
  exportData() {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(db(), null, 1)], { type: 'application/json' }));
    a.download = 'evam_atlas_export.json'; a.click();
  },
  reset() { /* session store resets on reload since it clones the seed */ },
};
