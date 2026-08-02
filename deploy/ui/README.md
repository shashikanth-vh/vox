# deploy/ui — the ATLAS UI slot

Anything in this folder is served by the edge NGINX at **`https://<host>:8443/ui/`**
(`/` redirects here). The compose file live-mounts it read-only, so replacing files
takes effect on the next browser reload — no container restart.

## Publish the UI

* **SPA build**: copy the build output (`index.html` + assets) into this folder.
* **Single-file prototype**: copy it here **renamed to `index.html`**.

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
