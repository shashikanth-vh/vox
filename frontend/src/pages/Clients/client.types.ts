export interface Client {
  name: string;
  sector: string;
  lens: string;
  state: string;
  about: string;
  toi?: string;
  notes?: string;
  lifecycle?: string;
  /** Register entity id (`POST /v1/entities` -> id). Present only for clients written to PRISM. */
  entityId?: string;
  /** The Register's own unique code (e.g. ECOSOCH-123456) — distinct from the local group code. */
  entityCode?: string;
}
export interface ClientRow extends Client { code: string; }
