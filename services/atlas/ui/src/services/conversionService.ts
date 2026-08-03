import { db, today } from '../api/atlasStore';
import { errText, USE_REAL_API } from '../api/http';
import { orchestrator } from '../api/orchestratorClient';
import { ORCHESTRATOR_URL } from '../api/axiosClient';
import { runSuffix } from './entitiesService';
import { employeesService } from './employeesService';
import { getSession } from '../auth/session';
import { writeAudit } from './auditService';
import { normName } from '../utils/format';
import type { Lead } from '../pages/Leads/lead.types';

export interface PushPayload {
  code: string;
  existing: boolean;
  client: { sector: string; lens: string; state: string; about: string; toi: string };
  deal: { temp: string; source: string; sourceDetail: string; an: string };
  flags: { lend: boolean; syn: boolean; am: boolean };
  lending?: { amt: number; stage: string };
  syndication?: { amt: number; synType: string; mstat3: string; status: string; fac: string; tenor: string; pri: string; im: string; pot: string; exist: string; price: string };
  am?: { val: number; mw: number; dtype: string; status: string };
}

/**
 * A conversion runs on the WORKFLOW plane, not the Register: the lead is qualified
 * first, and only a qualification that comes back successful may be converted. The two
 * are separate workflows and the second is gated on the first, so they run in order
 * rather than together.
 */
const QUALIFY_URL = '/v1/workflows/lead-qualifications';
const CONVERT_URL = '/v1/workflows/lead-conversions';

/** Turn an orchestrator failure into a message the dialog can render verbatim. */
function workflowError(e: any, step: string): string {
  const status = e?.response?.status;
  const asText = errText(e?.response?.data);
  if (e?.response) console.warn('[orchestrator] %s failed (%s):', step, status, e.response.data);
  if (status === 401 || status === 403) return asText || `Not permitted to ${step}.`;
  if (status === 404) return asText || 'That lead is not on the workflow plane — refresh and try again.';
  if (status === 409) return asText || 'That lead has already been converted.';
  if (status === 400 || status === 422) return asText || `The workflow rejected the request to ${step}.`;
  if (status) return asText || `The workflow plane returned ${status}.`;
  return asText || `Cannot reach the workflow plane at ${ORCHESTRATOR_URL || '(unset)'}.`;
}

/**
 * A qualification reference in the collection's shape — QUAL/<COMPANY>/<runSuffix>.
 * The suffix keeps it unique per run, as it does for entity codes.
 */
export function qualificationRef(company: string, suffix = runSuffix()): string {
  const stem = (company.trim().split(/\s+/)[0] || 'LEAD').replace(/[^A-Za-z0-9]/g, '').toUpperCase() || 'LEAD';
  return `QUAL/${stem.slice(0, 16)}/${suffix}`;
}

/** Whether the workflow plane says a qualification passed. */
function qualified(res: any): boolean {
  // A plain 2xx with no verdict in the body is taken as passed — the call is the gate,
  // and only an explicit negative should stop the conversion.
  const v = res?.passed ?? res?.qualified ?? res?.result?.passed;
  if (typeof v === 'boolean') return v;
  const status = String(res?.status ?? res?.state ?? '').toLowerCase();
  if (['failed', 'rejected', 'refused', 'not_qualified'].includes(status)) return false;
  return true;
}

/** The Access user id behind a person's display name, when they have been provisioned. */
const accessIdOf = (name?: string) => (name ? employeesService.find(name)?.accessId : undefined);
const emailOf = (name?: string) => (name ? employeesService.find(name)?.email : undefined);

/**
 * The conversion carries ONE amount, but the dialog collects a figure per product line,
 * so the ticked lines are summed — the total being taken to committee. A push with only
 * Lending ticked therefore sends exactly the lending amount.
 */
export function amountCr(p: PushPayload): number {
  const n = (v: any) => (Number.isFinite(Number(v)) ? Number(v) : 0);
  return (p.flags.lend ? n(p.lending?.amt) : 0)
    + (p.flags.syn ? n(p.syndication?.amt) : 0)
    + (p.flags.am ? n(p.am?.val) : 0);
}

/**
 * The conversion also carries ONE product_type. Syndication's "Facility type" is the only
 * field on the form that names an instrument ("Term Loan / WC / Leasing…"), so it is used
 * when present; otherwise the collection's default stands.
 */
export function productType(p: PushPayload): string {
  return (p.flags.syn && p.syndication?.fac.trim()) || 'Term Loan';
}

export function mintCode(name: string): string {
  let base = normName(name).toUpperCase().slice(0, 10) || 'CLIENT';
  let c = base, i = 2;
  while (db().clients[c]) c = base + i++;
  return c;
}

