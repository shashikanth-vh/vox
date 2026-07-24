# Build context is the REPO ROOT (so the shared packages are available):
#   docker build -f services/workflows/Dockerfile -t prism-workflows:0.1.0 .
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Shared packages first (their runtime deps are a subset of this service's).
COPY packages/evam-backend-core /tmp/evam-backend-core
COPY packages/evam-register-client /tmp/evam-register-client
RUN pip install --upgrade pip \
 && pip install --no-deps /tmp/evam-backend-core /tmp/evam-register-client

COPY services/workflows/pyproject.toml ./
COPY services/workflows/app ./app
RUN pip install .

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN groupadd -r worker && useradd -r -g worker worker && mkdir -p /app && chown worker:worker /app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=worker:worker services/workflows/app ./app
USER worker
# The worker connects out to Temporal; no port to expose.
CMD ["python", "-m", "app.worker"]
