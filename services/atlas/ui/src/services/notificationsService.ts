import { api } from '../api/http';

/**
 * The in-app inbox: durable notifications minted when something DECIDED about your
 * work — a checker approved/returned/rejected your checklist, your booking settled,
 * the committee decided your deal. Written by the register (in-transaction with the
 * decision) and by the workflow plane's ops events; read here for Today's
 * "Decisions on your work" strip. Never throws — a broken inbox must not take
 * Today down.
 */
export interface InboxItem {
  id: string;
  event: string;
  severity: 'info' | 'warning' | 'critical' | string;
  title: string;
  body?: string | null;
  subject_type?: string | null;
  subject_id?: string | null;
  created_at?: string | null;
  read_at?: string | null;
}

export const notificationsService = {
  async unread(): Promise<{ items: InboxItem[]; unread: number }> {
    try {
      const r = await api.get<any>('/notifications', { unread_only: true, limit: 20 });
      return { items: r?.items || [], unread: Number(r?.unread || 0) };
    } catch {
      return { items: [], unread: 0 };
    }
  },

  /** Mark one read — it leaves the strip; the record stays in the inbox store. */
  async markRead(id: string): Promise<void> {
    try { await api.post(`/notifications/${id}/read`); } catch { /* stays unread */ }
  },
};
