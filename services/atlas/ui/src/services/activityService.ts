import { db } from '../api/atlasStore';
import { applyQuery, delay } from '../api/queryEngine';
import { localMinute } from '../api/time';
import { api, withFallback, toCursorParams, asRows, nextCursorOf, totalOf } from '../api/http';
import type { TableQuery, Paged } from './types';

export interface ActivityRow {
  t: string; by: string; area: string; text: string; code: string; company: string; act: string;
  /** The record's own number (LD-283, a deal code …) — shown beside the company. */
  ref?: string;
}

// Which area of the business an action belongs to (drives the coloured pill + chips).
const AREAS: Record<string, string> = {
  'Lead added': 'Leads', 'Lead updated': 'Leads', 'Lead deleted': 'Leads', 'Lead converted': 'Leads',
  'Deal created': 'Deals', 'Deal merged': 'Deals', 'Deal updated': 'Deals', 'Deal deleted': 'Deals', 'Product added': 'Deals', 'Update added': 'Deals',
  'Stage-change requested': 'Deals', 'Stage-change approved': 'Deals', 'Stage-change rejected': 'Deals',
  'Lending row created': 'Lending', 'Lending stage': 'Lending', 'Lending updated': 'Lending', 'Lending deleted': 'Lending',
  'Platform Deals row created': 'Platform Deals', 'Platform Deals status': 'Platform Deals', 'Platform Deals updated': 'Platform Deals',
  'Platform Deals deleted': 'Platform Deals', 'Lender added': 'Platform Deals', 'Lender status': 'Platform Deals',
  'Matrix': 'Platform Deals', 'Chased lender': 'Platform Deals', 'Lender response': 'Platform Deals',
  'Asset Mon status': 'Asset Mon', 'Asset Mon updated': 'Asset Mon', 'Asset Mon deleted': 'Asset Mon',
  'Client created': 'Clients', 'Client updated': 'Clients',
  'FI updated': 'FI', 'FI note': 'FI', 'FI sectors': 'FI', 'FI toggle': 'FI',
  'Employee added': 'Team', 'Employee updated': 'Team', 'Employee deleted': 'Team',
  'Interaction logged': 'Deals', 'Document uploaded': 'Documents', 'Document removed': 'Documents',
  'Signed in': 'Session', 'Seeded': 'System', 'Deleted': 'System', 'Exported': 'System',
};
export const areaOf = (act: string): string => AREAS[act] || 'Other';

function coName(code: string): string {
  const c = db().clients?.[code];
  return (c && c.name) || code || '';
}

