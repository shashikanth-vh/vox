import axios from 'axios';
import { PULSE_URL, TENANT } from '../api/axiosClient';
import { authHeaders } from '../auth/session';
import { errText } from '../api/http';
import { writeAudit } from './auditService';

/* The News Radar's server side — email digests and recurring schedules.
   ----------------------------------------------------------------------------
   These were stubbed for as long as PULSE had no desk-facing half: the dialogs
   were complete and every call answered "not connected". PULSE now serves them
   at /pulse/v1/news/*, so the same payloads go to a real server.

   Email is OPTIONAL and off until PULSE_SMTP_* is set. That is not an error
   state — search works without it — so `config()` reports it and the dialogs
   say plainly what is missing rather than failing at send time. */

const pulse = axios.create({ baseURL: `${PULSE_URL}/v1/news`, timeout: 120_000 });
// A digest of 300 firms legitimately takes minutes; the edge allows it too.

pulse.interceptors.request.use((c) => {
  c.headers = { ...(c.headers || {}), 'X-Tenant': TENANT, ...authHeaders() } as any;
  return c;
});

export interface Schedule {
  id: string; q: string; recipients: string; cadence: 'daily' | 'weekly'; weekday: number;
  hour: number; window_days: number; adverse_only: boolean; scope: 'all-firms' | 'terms'; subject: string;
}
export interface DigestGroup { term: string; articles: any[] }
export interface PulseConfig { email: boolean; from: string; gdelt: boolean; scheduler: boolean }

type Result<T> = Promise<{ ok: boolean; error?: string; data?: T }>;

/** Every call answers the same shape, and a failure carries the SERVER's words —
 *  "Email is not configured", "Send failed: …" — not a bare status code. */
async function call<T>(run: () => Promise<{ data: any }>): Result<T> {
  try {
    const res = await run();
    const body = res.data || {};
    if (body.ok === false) return { ok: false, error: body.message || 'Failed', data: body };
    return { ok: true, data: body };
  } catch (e: any) {
    const detail = errText(e?.response?.data) || e?.response?.data?.message;
    return { ok: false, error: detail || e?.message || 'PULSE is not reachable.' };
  }
}

export const pulseService = {
  /** What the radar can actually do here — is email configured, is GDELT on. */
  async config(): Result<PulseConfig> {
    return call<PulseConfig>(() => pulse.get('/config'));
  },

  /** Search the news for one term, server-side (the browser cannot: no CORS). */
  async search(q: string, from = '', to = ''): Result<{ articles: any[] }> {
    return call<{ articles: any[] }>(() => pulse.get('/search', { params: { q, from, to } }));
  },

  async emailNews(p: { q: string; from: string; to: string; recipients: string; subject: string },
                  by: string): Result<{ count: number }> {
    const r = await call<{ count: number }>(() => pulse.post('/email', p));
    if (r.ok) writeAudit(by, 'News emailed', '', `${p.q} → ${p.recipients}`);
    return r;
  },

  async emailDigest(p: { recipients: string; subject: string; groups: DigestGroup[]; adverse_only: boolean },
                    by: string): Result<{ firms: number; count: number }> {
    const r = await call<{ firms: number; count: number }>(() => pulse.post('/email-digest', p));
    if (r.ok) writeAudit(by, 'News emailed', '', `all firms → ${p.recipients}`);
    return r;
  },

  async listSchedules(): Result<{ schedules: Schedule[]; smtp: boolean }> {
    const r = await call<{ schedules: Schedule[]; smtp: boolean }>(() => pulse.get('/schedules'));
    // The dialog renders `data` even on failure, so it must always have the shape.
    return r.ok ? r : { ...r, data: { schedules: [], smtp: false } };
  },

  async createSchedule(p: Omit<Schedule, 'id'>, by: string): Result<void> {
    const r = await call<void>(() => pulse.post('/schedules', p));
    if (r.ok) writeAudit(by, 'News schedule', '',
                         p.scope === 'all-firms' ? 'all firms' : p.q.slice(0, 60));
    return r;
  },

  async deleteSchedule(id: string): Result<void> {
    return call<void>(() => pulse.post('/schedules/delete', { id }));
  },

  async runSchedule(id: string): Result<void> {
    return call<void>(() => pulse.post('/schedules/run', { id }));
  },

  async sendTestEmail(recipients: string): Result<void> {
    return call<void>(() => pulse.post('/email-test', { recipients }));
  },
};
