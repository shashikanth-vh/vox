import { api, errText } from '../api/http';

/**
 * LMS — the servicing side of Lending (post-disbursement). Thin, typed calls onto the
 * register's loan-account and covenant APIs; no judgement lives here. Every error comes
 * back in the words the register used.
 */

export interface LoanAccount {
  id: string; lending_id: string; account_no: number;
  borrower?: string; facility_type?: string; disbursed_on?: string;
  amount?: number; rate_kind?: string; rate_pct?: number; tenor_months?: number;
  emi_amount?: number; repayment_start?: string; day_count?: string;
  status: string; overdue_position?: string; provisioning_amount?: number;
  closed_on?: string; note?: string;
}

export interface LedgerEntry {
  entry_no: number; entry_date: string; particulars: string; entry_type: string;
  debit?: number; credit?: number; balance: number;
}

export interface TrancheItem {
  id: string; lending_id: string; tranche_ref: string; amount?: number;
  disbursed_on?: string; advaya_reference?: string; note?: string;
  recorded_by?: string; booking_status: 'Pending' | 'Booked' | 'Rejected' | string;
  booked_by?: string; booked_at?: string; booking_note?: string;
  tranche_no?: string | null;
  /** Point-in-time disclosure stamped at recording: the CP/CS conditions that were
   *  open on the line when this tranche was recorded — frozen even after they clear. */
  conditions_open?: { key: string; label: string; condition_type: string;
    status: string; expiry_date?: string }[];
  // pending-queue extras
  stage?: string; entity_id?: string; borrower?: string | null;
}

export interface TrancheSchedule {
  lending_id: string; stage: string; items: TrancheItem[];
  total_disbursed: number; total_pending: number; ceiling?: number | null;
  fully_disbursed: boolean; remaining?: number | null;
}

export interface AccountCondition {
  key: string; label: string; condition_type: string; required: boolean;
  status: string; reason?: string; expiry_date?: string; evidence_ref?: string;
  source_version?: number; completed_on?: string; completed_by?: string; note?: string;
}

export interface Covenant {
  id: string; entity_id: string; lending_id?: string; name: string;
  covenant_type: string; description?: string; metric?: string; operator?: string;
  threshold?: number; frequency: string; first_due_on?: string; grace_days: number;
  breach_severity: string; is_active: boolean;
}

export interface Observation {
  id: string; entity_id: string; covenant_name?: string; due_date?: string;
  submitted_date?: string; on_time?: boolean; delay_days?: number; period?: string;
  status: string; target_value?: number; actual_value?: number; breached?: boolean;
  waiver_status?: string; waiver_valid_until?: string; waiver_decision_ref?: string;
  details?: Record<string, any>;
}

function msg(e: any, what: string): string {
  return errText(e?.response?.data) || e?.message || `Could not ${what}.`;
}

