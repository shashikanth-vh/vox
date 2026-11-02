import axios from 'axios';
import { PULSE_URL, TENANT } from '../api/axiosClient';
import { authHeaders } from '../auth/session';
import { errText } from '../api/http';
import { attach401Recovery } from '../auth/refresh';

/* The mailbox reader (PULSE, /pulse/v1/mail/*): a person's own connection, the
   review queue of conversations the reader could not place, and the Admin's
   coverage view. Every call answers the same {ok, data | error} shape so the
   dialogs can show the SERVER's words when something is refused. */

const mail = axios.create({ baseURL: `${PULSE_URL}/v1/mail`, timeout: 60_000 });
mail.interceptors.request.use((c) => {
  c.headers = { ...(c.headers || {}), 'X-Tenant': TENANT, ...authHeaders() } as any;
  return c;
});
attach401Recovery(mail);

export type MailboxState = 'connected' | 'backfill' | 'reconnect';

export interface MailboxStatus {
  email: string;
  connected: boolean;
  configured: boolean;      // Google client id + secret present on the server
  enabled?: boolean;
  engine?: string;
  client_id?: string;
  name?: string;
  status?: MailboxState;
  error?: string | null;
  since?: string | null;
  connected_at?: number;
  last_run?: number | null;
  last_ok?: number | null;
  reading?: boolean;
  backfill?: { done: boolean; pages: number; threads: number };
  counts?: { seen: number; filed: number; queued: number; errors: number; skipped: Record<string, number> };
  last_pass?: { seen: number; filed: number; queued: number; errors: number; note?: string; skipped: Record<string, number> };
}

export interface QueueItem {
  key: string;
  mailbox: string;
  subject: string;
  first_at: string;
  last_at: string;
  messages: number;
  parties: { email: string; name: string }[];
  excerpt: string;
  suggested: { entity_id: string; name: string; confidence: number; how: string } | null;
  queued_at: number;
}

export interface Coverage {
  items: MailboxStatus[];
  enabled: boolean;
  configured: boolean;
  engine: string;
  store_ok: boolean;
  store_error: string;
  queue: number;
}

type Result<T> = Promise<{ ok: boolean; error?: string; data?: T }>;

async function call<T>(fn: () => Promise<{ data: T }>): Result<T> {
  try {
    const r = await fn();
    return { ok: true, data: r.data };
  } catch (e: any) {
    const d = e?.response?.data;
    const msg = (d && (d.message || errText(d))) || e?.message || 'PULSE did not answer.';
    return { ok: false, error: msg };
  }
}

export const mailboxService = {
  me: () => call<MailboxStatus>(() => mail.get('/me')),
  connect: (code: string, sinceMonths: number, name: string) =>
    call<MailboxStatus>(() => mail.post('/connect', { code, since_months: sinceMonths, name })),
  disconnect: () => call<{ ok: boolean; disconnected: boolean }>(() => mail.post('/disconnect')),
  readNow: () => call<{ ok: boolean; started: boolean }>(() => mail.post('/read-now')),
  queue: () => call<{ items: QueueItem[]; total: number }>(() => mail.get('/queue')),
  attach: (key: string, entityId: string, addDomain: boolean) =>
    call<{ ok: boolean; interaction_id: string; subject_type: string }>(
      () => mail.post('/queue/attach', { key, entity_id: entityId, add_domain: addDomain })),
  ignore: (key: string) => call<{ ok: boolean; ignored: boolean }>(() => mail.post('/queue/ignore', { key })),
  coverage: () => call<Coverage>(() => mail.get('/coverage')),
};

export const when = (t?: number | null) => (t ? new Date(t * 1000).toLocaleString('en-IN', {
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—');
