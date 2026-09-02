export interface LendingHist { stage: string; t: string; by: string; }
export interface LendingRow {
  dealRm?: string; dealAn?: string;
  id: string; code: string; amt: number; rm: string; an: string;
  stage: string; updated: string; sanc: string | null; pendingWith: string;
  h?: LendingHist[]; createdAt: string; remarks: string; _name?: string;
  /** The deal this line hangs off (`deal_id`), when the API supplied it. */
  dealId?: string;
  /** The borrower entity (`entity_id`) — the LMS covenant tab reads by it. */
  entityId?: string;
  /** Drawdown proposed on the way to Ready for Disbursement. */
  proposedAmt?: number;
  proposedDate?: string | null;
}