export const lmsService = {
  /** The account + full statement ledger; null when no account opened yet. */
  async account(lendingId: string): Promise<{ account: LoanAccount; entries: LedgerEntry[] } | null> {
    try {
      return await api.get<any>(`/lending/${lendingId}/loan-account`);
    } catch (e: any) {
      if (e?.response?.status === 404) return null;
      throw new Error(msg(e, 'read the loan account'));
    }
  },

  /** The interest CALCULATOR — shows the inputs and formula; nothing is written. */
  async interestPreview(lendingId: string, upto: string): Promise<{
    from: string; upto: string; days: number; balance: number; rate_pct: number;
    day_count: string; interest: number; formula: string;
  }> {
    try {
      return await api.get<any>(`/lending/${lendingId}/loan-account/interest-preview`, { upto });
    } catch (e) { throw new Error(msg(e, 'compute the interest')); }
  },

  async accrue(lendingId: string, upto: string): Promise<LedgerEntry> {
    try {
      return await api.post<any>(`/lending/${lendingId}/loan-account/accrue`, { upto });
    } catch (e) { throw new Error(msg(e, 'write the interest row')); }
  },

  async addEntry(lendingId: string, input: {
    entry_date: string; kind: 'EMI' | 'Receipt' | 'Charge' | 'Adjustment';
    amount: number; side?: 'debit' | 'credit'; particulars?: string;
  }): Promise<LedgerEntry> {
    try {
      return await api.post<any>(`/lending/${lendingId}/loan-account/entries`, input);
    } catch (e) { throw new Error(msg(e, 'record the ledger entry')); }
  },

  /** FILL a missing repayment term (tenure/rate) — the repair lane for an account
   *  that opened before its terms were complete; the EMI recomputes on the spot.
   *  Recorded values cannot be changed here (that is an amendment). */
  async setAccountTerms(lendingId: string, input: {
    tenor_months?: number; rate_pct?: number; repayment_start?: string;
  }): Promise<LoanAccount> {
    try {
      const r = await api.post<any>(`/lending/${lendingId}/loan-account/terms`, input);
      return r.account;
    } catch (e) { throw new Error(msg(e, 'set the repayment terms')); }
  },

  /** Classification / provisioning / closure — the AUTHORIZER's verbs. */
  async patchAccount(lendingId: string, input: Partial<{
    status: string; overdue_position: string; provisioning_amount: number;
    closed_on: string; note: string;
  }>): Promise<LoanAccount> {
    try {
      return await api.patch<any>(`/lending/${lendingId}/loan-account`, input);
    } catch (e) { throw new Error(msg(e, 'update the account')); }
  },

  // ---- tranche bookings (increment ⑥) --------------------------------------
  /** The tranche schedule with booking states and remaining headroom. */
  async tranches(lendingId: string): Promise<TrancheSchedule | null> {
    try {
      return await api.get<any>(`/lending/${lendingId}/tranches`);
    } catch (e: any) {
      if (e?.response?.status === 404) return null;
      throw new Error(msg(e, 'read the tranche schedule'));
    }
  },

  /** The MAKER's recorder (T2, T3, … from LMS) — lands as a PENDING booking. */
  async recordTranche(lendingId: string, input: {
    tranche_ref: string; amount: number; disbursed_on?: string; note?: string;
  }): Promise<TrancheItem> {
    try {
      return await api.post<any>(`/lending/${lendingId}/tranches`, input);
    } catch (e) { throw new Error(msg(e, 'record the tranche')); }
  },

  /** The AUTHORIZER's queue: every tranche awaiting booking approval, whole-book. */
  async pendingBookings(): Promise<TrancheItem[]> {
    try {
      const out = await api.get<any>('/bookings/pending');
      return out.items || [];
    } catch (e) { throw new Error(msg(e, 'read the pending bookings')); }
  },

  /** Settle a pending booking — approval opens/grows the account in the same
   *  transaction; rejection needs the reason. Four-eyes is enforced server-side. */
  async book(lendingId: string, trancheId: string,
             action: 'approve' | 'reject', note?: string): Promise<TrancheItem> {
    try {
      return await api.post<any>(`/lending/${lendingId}/tranches/${trancheId}/book`,
        { action, ...(note ? { note } : {}) });
    } catch (e) { throw new Error(msg(e, `${action} the booking`)); }
  },

  /** The account's OWN conditions register — the complete checklist handed over from
   *  LOS at account opening (completed and open items alike), owned by servicing from
   *  then on. Null until the account exists. */
  async accountConditions(lendingId: string): Promise<{ items: AccountCondition[]; open: number } | null> {
    try {
      return await api.get<any>(`/lending/${lendingId}/loan-account/conditions`);
    } catch (e: any) {
      if (e?.response?.status === 404) return null;
      throw new Error(msg(e, 'read the conditions register'));
    }
  },

  /** The servicing MAKER retires an obligation on the LMS's own record. */
  async receiveCondition(lendingId: string, key: string,
                         input?: { evidence_ref?: string; note?: string }): Promise<AccountCondition> {
    try {
      return await api.post<any>(
        `/lending/${lendingId}/loan-account/conditions/${encodeURIComponent(key)}/receive`,
        input ?? {});
    } catch (e) { throw new Error(msg(e, 'record the receipt')); }
  },

  /** A post-disbursement obligation discovered later joins the account's register. */
  async addCondition(lendingId: string, input: {
    key: string; label: string; expiry_date?: string; note?: string;
  }): Promise<AccountCondition> {
    try {
      return await api.post<any>(`/lending/${lendingId}/loan-account/conditions`, input);
    } catch (e) { throw new Error(msg(e, 'add the condition')); }
  },

  // ---- covenants -----------------------------------------------------------
  async covenants(entityId: string): Promise<Covenant[]> {
    try {
      const out = await api.get<any>('/covenants', { entity_id: entityId });
      return out.items || [];
    } catch (e) { throw new Error(msg(e, 'read the covenant register')); }
  },

  async observations(entityId: string, lendingId?: string): Promise<Observation[]> {
    try {
      const out = await api.get<any>('/covenants/observations',
        { entity_id: entityId, ...(lendingId ? { lending_id: lendingId } : {}) });
      return out.items || [];
    } catch (e) { throw new Error(msg(e, 'read the covenant observations')); }
  },

  /** Submit a period's result — the register computes breach; a breach auto-opens
   *  an EWS case in the same transaction. */
  async submitResult(monitoringId: string, input: {
    actual_value?: number; submitted_on?: string; note?: string;
  }): Promise<Observation & { ews_case_id?: string }> {
    try {
      return await api.post<any>(`/monitoring/${monitoringId}/result`, input);
    } catch (e) { throw new Error(msg(e, 'submit the result')); }
  },

  /** Waive a live breach — takes effect only against a recorded, time-boxed
   *  waiver decision (the register verifies the record, never a claim). */
  async waive(monitoringId: string, decisionRef: string, note?: string): Promise<Observation> {
    try {
      return await api.post<any>(`/monitoring/${monitoringId}/waive`,
        { decision_ref: decisionRef, ...(note ? { note } : {}) });
    } catch (e) { throw new Error(msg(e, 'apply the waiver')); }
  },
};
