# EVAM ATLAS — React package

Enterprise deal-flow & pipeline console, rebuilt from the `ATLAS_EVAM_v9` HTML prototype into a production-shaped React application.

## Stack (as specified)
- **React 18 + TypeScript** (Vite)
- **Material UI v5** — all components default to small/medium (see `src/theme.ts`)
- **Material React Table v2** — the common data grid
- **TanStack Query v5** — server-state; drives all pagination/sorting/filtering
- **React Router v6** — routing
- **Recharts** — dashboard charts (free/OSS)
- **Axios** — API client, base URL from `.env`

## Run it
```bash
cd atlas-react
npm install
npm run dev      # http://localhost:5173
```

> ⚠️ This package was authored in a design workspace that renders HTML, so it was **not** compiled/booted here. Run `npm install && npm run dev` in your IDE and fix any small type nits your exact MRT/MUI minor versions surface. The architecture, data model, RBAC, services, and all screens are complete.

## Backend configuration (.env)
```
VITE_API_BASE_URL=http://localhost:8080/api
VITE_USE_REAL_API=false
```
Requirements 9 & 22: every screen reads data through the **services layer**, which is now **API-first**. Set `VITE_USE_REAL_API=true` and every service calls your backend via `src/api/http.ts` (`axiosClient` + `VITE_API_BASE_URL`); with it `false` (or on any request error) it transparently falls back to the bundled seed JSON (`src/api/mockData/atlasData.json`) so the app runs offline today.

- **Reads** (`list`, `getRef`, `getRoles`, `getDashboard`) go through `withFallback(real, mock)`; list calls send `toParams(query)` so pagination/sorting/filtering are **server-side** (req 16).
- **Writes** call `remote(method, url, body)` — dispatched to the backend when real-API is on, while still applying the optimistic local-store mutation so the UI stays instant.

### Expected REST contract
```
GET  /leads|/deals|/lending|/syndication|/asset-mon|/clients|/employees|/fi|/audit
       ?page&size&q&sortBy&sortDir&filter.<col>   ->  { rows: T[], total: number }
GET  /reference   ->  Record<string,string[]>      GET /roles ->  { roles, roleTabs }
GET  /dashboard   ->  DashboardData
POST /leads   PATCH /leads/:id   DELETE /leads/:id   POST /leads/:id/push
PATCH /deals/:code   POST /deals/:code/products
PATCH /lending/:id   PATCH /lending/:id/stage   DELETE /lending/:id
PATCH /syndication/:id   DELETE /syndication/:id
POST  /syndication/:code/lenders   PATCH /syndication/:code/lenders/:name
POST  /syndication/:code/lenders/:name/chase|/response
PATCH /syndication/matrix/:code/:lender   PATCH /syndication/matrix/lender-order
PATCH /clients/:code   PATCH /fi/:index
POST  /employees   PATCH /employees/:name   DELETE /employees/:name
```

## Industry folder structure
```
src/
  api/            axiosClient, in-memory store, mock JSON, server-style query engine
  auth/           AuthContext, rbac.ts (ROLE_TABS), RoleGuard
  context/        SearchContext (navbar search -> active table)
  components/
    layout/       AppLayout, Navbar, navConfig
    table/        CommonTable   <-- reusable MRT grid (req 11-13,16,17)
    common/       StatCard, ConfirmDialog, Pills, Field helpers, PageHint
  services/       one service per entity + shared types.ts
  pages/          one folder per page, each with its own <name>.types.ts (req 18)
                  Home Today Dashboard Leads Deals Lending Syndication
                  AssetMonetisation FIMaster Clients Employees Audit
  theme.ts        MUI theme + ATLAS palette tokens
```

## Requirement checklist
1–8. React + MUI + MRT + TS + TanStack + Recharts + Routing + Services layer — done.
9. All JSON served from services (mock now, swap to API later) — `src/services/*`.
10. Models/interfaces per entity — `pages/<X>/<x>.types.ts` + `services/types.ts`.
11. `CommonTable` toolbar order: **Clear filters → global-search toggle → filters toggle → columns toggle → fullscreen (expand) → download**; sticky right-pinned **Actions** column with View / Edit / Delete; sort + filter affordances on the right of each header.
12. Compact pagination + filter rows (reduced height, 11.5px font, vertically centred) — `muiBottomToolbarProps` / `muiFilterTextFieldProps` in `CommonTable.tsx`.
13. MUI components default to small/medium via theme `defaultProps`.
14. RBAC — `auth/rbac.ts` `ROLE_TABS` (RM, Analyst, Ops, Management, Admin); `RoleGuard` hides routes; Management is read-only (`isReadOnly`) across every write.
15. Navbar search feeds the active page's table via `SearchContext` (server-side global filter).
16. Pagination/sorting/filtering all manual/server-driven (TanStack Query + `applyQuery`).
17. Common components: `CommonTable`, `StatCard`, `ConfirmDialog`, `CompanyDrawer`, Field kit.
18. Each page owns its folder + interfaces.
22. `.env` backend URL.
23. Roles served from backend json — `referenceService.getRoles()`.
27–30. Popups & flows reproduced: **Add lead** (with duplicate detection), **Push to Deals** (mints Group Code, creates client+deal+product rows, converts the lead), **Add product**, universal **Company drawer** (edit client/deal/lending/syndication/AM inline, add lenders), inline stage/status editing that stamps history, delete confirmations, Export/Reset.

