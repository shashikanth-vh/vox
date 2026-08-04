Google OAuth client secret for the VocX pipeline goes here as `client_secret.json`.
Git-ignored — NEVER commit it, and it must never appear in a delivered zip.
**File absent = the Google integration is off**: follow-ups are still recorded on the
register, no calendar event is created, and the panel says so rather than failing.

Turning it on takes three things, and all three must agree:

1. **The client secret.** In Google Cloud Console create an OAuth 2.0 Client ID of type
   *Web application*, download the JSON, and save it here as `client_secret.json`.
   Compose mounts this directory read-only at `/run/vocx-secrets`.

2. **The redirect URI — as the BROWSER reaches it.** Set `VOCX_REDIRECT_URI` in
   `deploy/compose/.env` to the address people actually type, through the edge:

       VOCX_REDIRECT_URI=https://<host>:8443/vocx/v1/auth/callback

   The default is `https://localhost:8443/...`, which is wrong for every deployment
   reached by IP or hostname. Add this EXACT string to the client's *Authorised redirect
   URIs* in Google Cloud Console — Google matches it character for character.

3. **Restart vocx** so it re-reads both.

Then each RM connects their OWN account from the capture panel's follow-up banner
("Connect Google"). Tokens are stored server-side per person under `VOCX_TOKENS_DIR`,
which is a volume — never in the image, never in the repo.

Two things worth knowing:

* `GET /v1/capabilities` reports `google_configured` — the DEPLOYMENT fact. Whether a
  given person has connected is `GET /v1/auth/status`, and only that second one means a
  calendar event will actually be created for them.
* A self-signed edge certificate is fine for the app but Google will refuse to redirect
  to a host it cannot verify in some browsers. If the callback lands on a warning page,
  accept the certificate once at `https://<host>:8443/` first.
