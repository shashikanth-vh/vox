import { writeAudit } from './auditService';

/* Port of v12 AUGMENT 17/18 — email digests + recurring schedules.
   ----------------------------------------------------------------------------
   These are the only Tools features that need a server: v12 posts to the PRISM
   gateway's /api/pulse/* routes, which re-run the search and send the mail.
   This app has no such backend yet, so every call here is STUBBED — the UI is
   complete and the payloads are exactly v12's, but nothing is sent.

   To go live: drop `STUBBED = false` and implement each call against
   /api/pulse/* (the commented endpoint above each method is v12's contract).
   Until then the dialogs surface `NOT_CONNECTED` rather than pretending to send. */

export const STUBBED = true;
export const NOT_CONNECTED =
  'Email and schedules need the PULSE backend (/api/pulse). Not connected — nothing was sent.';

export interface Schedule {
  id: string; q: string; recipients: string; cadence: 'daily' | 'weekly'; weekday: number;
  hour: number; window_days: number; adverse_only: boolean; scope: 'all-firms' | 'terms'; subject: string;
}
export interface DigestGroup { term: string; articles: any[] }

type Result<T> = Promise<{ ok: boolean; error?: string; data?: T }>;
const stub = async <T>(): Result<T> => ({ ok: false, error: NOT_CONNECTED });

export const pulseService = {
  // v12: POST /api/pulse/email  {q, from, to, recipients, subject}
  async emailNews(_p: { q: string; from: string; to: string; recipients: string; subject: string }, by: string): Result<void> {
    if (STUBBED) return stub();
    writeAudit(by, 'News emailed', '', `${_p.q} → ${_p.recipients}`);
    return { ok: true };
  },

  // v12: POST /api/pulse/email_digest  {recipients, subject, groups, adverse_only}
  async emailDigest(_p: { recipients: string; subject: string; groups: DigestGroup[]; adverse_only: boolean }, by: string): Result<{ firms: number; count: number }> {
    if (STUBBED) return stub();
    writeAudit(by, 'News emailed', '', `all firms → ${_p.recipients}`);
    return { ok: true };
  },

  // v12: GET /api/pulse/schedules -> {schedules:[], smtp:bool}
  async listSchedules(): Result<{ schedules: Schedule[]; smtp: boolean }> {
    if (STUBBED) return { ok: false, error: NOT_CONNECTED, data: { schedules: [], smtp: false } };
    return { ok: true };
  },

  // v12: POST /api/pulse/schedules
  async createSchedule(_p: Omit<Schedule, 'id'>, by: string): Result<void> {
    if (STUBBED) return stub();
    writeAudit(by, 'News schedule', '', _p.scope === 'all-firms' ? 'all firms' : _p.q.slice(0, 60));
    return { ok: true };
  },

  // v12: POST /api/pulse/schedules/delete  {id}
  async deleteSchedule(_id: string): Result<void> { return STUBBED ? stub() : { ok: true }; },
  // v12: POST /api/pulse/schedules/run  {id}
  async runSchedule(_id: string): Result<void> { return STUBBED ? stub() : { ok: true }; },
  // v12: POST /api/pulse/email_test  {recipients}
  async sendTestEmail(_recipients: string): Result<void> { return STUBBED ? stub() : { ok: true }; },
};
