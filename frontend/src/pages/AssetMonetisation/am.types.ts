export interface AmRow {
  id: string; code: string; state: string; val: number; mw: number; nature: string;
  dtype: string; inv: string; itype: string; status: string; teaser: string | null;
  createdAt: string; notes: string; _name?: string;
  /** The deal this line hangs off (`deal_id`), when the API supplied it. */
  dealId?: string;
}