## Scope notes (next iteration)
- **Syndication** ships with three view modes: the lender-wise **register** table, the interactive **dot-matrix** (companies × lenders; click a dot to advance state, drag columns to reorder, state/dwell/preset filters, live/closed/all scope, CSV export), and the **chase heat view** (`ChaseView.tsx` — per-company lender cards heat-tinted by silence, an attention strip of lenders needing a nudge, inline status change, and Chase/Response interaction logging) — all over `syndicationService`.
- **Dashboard** implements the tiles, sector composition, funnel, and the core attention rules (stale lead / lending stuck / sanctioned-undisbursed). The prototype's fuller MIS (lender scoring, velocity medians, people productivity, pendency) can be layered onto `dashboardService` — the raw data is all present.
- **Login** — `pages/Login/LoginPage.tsx` gates the app: unauthenticated users see the ATLAS sign-in card (username/password + Google, matching the prototype); `signIn()` in `AuthContext` unlocks the routed app, and the navbar's **Sign out** returns to it. It is now wired to **PRISM** through `services/authService.ts` — see below.

## Login / PRISM auth
Built from `api.json` (folders **00b · Sign in (Dex)**, **00c · Sign in (Google)**, **01 · Users, roles & people**) and `env.json`. PRISM has **no `/auth/login`** endpoint; signing in is two steps:

1. **Get an identity.**
   - *Production posture* — `POST {VITE_DEX_URL}/dex/token`, an OIDC **password grant** (`grant_type=password`, `client_id=prism`, `scope=openid email profile`). The gateway validates the **`id_token`**, so that (not `access_token`) is what the app keeps.
   - *Dev posture* — `VITE_DEX_URL` empty ⇒ **no sign-in request at all**; the gateway trusts `X-User-Email` / `X-User-Roles`. Local development only: nothing verifies the password.
   - *Google* — **Continue with Google** replays folder 00c's **refresh grant** directly from the browser: `POST https://oauth2.googleapis.com/token` with `grant_type=refresh_token` + `client_id` + `client_secret` + `refresh_token`, then signs in with the returned `id_token`. The username/password fields are not involved or validated. Configure `VITE_GOOGLE_CLIENT_ID` / `VITE_GOOGLE_CLIENT_SECRET` / `VITE_GOOGLE_REFRESH_TOKEN` (the `googleClientId` / `googleClientSecret` / `adminRefreshToken` from `env.json`). **Two consequences to be aware of:** `VITE_*` values are compiled into the public bundle, so the secret and refresh token ship to the client; and the refresh token is a single fixed account, so every click signs in as that user. `authService.signInWithIdToken(idToken)` remains available for an id_token minted elsewhere (e.g. Google Identity Services).
2. **Resolve who that identity is**, from Access — `GET /access/v1/users?q=<email>` (id, full name, roles) and `GET /access/v1/resolve?email=<email>` (effective view matrix, e.g. `views.leads = 'SCOPED'`). Access role names are the same vocabulary as `auth/rbac.ts` `ROLES`, so no translation is needed. A `/resolve` failure is non-fatal — the app falls back to the local RBAC matrix.

The resulting session (`auth/session.ts`) is held in **sessionStorage** and every outbound request carries it via an axios interceptor: `Authorization: Bearer <id_token>` for the prod posture plus `X-Tenant` / `X-Actor` / `X-User-Email` / `X-User-Roles` for the dev one.

`VITE_USE_REAL_API=false` keeps the previous **offline mock sign-in** (any credentials, lands on Admin, no network). Set it `true` for real PRISM sign-in, where bad credentials, an unprovisioned e-mail, or a deactivated account surface as errors on the login card.

## Reference
The original design prototype `ATLAS_EVAM_v9_final.html` is included at this folder's root for pixel/behaviour reference.
