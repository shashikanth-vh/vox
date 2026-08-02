export interface Deal {
  code: string;
  rm: string;
  an: string;
  lend: boolean;
  syn: boolean;
  am: boolean;
  temp: string;
  source: string;
  sourceDetail: string;
  createdAt: string;
  remarks: string;
  /** The API's own row id (a UUID). `code` is the group code the grid shows. */
  apiId?: string;
  /** The Register entity this deal hangs off. */
  entityId?: string;
  /** Workflow stage as the API reports it (Sanctioned, …) — read-only here. */
  stage?: string;
  productType?: string;
  amountCr?: number;
}
export interface DealRow extends Deal { _name?: string; lens?: string; }
