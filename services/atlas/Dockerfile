# Build context is the REPO ROOT:
#   docker build -f services/atlas/Dockerfile -t prism-atlas:0.1.0 .
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY packages/evam-backend-core /tmp/evam-backend-core
COPY packages/evam-register-client /tmp/evam-register-client
RUN pip install --upgrade pip && pip install --no-deps /tmp/evam-backend-core /tmp/evam-register-client
COPY services/atlas/pyproject.toml services/atlas/README.md ./
RUN pip install .
COPY services/atlas/app ./app
RUN pip install .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN groupadd -r atlas && useradd -r -g atlas atlas && mkdir -p /app && chown atlas:atlas /app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=atlas:atlas services/atlas/app ./app
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-"]
