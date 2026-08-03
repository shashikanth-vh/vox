// One row of the syndication ledger for a lender (v12 `e.cos`).
export interface FiDeal { co: string; code: string; st?: string; resp?: string; amt: number }

// A row of the full deal ledger (v12 openBank) — carries the mandate context too.
export interface FiLedgerRow {
  code: string; co: string; amt: number; st?: string; resp?: string;
  note?: string; synStatus?: string; rm?: string; an?: string;
}

export interface FiRow {
  apiId?: string;  // the register counterparty UUID on platform builds (writes address it)
  name: string; type?: string; notes?: string; inactive?: boolean;
  preferredSectors?: string; sectors?: string; engagements: number; _i: number;
  // v12 engagement rollup: every mandate this lender was put on, by outcome.
  pursued: number; live: number; ip: number; sanc: number; decl: number;
  cos: FiDeal[];
}
