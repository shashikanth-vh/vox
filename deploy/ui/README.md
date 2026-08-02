# deploy/ui — the ATLAS UI image

The UI ships as its **own Docker image** (the `Dockerfile` here bakes this folder's
contents into an nginx), and the edge proxies **`https://<host>:8443/ui/`** to it
(`/` redirects there). The UI container publishes no host port — like every service,
it is reachable only through the one door, so the UI and all APIs share ONE origin.

## Publish / update the UI

1. Put the UI in this folder:
   * **SPA build**: copy the build output (`index.html` + assets) here. Build the SPA
     with base path `/ui/` (e.g. Vite: `base: '/ui/'`) so asset URLs resolve.
   * **Single-file prototype**: copy it here **renamed to `index.html`**.
2. Rebuild and roll just the UI:

   ```
   docker compose -f deploy/compose/docker-compose.yml build ui
   docker compose -f deploy/compose/docker-compose.yml up -d ui
   ```

CI can equally `docker build deploy/ui -t <registry>/prism-ui:<tag>` and push; point
the compose service at the tag instead of the build context.

Configure the UI to call the APIs at **relative paths** (`/v1/…`, `/access/…`,
`/orchestrator/…`, `/atlas/…`) — same origin as the page, which is why no CORS setup
is needed and why this is the recommended production shape. Deep links work: unknown
paths under `/ui/` fall back to `index.html`.

## Reaching it from another machine

The edge publishes `8443` (HTTPS) and `8080` (HTTP→HTTPS redirect) on all interfaces.
Open the VM's firewall / cloud security group for those ports and browse to
`https://<vm-address>:8443/`. With the self-signed demo certificate the browser shows
a warning once — install a CA-issued pair at `deploy/nginx/certs/tls.{crt,key}` to
remove it (nothing else changes).

## If the UI is hosted on a DIFFERENT origin (optional)

A UI served by another host, or a local dev server (`http://localhost:5173`), is a
cross-origin caller. Allow its origin at the gateway:

```
GATEWAY_CORS_ORIGINS=https://ui.example.com,http://localhost:5173
```

in `deploy/compose/.env`, then `docker compose up -d gateway`. Auth stays a bearer
HEADER (never a cookie), so an allowed origin grants nothing by itself — every request
still needs a valid token. Leave the variable empty when the UI is served from this
folder: same-origin needs no CORS at all.

## Sign-in — Dex or Google

The backend accepts either issuer; the UI obtains an **ID token** and sends it as
`Authorization: Bearer <token>` on every call.

* **Dex** (`--profile sso`, the default prod posture): the SPA drives the standard
  OIDC code+PKCE flow against `https://<host>:8443/dex` and uses the returned
  `id_token`.
* **Google**: set the multi-issuer knobs in `deploy/compose/.env` (both are already
  plumbed through the prod-posture overlay):

  ```
  GATEWAY_OIDC_ISSUERS=https://accounts.google.com|<client-id>.apps.googleusercontent.com,http://dex:5556/dex|prism
  WORKFLOWS_OIDC_ISSUERS=https://accounts.google.com|<client-id>.apps.googleusercontent.com,http://dex:5556/dex|prism
  ```

  and keep `GATEWAY_OIDC_ALLOWED_DOMAINS=evamfinance.com` so only organisation
  accounts pass. In the Google Cloud console, add the UI's origin
  (`https://<vm-address>:8443`) to the OAuth client's **Authorized JavaScript
  origins** (and the redirect URI if using the code flow); the SPA then uses Google
  Identity Services to obtain the ID token client-side. Google's own token endpoints
  send permissive CORS headers, so no PRISM-side change is needed for the sign-in
  round-trip itself.
