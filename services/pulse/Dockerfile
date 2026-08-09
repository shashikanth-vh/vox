# Build context is the REPO ROOT:
#   docker build -f services/pulse/Dockerfile -t prism-pulse:0.1.0 .
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY packages/evam-backend-core /tmp/evam-backend-core
COPY packages/evam-register-client /tmp/evam-register-client
# setuptools + wheel go in EARLY, in a cached layer: the final install below then builds
# this package with the venv's own backend instead of downloading one.
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-deps /tmp/evam-backend-core /tmp/evam-register-client
COPY services/pulse/pyproject.toml services/pulse/README.md ./
RUN pip install .
COPY services/pulse/app ./app
# The LAST step of the build needs NO network at all:
#   --no-deps            the line above already installed every dependency from this same
#                        pyproject, and copying the app code cannot change them;
#   --no-build-isolation build with the setuptools already in the venv. Without this pip
#                        fetches its build backend (setuptools>=68) into a throwaway env
#                        EVERY time — so this step failed with "Network is unreachable"
#                        on a machine whose internet was down, after the eight cached
#                        layers before it had cost nothing.
RUN pip install --no-deps --no-build-isolation .

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
