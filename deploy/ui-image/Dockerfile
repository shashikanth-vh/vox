# The ATLAS UI image, built FROM SOURCE (services/atlas/ui/ — the React app) in two stages.
# Build context = the REPO ROOT (like every service image); compose passes this file
# via `dockerfile:`. CI equivalent:
#   docker build -f deploy/ui-image/Dockerfile . -t <registry>/prism-ui:<tag>
#
# Update the UI: change services/atlas/ui/, then
#   docker compose build ui && docker compose up -d ui

# --- build: tsc + vite, based under /ui/ (the edge serves the app there) ----------
FROM node:22-alpine AS build
WORKDIR /app
COPY services/atlas/ui/package.json services/atlas/ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY services/atlas/ui/ .
# Compile-time endpoints. EMPTY = same-origin relative paths — the PRISM edge shape,
# where the UI and every API share https://<host>:8443. Set VITE_DEX_URL to that same
# public origin to switch the login screen to the OIDC password grant (the edge
# proxies /dex to the sso-profile Dex); empty = dev posture (header trust, no sign-in).
ARG VITE_PRISM_BASE_URL=""
# The register axios client expects its base to CARRY the /v1 prefix (service paths
# are written without it) — origin-relative on the platform.
ARG VITE_API_BASE_URL="/v1"
# The Access/Orchestrator URLs are DERIVED from the base when it is set — but the
# platform build keeps the base EMPTY (origin-relative calls), so name the prefixes
# explicitly; a leading-slash URL resolves against the page's own origin.
ARG VITE_ACCESS_URL="/access"
ARG VITE_ORCHESTRATOR_URL="/orchestrator"
# VocX (field-intel capture) rides the same edge; the gateway injects VocX's own
# front-door key, so the browser never holds it.
ARG VITE_VOCX_URL="/vocx"
ARG VITE_DEX_URL=""
# PER-USER Google sign-in (a PUBLIC OAuth client id — no secret involved). Set it and
# the login screen renders Google's own account-picker button alongside the Dex
# username/password form; leave it empty and only Dex sign-in shows.
# The gateway must then accept accounts.google.com too — see docs/GOOGLE_SSO.md.
ARG VITE_GOOGLE_SSO_CLIENT_ID=""
ARG VITE_TENANT="EVAM"
# The app ships a full MOCK data store for standalone demos and only calls the real
# APIs when this compiles in as 'true'. On the platform the backend IS present, so
# real is the default here — override to 'false' only for a data-free demo build.
ARG VITE_USE_REAL_API="true"
# On the platform, a failed API call must LOOK failed — never silently render the
# bundled mock instead (that masquerade cost two debugging rounds). Demo builds may
# set 'true' to restore the soft fallback.
ARG VITE_MOCK_FALLBACK="false"
ENV VITE_PRISM_BASE_URL=$VITE_PRISM_BASE_URL \
    VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_MOCK_FALLBACK=$VITE_MOCK_FALLBACK \
    VITE_ACCESS_URL=$VITE_ACCESS_URL \
    VITE_ORCHESTRATOR_URL=$VITE_ORCHESTRATOR_URL \
    VITE_VOCX_URL=$VITE_VOCX_URL \
    VITE_DEX_URL=$VITE_DEX_URL \
    VITE_GOOGLE_SSO_CLIENT_ID=$VITE_GOOGLE_SSO_CLIENT_ID \
    VITE_TENANT=$VITE_TENANT \
    VITE_USE_REAL_API=$VITE_USE_REAL_API
RUN npm run build -- --base=/ui/

# --- serve ------------------------------------------------------------------------
FROM nginx:1.27-alpine
COPY deploy/ui-image/ui-nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html/ui
