# deploy/ui-image — how the ATLAS UI ships

The React app lives in **`frontend/`**; this folder holds the image build (two-stage
Dockerfile: node build with `--base=/ui/`, then nginx) and the container's server
block. The compose `ui` service builds it from the repo root and the edge proxies
**`https://<host>:8443/ui/`** to it (`/` redirects there). No host port — the UI and
every API share the ONE origin, so the browser needs no CORS and holds no secrets.

## Update / roll the UI

```
docker compose -f deploy/compose/docker-compose.yml build ui
docker compose -f deploy/compose/docker-compose.yml up -d ui
```

CI: `docker build -f deploy/ui-image/Dockerfile . -t <registry>/prism-ui:<tag>`.

## Sign-in postures (compile-time, via build args)

* **Dev (default)**: `UI_DEX_URL` unset → the bundle's `VITE_DEX_URL` is empty → the
  app uses header trust (no token request). Matches the dev backend posture.
* **Dex SSO**: set `UI_DEX_URL=https://<host>:8443` in `deploy/compose/.env` and
  rebuild the ui image. The login screen runs the OIDC password grant against
  `…/dex/token` — same origin, because the edge now proxies `/dex/` to Dex.
  Backend side: run the prod-posture overlay with `--profile sso` as usual.
* **Google**: the backend accepts Google ID tokens via `GATEWAY_OIDC_ISSUERS` /
  `WORKFLOWS_OIDC_ISSUERS` (+ `ALLOWED_DOMAINS`); in the Google console add the edge
  origin to Authorized JavaScript origins. NOTE: the frontend's current "Continue
  with Google" wires a client SECRET + a fixed refresh token into the public bundle —
  that is a DEMO shim, never a production sign-in; use the proper GIS flow before
  enabling Google for real users.

## A UI hosted on a different origin (dev servers)

`http://localhost:5173` (Vite dev) calls the APIs cross-origin: allow it with
`GATEWAY_CORS_ORIGINS=http://localhost:5173` in `deploy/compose/.env`. Auth is a
bearer HEADER, never a cookie — an allowed origin still needs a valid token on every
request.
