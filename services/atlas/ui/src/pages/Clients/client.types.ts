/** A director or promoter the News Radar watches with the firm. `city` narrows a
 *  common name to the one who runs this company. */
export interface KeyPerson {
  name: string;
  role?: string;
  city?: string;
}
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
  /** Optional identity — the CIN anchors the 360's market feed (Tracxn). */
  cin?: string;
  city?: string;
  country?: string;
  address?: string;
  /** Directors / promoters searched in the news digest alongside the firm. */
  keyPeople?: KeyPerson[];
  /** Names the news digest searches the firm by ("Enerparc"); when set, the whole list. */
  newsAliases?: string[];
  /** false = left out of the news digest (a placeholder, a place, a person). */
  newsWatch?: boolean;
}
export interface ClientRow extends Client { code: string; }
