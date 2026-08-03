export interface Employee {
  name: string; full: string; role: string; username?: string; email?: string; phone?: string;
  geography?: string; sectors?: string; startedOn?: string; reportsTo?: string;
  inactive?: boolean; notes?: string;
  /** Access user id (`POST /access/v1/users` -> id). Present only for provisioned users. */
  accessId?: string;
  /** Register `people.id` — set when the roster came from GET /v1/people. */
  registerId?: string;
}

export interface BookRollup {
  leads: number; activeLeads: number; deals: number; lend: number; syn: number; am: number; total: number;
}