// One readable sentence per operation. Detail often carries "old → new" — reuse it.
export function describe(a: { act: string; code?: string; detail?: string }): string {
  const act = a.act || '', code = a.code || '', d = (a.detail || '').trim(), co = coName(code);
  const lender = d.indexOf(':') > -1 ? d.split(':')[0].trim() : '';
  const after = d.indexOf(':') > -1 ? d.slice(d.indexOf(':') + 1).trim() : d;
  switch (act) {
    case 'Seeded': return 'Register initialised' + (d ? ' — ' + d : '');
    case 'Signed in': return 'Signed in to ATLAS';
    case 'Exported': return 'Exported ' + (d || 'data');
    case 'Deleted': return 'Deleted a ' + (d || 'row') + ' (admin action)';
    case 'Lead added': return 'Added a new lead: ' + (d || co);
    case 'Lead updated': return 'Updated lead ' + code + (d ? ' — ' + d : '');
    case 'Lead deleted': return 'Removed lead ' + code + (d ? ' (' + d + ')' : '');
    case 'Lead converted': return 'Converted lead ' + code + ' into a client ' + d;
    case 'Deal created': return 'Created a deal for ' + co + (d ? ' (' + d + ')' : '');
    case 'Deal merged': return 'Merged a lead into the existing deal for ' + co;
    case 'Deal updated': return 'Updated the deal for ' + co + (d ? ' — changed ' + d : '');
    case 'Deal deleted': return 'Deleted the deal for ' + co;
    case 'Product added': return 'Added a product to ' + co + ': ' + d;
    case 'Update added': return 'Logged an update on ' + co + (d ? ': “' + d + '”' : '');
    case 'Stage-change requested': return 'Requested a stage change on ' + co + (d ? ' — ' + d : '');
    case 'Stage-change approved': return 'Approved a stage change on ' + co + (d ? ' — ' + d : '');
    case 'Stage-change rejected': return 'Rejected a stage change on ' + co + (d ? ' — ' + d : '');
    case 'Lending row created': return 'Opened a lending facility for ' + co + (d ? ' (' + d + ')' : '');
    case 'Lending stage': return 'Moved ' + co + '’s lending stage ' + d;
    case 'Lending updated': return 'Edited ' + co + '’s lending record' + (d ? ' — ' + d : '');
    case 'Lending deleted': return 'Removed a lending row for ' + co;
    case 'Platform Deals row created': return 'Opened a Platform Deals mandate for ' + co + (d ? ' (' + d + ')' : '');
    case 'Platform Deals status': return 'Moved ' + co + '’s Platform Deals status ' + d;
    case 'Platform Deals updated': return 'Edited ' + co + '’s Platform Deals record' + (d ? ' — ' + d : '');
    case 'Platform Deals deleted': return 'Removed a Platform Deals row for ' + co;
    case 'Lender added': return 'Added lender ' + d + ' to ' + co + '’s mandate';
    case 'Lender status':
    case 'Matrix': return lender ? ('Moved ' + lender + ' ' + after + ' on ' + co + '’s mandate')
      : ('Updated lender progress on ' + co + ': ' + d);
    case 'Chased lender': return lender ? ('Chased ' + lender + ' on ' + co + '’s mandate: “' + after + '”')
      : ('Chased a lender on ' + co + '’s mandate');
    case 'Lender response': return lender ? ('Recorded ' + lender + '’s response on ' + co + ': ' + after)
      : ('Recorded a lender response on ' + co + ': ' + d);
    case 'Asset Mon status': return 'Changed ' + co + '’s asset status ' + d;
    case 'Asset Mon updated': return 'Edited ' + co + '’s asset entry' + (d ? ' — ' + d : '');
    case 'Asset Mon deleted': return 'Removed an asset entry for ' + co;
    case 'Client created': return 'Onboarded ' + (d || co) + ' as a client';
    case 'Client updated': return 'Updated ' + co + '’s client profile' + (d ? ' (' + d + ')' : '');
    case 'FI updated': return 'Updated FI ' + (code || '') + (d ? ' — ' + d : '');
    case 'FI note': return 'Added a note on FI ' + (code || lender) + (after ? ': ' + after : '');
    case 'FI sectors': return 'Updated sector coverage for FI ' + (code || '') + (d ? ' — ' + d : '');
    case 'FI toggle': return 'Updated FI ' + (code || '') + (d ? ' — ' + d : '');
    case 'Employee added': return 'Added employee ' + (d || code);
    case 'Employee updated': return 'Updated the employee record for ' + (code || d);
    case 'Employee deleted': return 'Removed employee ' + (code || d);
    case 'Interaction logged': return 'Logged an interaction with ' + co + (d ? ': ' + d : '');
    case 'Document uploaded': return 'Uploaded ' + d + ' for ' + co;
    case 'Document removed': return 'Removed ' + d + ' from ' + co + '’s file';
    default: return act + (d ? ': ' + d : '');
  }
}

// Full enriched activity stream, newest first (audit is already unshift-ordered).
function enrich(): ActivityRow[] {
  return (db().audit || []).map((a: any) => ({
    t: a.t, by: a.by || a.role || '', area: areaOf(a.act || ''),
    text: describe(a), code: a.code || '', company: coName(a.code || ''), act: a.act || '',
  }));
}

/**
 * A notification read back as an activity row. The API's own message is preferred when
 * it sends one; otherwise the action is described the same way a local audit entry is,
 * so both sources read alike in the table.
 */
