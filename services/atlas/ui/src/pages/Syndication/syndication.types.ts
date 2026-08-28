// `apiId` fields carry the REGISTER row UUIDs on platform builds (the writes address
// them); mock rows simply leave them unset.
// `amt` is the bank's sanctioned allocation in ₹ Cr (register amount_cr) — set when
// the lender reaches Sanctioned; null until then.
export interface SynLender { apiId?: string; name: string; ex: boolean; st: string; since: string; resp: string; chased: string | null; note: string; amt?: number | null; h?: any[]; heldFrom?: string; }
export interface SynRow {
  apiId?: string; entityId?: string; dealId?: string;
  id: string; code: string; toi: string; rm: string; an: string; lc: string; pri: string;
  status: string; amt: number; synType: string; mstat3: string; fac: string; tenor: string;
  im: string; pot: string; sancL: string; ipL: string; exist: string; price: string;
  pendingWith: string; lenders?: SynLender[]; h?: any[]; createdAt: string; remarks: string; _name?: string;
}
