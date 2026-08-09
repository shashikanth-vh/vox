# Build context is the REPO ROOT:
#   docker build -f services/pulse/Dockerfile -t prism-pulse:0.1.0 .
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY packages/evam-backend-core /tmp/evam-backend-core
COPY packages/evam-register-client /tmp/evam-register-client
RUN pip install --upgrade pip && pip install --no-deps /tmp/evam-backend-core /tmp/evam-register-client
COPY services/pulse/pyproject.toml services/pulse/README.md ./
RUN pip install .
COPY services/pulse/app ./app
# --no-deps: the line above already installed every dependency from the same
# pyproject, and nothing about copying the app code can change them. Re-resolving them
# here only re-opens a network round trip to PyPI on the LAST step of the build — the
# one that has already paid for everything else.
RUN pip install --no-deps .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN groupadd -r pulse && useradd -r -g pulse pulse && mkdir -p /app && chown pulse:pulse /app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=pulse:pulse services/pulse/app ./app
USER pulse
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-"]
