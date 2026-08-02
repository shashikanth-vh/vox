/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_USE_REAL_API: string;
  // PRISM (see env.json) — one door at VITE_PRISM_BASE_URL; /access and /orchestrator
  // are derived from it unless overridden.
  readonly VITE_PRISM_BASE_URL: string;
  readonly VITE_ACCESS_URL: string;
  readonly VITE_ORCHESTRATOR_URL: string;
  // Empty VITE_DEX_URL = the dev posture (header trust, no sign-in request). Set = the
  // OIDC posture; login POSTs the password grant to {VITE_DEX_URL}/dex/token.
  readonly VITE_DEX_URL: string;
  // Where the dev server proxies /dex (vite.config.ts only — never read by the app).
  readonly VITE_DEX_TARGET: string;
  readonly VITE_OIDC_CLIENT_ID: string;
  readonly VITE_TENANT: string;
  // X-Actor on the Access plane (user provisioning). Defaults to "e2e-runner".
  readonly VITE_ACCESS_ACTOR: string;
}
interface ImportMeta { readonly env: ImportMetaEnv; }
