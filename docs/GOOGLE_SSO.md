# Google (Gmail) sign-in for PRISM

Lets each person sign in to ATLAS **with their own Google account** — alongside, not
instead of, the Dex username/password sign-in. Both issuers are accepted at once:
Dex keeps serving the demo/E2E identities and Postman, Google serves people.

How it works: the login page renders Google's own account-picker button. Google mints
an **id_token for whoever signed in**; the gateway (and the orchestrator) verify that
token exactly like a Dex one — `accounts.google.com` simply becomes a second accepted
issuer. Roles still come from **Access**: a valid Google account that was never
provisioned by your Admin authenticates but can do **nothing**. The token proves who
you are; the roster decides what you may do.

## 1. Create the Google OAuth client (one-time, ~5 minutes)

In [Google Cloud Console](https://console.cloud.google.com/apis/credentials) →
*Create credentials → OAuth client ID → Web application*:

- **Authorized JavaScript origins** — Google refuses raw IP addresses, so the portal
  needs a hostname. Zero-DNS option: **sslip.io** maps a name onto your IP —
  `https://192-168-44-128.sslip.io:8443` resolves to `192.168.44.128` with no setup.
  (Or use a real domain you own that points at the VM.)
- No redirect URI is needed — the button flow returns the credential in-page.
- If the consent screen is in **Testing** mode, add your colleagues as test users
  (or publish the app for your workspace).

Copy the **client ID** (`…apps.googleusercontent.com`). There is no secret in this
flow — the client id is public by design.

## 2. Configure PRISM — `deploy/compose/.env`

```bash
GOOGLE_SSO_CLIENT_ID=1234567890-abc.apps.googleusercontent.com

# Both issuers, side by side: Google for people, Dex for demo users + Postman/CI.
GATEWAY_OIDC_ISSUERS=https://accounts.google.com|1234567890-abc.apps.googleusercontent.com,http://dex:5556/dex|prism
WORKFLOWS_OIDC_ISSUERS=https://accounts.google.com|1234567890-abc.apps.googleusercontent.com,http://dex:5556/dex|prism

# Which e-mail domains may authenticate. REQUIRED once Google is accepted — any real
# Google account can otherwise present a valid token (they'd still have no roles, but
# don't let them through the door at all). List every domain your team signs in from:
GATEWAY_OIDC_ALLOWED_DOMAINS=evamfinance.com,gmail.com
WORKFLOWS_OIDC_ALLOWED_DOMAINS=evamfinance.com,gmail.com
```

Then rebuild/restart (the client id is compiled into the UI bundle):

```bash
cd deploy/compose
docker compose --profile sso build ui
docker compose --profile sso -f docker-compose.yml -f docker-compose.prod-posture.yml up -d ui gateway orchestrator
```

## 3. Browse via the hostname, not the IP

Open **`https://192-168-44-128.sslip.io:8443/ui/`** — the Google button only works on
the origin registered in step 1, so bookmark the hostname form. (The self-signed
certificate warning behaves the same as before.)

## 4. Provision each person (the roster is the real gate)

Signing in with Google does **not** grant anything. For each colleague, the Admin
creates their Access user under the **exact Gmail address** they sign in with — same
flow as `docs/USER_PROVISIONING.md`, minus the Dex step (Google is their password now):

```
POST {{base}}/access/v1/users
{ "email": "colleague@gmail.com", "full_name": "Their Name",
  "short_name": "Name", "roles": ["BDRM"], "active": true }
```

Un-provisioned Google users get a clear refusal at sign-in, not an empty portal.

## What still signs in through Dex

- The seven demo identities (password `prism`) — the username/password form is
  unchanged, left side of the same login screen.
- Postman collections, the E2E suite, and the k6 load test — all use the Dex password
  grant and keep working untouched.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Google button never renders | `GOOGLE_SSO_CLIENT_ID` empty at ui build, or an ad-blocker blocking `accounts.google.com/gsi/client` |
| Google popup: *origin not allowed* | The browser URL isn't the origin registered in step 1 — use the sslip.io hostname, include the `:8443` |
| Sign-in succeeds at Google, PRISM says the domain is not allowed | Add the domain to both `*_OIDC_ALLOWED_DOMAINS` and `up -d gateway orchestrator` |
| Signed in but "no Access user" error | Provision the exact Gmail address in Access (step 4) |
| Everything worked yesterday, 401 today | Google id_tokens expire hourly — the UI keeps the session, but a long-idle tab may need a fresh sign-in |
