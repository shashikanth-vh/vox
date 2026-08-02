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
ARG VITE_DEX_URL=""
ARG VITE_TENANT="EVAM"
# The app ships a full MOCK data store for standalone demos and only calls the real
# APIs when this compiles in as 'true'. On the platform the backend IS present, so
# real is the default here — override to 'false' only for a data-free demo build.
ARG VITE_USE_REAL_API="true"
ENV VITE_PRISM_BASE_URL=$VITE_PRISM_BASE_URL \
    VITE_DEX_URL=$VITE_DEX_URL \
    VITE_TENANT=$VITE_TENANT \
    VITE_USE_REAL_API=$VITE_USE_REAL_API
RUN npm run build -- --base=/ui/

# --- serve ------------------------------------------------------------------------
FROM nginx:1.27-alpine
COPY deploy/ui-image/ui-nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html/ui
