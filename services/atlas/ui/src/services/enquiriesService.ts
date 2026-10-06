import { api } from '../api/http';

// Website / WhatsApp enquiries (Leads → Enquiries) — a LIVE-register feature:
// the register parks every enquiry the website posts, mails the approvers, and
// records what became of it. The desk reads the whole list (a few hundred rows)
// and pages locally; "Resend link" mints fresh Approve / Reject links and mails
// them again.

export type EnquiryStage = 'waiting' | 'reminded' | 'escalated' | 'expired' | 'approved' | 'rejected';

export interface EnquiryRow {
  id: string;
  enquiry_no: string;
  channel: string;
  status: string;
  outcome: string;
  stage: EnquiryStage;
  company: string | null;
  contact: string | null;
  mobile: string | null;
  email: string | null;
  intent: string | null;
  need: string | null;
  submitted_at: string | null;
  received_at: string | null;
  approvers: string[];
  approved_by: string | null;
  approved_at: string | null;
  expires_at: string | null;
  reminded_at: string | null;
  escalated_at: string | null;
  lead_id: string | null;
  lead_no: string | null;
  deal_id: string | null;
  rm: string | null;
  note: string | null;
}

export const STAGE_LABEL: Record<EnquiryStage, string> = {
  waiting: 'Waiting', reminded: 'Reminded', escalated: 'Escalated', expired: 'Expired',
  approved: 'Approved', rejected: 'Rejected',
};
export const STAGE_COLOR: Record<EnquiryStage, 'default' | 'primary' | 'warning' | 'error' | 'success'> = {
  waiting: 'primary', reminded: 'warning', escalated: 'warning', expired: 'error',
  approved: 'success', rejected: 'default',
};

export const enquiriesService = {
  async list(): Promise<EnquiryRow[]> {
    const res = await api.get<{ items: EnquiryRow[] }>('/enquiries');
    return (res as any)?.items ?? (res as any) ?? [];
  },
  async resend(id: string, approvers?: string[]): Promise<{ enquiry_no: string; links: { recipient: string | null }[] }> {
    return api.post(`/enquiries/${id}/resend`, approvers?.length ? { approvers } : {});
  },
};
