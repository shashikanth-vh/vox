export interface Lead {
  id: string;
  company: string;
  sector: string;
  lens: 'Mitigation' | 'Adaptation' | string;
  source: string;
  sourceDetail: string;
  rm: string;
  an?: string;
  status: 'Active' | 'Converted' | 'Dropped' | string;
  temp: 'Hot' | 'Warm' | 'Cold' | string;
  contact: string;
  designation?: string;
  phone: string;
  last: string;
  next: string;
  nextDate?: string | null;
  conv: string;
  createdAt: string;
  notes: string;
  /** The Register entity this lead hangs off — the id POST /v1/leads carries. */
  entityId?: string;
  /**
   * The API's own row id (a UUID). `id` above is the human lead_no shown in the grid,
   * so the two are kept apart: URLs address a lead by this, columns render `id`.
   */
  apiId?: string;
}
