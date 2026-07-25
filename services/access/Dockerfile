# Build context is the REPO ROOT (so the shared evam-backend-core package is available):
#   docker build -f services/access/Dockerfile -t prism-access:0.1.0 .
#
# ---- builder ---------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Shared backend platform package first. --no-deps because its runtime deps are a subset
# of the Register's pinned deps installed in the next step.
COPY packages/evam-backend-core /tmp/evam-backend-core
RUN pip install --upgrade pip && pip install --no-deps /tmp/evam-backend-core

# Register dependencies (separate layer for caching).
COPY services/access/pyproject.toml services/access/README.md ./
RUN pip install .

# Register application code.
COPY services/access/app ./app
COPY services/access/migrations ./migrations
COPY services/access/alembic.ini ./
RUN pip install .

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Run as an unprivileged user.
RUN groupadd -r access && useradd -r -g access access \
    && mkdir -p /app && chown access:access /app

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=access:access services/access/app ./app
COPY --chown=access:access services/access/migrations ./migrations
COPY --chown=access:access services/access/alembic.ini ./
COPY --chown=access:access services/access/scripts/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

USER access
EXPOSE 8000

# Container-level liveness probe (Kubernetes uses its own; this helps docker-compose).
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]
# Gunicorn manages Uvicorn workers; worker count is tuned via env in the entrypoint.
CMD ["serve"]
