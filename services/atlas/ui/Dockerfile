# --- build -------------------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# VITE_* values are read from .env at build time and compiled into the bundle.
# DEX_URL must point at THIS container's public origin (the /dex proxy below),
# not at the :5173 dev server, so override it here.
ARG VITE_DEX_URL=http://localhost:5556
ENV VITE_DEX_URL=${VITE_DEX_URL}

RUN npm run build

# --- serve -------------------------------------------------------------------
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
