import axios from 'axios';
import { authHeaders } from '../auth/session';

// Requirement 22: backend URL is configured through .env
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
export const USE_REAL_API = import.meta.env.VITE_USE_REAL_API === 'true';

// ---------------------------------------------------------------------------
// PRISM endpoints — mirrors env.json ("PRISM — Full (via NGINX)").
// Every request enters at ONE door ({{baseUrl}} = https://<host>:8443); the gateway
// routes by prefix: /access -> Access, /orchestrator -> the workflow plane, anything
// else -> the Register. accessUrl/orchestratorUrl are therefore derived from the base
// unless explicitly overridden.
// ---------------------------------------------------------------------------
export const PRISM_BASE_URL: string = import.meta.env.VITE_PRISM_BASE_URL || '';
export const ACCESS_URL: string =
  import.meta.env.VITE_ACCESS_URL || (PRISM_BASE_URL ? `${PRISM_BASE_URL}/access` : '');
export const ORCHESTRATOR_URL: string =
  import.meta.env.VITE_ORCHESTRATOR_URL || (PRISM_BASE_URL ? `${PRISM_BASE_URL}/orchestrator` : '');

// Empty dexUrl = the DEV POSTURE: no sign-in request at all, identity is header-trusted.
// env.json ships it empty for precisely that reason.
export const DEX_URL: string = import.meta.env.VITE_DEX_URL || '';
export const OIDC_CLIENT_ID: string = import.meta.env.VITE_OIDC_CLIENT_ID || 'prism';
export const TENANT: string = import.meta.env.VITE_TENANT || 'EVAM';

// --- Google (api.json folder 00c · refresh grant) ---------------------------
// "Continue with Google" replays folder 00c verbatim from the browser. NOTE that this
// puts the client SECRET and a refresh token into the public bundle — every VITE_* value
// is compiled in and readable by anyone who opens devtools — and that the refresh token
// is one fixed account, so the button always signs in as that user.
export const GOOGLE_CLIENT_ID: string = import.meta.env.VITE_GOOGLE_CLIENT_ID || '';
export const GOOGLE_CLIENT_SECRET: string = import.meta.env.VITE_GOOGLE_CLIENT_SECRET || '';
export const GOOGLE_REFRESH_TOKEN: string = import.meta.env.VITE_GOOGLE_REFRESH_TOKEN || '';
// PER-USER Google sign-in (Google Identity Services). A public OAuth client id — safe
// in the bundle; no secret is involved in the credential flow. Setting it switches the
// login screen's Google button from the fixed-identity dev shortcut above to a real
// "pick your account" sign-in, verified by the gateway (accounts.google.com must be in
// GATEWAY_OIDC_ISSUERS / WORKFLOWS_OIDC_ISSUERS — see docs/GOOGLE_SSO.md).
export const GOOGLE_SSO_CLIENT_ID: string = import.meta.env.VITE_GOOGLE_SSO_CLIENT_ID || '';
// Google's token endpoint does answer cross-origin, so the direct URL is the default.
// Override it with the dev-server proxy path if a CSP or a corporate proxy gets in the way.
export const GOOGLE_TOKEN_URL: string =
  import.meta.env.VITE_GOOGLE_TOKEN_URL || 'https://oauth2.googleapis.com/token';


/**
 * A FormData body must NOT travel as application/json.
 *
 * Both clients default `Content-Type: application/json`, which is right for every JSON
 * call and wrong for exactly one thing: a multipart upload. With the header pinned, axios
 * stops generating the `multipart/form-data; boundary=…` value the browser would
 * otherwise supply, so the server is handed a multipart body labelled JSON, parses no
 * parts, and answers `422 missing: body.file` — a file that was definitely attached,
 * reported as absent. Dropping the header lets the browser set it, boundary and all.
 */
function withBodyContentType<T extends { data?: unknown; headers: any }>(cfg: T): T {
  if (typeof FormData !== 'undefined' && cfg.data instanceof FormData) {
    cfg.headers.delete('Content-Type');
  }
  return cfg;
}

const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * The 15s default is for API chatter, and it KILLED file transfers: a scanned
 * financials PDF on a slow uplink takes longer than that to even leave the browser.
 * Any request carrying a file (FormData) or fetching bytes (blob) gets a
 * transfer-sized window instead — unless the caller already set its own.
 */
function withTransferTimeout<T extends { data?: unknown; responseType?: string;
                                         timeout?: number }>(cfg: T): T {
  if (cfg.timeout && cfg.timeout !== DEFAULT_TIMEOUT_MS) return cfg;
  if (typeof FormData !== 'undefined' && cfg.data instanceof FormData) cfg.timeout = 180_000;
  else if (cfg.responseType === 'blob') cfg.timeout = 120_000;
  return cfg;
}

const axiosClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
});

// Every call travels with the signed-in identity: the bearer for the prod posture and
// the X-* headers for the dev one. Read per-request (not captured at module load) so a
// sign-in or sign-out takes effect immediately.
axiosClient.interceptors.request.use((cfg) => {
  Object.entries(authHeaders()).forEach(([k, v]) => cfg.headers.set(k, v));
  return withTransferTimeout(withBodyContentType(cfg));
});

axiosClient.interceptors.response.use(
  (res) => res,
  (err) => Promise.reject(err),
);

// A second client rooted at the ORIGIN (PRISM_BASE_URL, '' = same origin) rather than
// the /v1 register base — for gateway prefixes like /atlas/v1/* that must not inherit
// the register prefix. Same identity interceptor.
export const gwClient = axios.create({
  baseURL: PRISM_BASE_URL || '',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
});
gwClient.interceptors.request.use((cfg) => {
  Object.entries(authHeaders()).forEach(([k, v]) => cfg.headers.set(k, v));
  return withTransferTimeout(withBodyContentType(cfg));
});

export default axiosClient;