export const conversionService = {
  findExistingCode(company: string): string | null {
    const hit = Object.entries(db().clients).find(([, v]: any) => normName(v.name) === normName(company));
    return hit ? hit[0] : null;
  },
  /**
   * Push a lead to Deals: qualify it on the workflow plane, and convert it only if that
   * qualification passed. Both are awaited and neither is fire-and-forget — a lead that
   * the workflow refused must not disappear from the register and show up as a deal.
   * The local store is written only once the plane has accepted both steps.
   */
  async pushLeadToDeals(lead: Lead, p: PushPayload, by: string): Promise<{ ok: boolean; code?: string; error?: string }> {
    if (USE_REAL_API) {
      const session = getSession();
      // The workflows address the lead by the API's id, not the LD-nnn lead_no.
      const leadId = lead.apiId || lead.id;
      const analyst = p.deal.an;
      try {
        const res = await orchestrator.post<any>(QUALIFY_URL, {
          lead_id: leadId,
          qualified_by: session?.email || by,
          qualification_reference: qualificationRef(lead.company),
          passed: true,
        });
        if (!qualified(res)) {
          return { ok: false, error: 'The lead did not pass qualification, so it was not converted.' };
        }
      } catch (e: any) {
        return { ok: false, error: workflowError(e, 'qualify the lead') };
      }
      // The RM who owns the lead is the requester; the signed-in user stands in when that
      // RM has no email on record.
      const rmEmail = emailOf(lead.rm) || session?.email || by;
      try {
        await orchestrator.post<any>(CONVERT_URL, {
          lead_id: leadId,
          requested_by: rmEmail,
          is_lending: p.flags.lend,
          is_syndication: p.flags.syn,
          is_asset_mon: p.flags.am,
          product_type: productType(p),
          amount_cr: amountCr(p),
          rm: lead.rm,
          rm_id: accessIdOf(lead.rm),
          analyst,
          analyst_id: accessIdOf(analyst),
          note: `Converted from lead ${lead.id} (${lead.company}).`,
          // THE CLIENT this dialog collects — the "one save: client + deal + product
          // rows" promise. A deal must belong to a company, and a lead added through
          // the Add-lead form carries none, so the plane links the matching client or
          // CREATES it from these fields. (They were previously written only to the
          // local store, which is why such a conversion died on the workflow plane.)
          company_name: lead.company,
          sector: p.client.sector || undefined,
          lens: p.client.lens || undefined,
          state: p.client.state || undefined,
          industry: p.client.toi || undefined,
          about: p.client.about || undefined,
        });
      } catch (e: any) {
        // The qualification already went through, so say so — a retry re-qualifies.
        return { ok: false, error: `${workflowError(e, 'convert the lead')} The lead was qualified but not converted.` };
      }
    }
    const code = p.code, now = today();
    // client
    if (!p.existing) {
      db().clients[code] = { name: lead.company, ...p.client, notes: 'Onboarded ' + now };
      writeAudit(by, 'Client created', code, lead.company);
    } else {
      Object.assign(db().clients[code], { sector: p.client.sector, lens: p.client.lens });
      if (p.client.about) db().clients[code].about = p.client.about;
    }
    // deal
    let d = db().deals.find((x: any) => x.code === code);
    if (!d) {
      d = { code, rm: lead.rm, an: p.deal.an, lend: false, syn: false, am: false, temp: p.deal.temp, source: p.deal.source, sourceDetail: p.deal.sourceDetail, createdAt: now, remarks: 'From lead ' + lead.id };
      db().deals.unshift(d); writeAudit(by, 'Deal created', code, 'from ' + lead.id);
    } else {
      Object.assign(d, { temp: p.deal.temp, source: p.deal.source, sourceDetail: p.deal.sourceDetail });
      if (p.deal.an) d.an = p.deal.an; writeAudit(by, 'Deal merged', code, 'push from ' + lead.id);
    }
    if (p.flags.lend && p.lending) {
      d.lend = true;
      db().lending.unshift({ id: 'L' + Date.now(), code, amt: p.lending.amt, rm: lead.rm, an: p.deal.an, stage: p.lending.stage, updated: now, sanc: null, pendingWith: '', h: [{ stage: p.lending.stage, t: now, by }], createdAt: now, remarks: 'From lead ' + lead.id });
    }
    if (p.flags.syn && p.syndication) {
      const s = p.syndication; d.syn = true;
      db().syn.unshift({ id: 'S' + Date.now(), code, toi: p.client.toi, rm: lead.rm, an: p.deal.an, lc: '', pri: s.pri, status: s.status, amt: s.amt, synType: s.synType, mstat3: s.synType === 'Fee will be paid by customer' ? s.mstat3 : '', fac: s.fac, tenor: s.tenor, im: s.im, pot: s.pot, sancL: '', ipL: '', exist: s.exist, price: s.price, pendingWith: '', lenders: [], h: [{ status: s.status, t: now, by }], createdAt: now, remarks: 'From lead ' + lead.id });
    }
    if (p.flags.am && p.am) {
      d.am = true;
      db().am.unshift({ id: 'A' + Date.now(), code, state: p.client.state, val: p.am.val, mw: p.am.mw, nature: 'Seller', dtype: p.am.dtype, inv: '', itype: '', status: p.am.status, teaser: null, createdAt: now, notes: 'From lead ' + lead.id });
    }
    lead.status = 'Converted'; lead.conv = code; (lead as any).convertedAt = now;
    writeAudit(by, 'Lead converted', lead.id, '→ ' + code);
    return { ok: true, code };
  },
};