/**
 * One row of GET /v1/activity — the register's own plain-English rendering of its audit
 * trail. The sentence, the area and the company are resolved SERVER-side (where the
 * before→after values and the entity join live), so the browser neither re-derives a
 * vocabulary nor resolves a UUID per row.
 */
export function fromActivityWire(r: any): ActivityRow {
  return {
    // localMinute, not a slice: the wire is UTC, the reader lives in IST — the
    // heal that ran at 06:23 must not read "00:53".
    t: localMinute(String(r?.at || '')),
    by: r?.actor || '',
    area: r?.area || 'Other',
    text: r?.summary || '',
    code: r?.code || '',
    company: r?.company || '',
    act: r?.action || '',
    ref: r?.ref || '',
  };
}

export function toActivityRow(r: any): ActivityRow {
  const act = r?.action || r?.event_type || r?.type || r?.title || '';
  const code = r?.subject_no || r?.code || r?.subject_id || '';
  // `t` is rendered verbatim, and the local store writes "YYYY-MM-DD HH:MM".
  const t = localMinute(String(r?.created_at || r?.occurred_at || ''));
  const detail = r?.detail || r?.body || '';
  return {
    t,
    by: r?.actor_name || r?.actor || r?.performed_by || r?.actor_email || '',
    area: areaOf(act),
    text: r?.message || r?.body || describe({ act, code, detail }),
    code,
    company: r?.company || r?.entity_name || coName(code),
    act,
  };
}

export const activityService = {
  // Unpaged stream — used for the KPI chips and the area filter list. Asks for one
  // capped page rather than "everything": the endpoint is cursor-paged, so there is no
  // unbounded read to make.
  async listAll(): Promise<ActivityRow[]> {
    return withFallback<ActivityRow[]>(
      async () => {
        // The ACTIVITY TRAIL, not the notification feed. This screen used to read
        // /v1/notifications — a per-user list of things still UNREAD, which is empty on a
        // busy register and always will be: it was never a history of what people did.
        const data = await api.get<any>('/activity', { limit: 200 });
        return asRows(data, 'activity').map(fromActivityWire);
      },
      () => enrich(),
    );
  },

  /** The headline counts over the WHOLE trail (B36) — the chips used to be summed
   *  in the browser over the first 200 rows. */
  async stats(): Promise<{ total: number; today: number; people: number; records: number }> {
    return withFallback(
      () => api.get<any>('/activity/stats'),
      async () => {
        const all = enrich(); const today = all[0]?.t ? all[0].t.slice(0, 10) : '';
        return { total: all.length, today: all.filter((e) => e.t && e.t.slice(0, 10) === today).length,
          people: new Set(all.map((e) => e.by).filter(Boolean)).size, records: new Set(all.map((e) => e.code).filter(Boolean)).size };
      },
    );
  },

  // Paged/sorted/filtered slice for the table. `area` narrows before querying.
  async list(q: TableQuery, area?: string): Promise<Paged<ActivityRow>> {
    return withFallback<Paged<ActivityRow>>(
      async () => {
        // Paged at the register (B37): the page size is the grid's, the cursor is the
        // offset of the next page, and the total is exact. `area` narrows there too.
        // The grid's prose filters walk the trail with a large page; the register caps
        // one page at 500 rows, so a walked filter sees the latest 500.
        const size = Math.min(Math.max(1, q.pageSize || 25), 500);
        const offset = q.cursor ? Math.max(0, parseInt(q.cursor, 10) || 0) : (q.pageIndex || 0) * size;
        const data = await api.get<any>('/activity', { limit: size, offset, with_total: 'true',
          ...(area ? { area } : {}) });
        const rows = asRows(data, 'activity').map(fromActivityWire);
        const total = Number(data?.total) || rows.length;
        const next = offset + rows.length;
        return { rows, total, nextCursor: next < total && rows.length === size ? String(next) : null };
      },
      async () => {
        await delay();
        const rows = area ? enrich().filter((e) => e.area === area) : enrich();
        return applyQuery(rows, { ...q, searchFields: ['text', 'by', 'area', 'code', 'company'] });
      },
    );
  },
};
