# Build context is the REPO ROOT:
#   docker build -f services/vocx/Dockerfile -t prism-vocx:0.1.0 .
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY packages/evam-backend-core /tmp/evam-backend-core
COPY packages/evam-register-client /tmp/evam-register-client
RUN pip install --upgrade pip && pip install --no-deps /tmp/evam-backend-core /tmp/evam-register-client
COPY services/vocx/pyproject.toml services/vocx/README.md ./
# Speech-to-text lives in the dedicated STT service (services/stt) — VocX calls it
# over HTTP (VOCX_STT_BACKEND=api), so this image stays slim. To run Whisper
# IN-PROCESS instead (fallback), change both installs to `pip install .[stt]` and set
# VOCX_STT_BACKEND=faster_whisper (the HF_HOME env below keeps its cache writable).
RUN pip install .
COPY services/vocx/app ./app
RUN pip install .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
# HF_HOME on the state volume: the model itself honours download_root, but the HF hub's
# xet download backend keeps its chunk cache + logs under $HOME/.cache/huggingface —
# and the system user's home does not exist. Without this the FIRST model download dies
# with 'Permission denied (os error 13)'.
ENV HF_HOME=/data/vocx/hf
RUN groupadd -r vocx && useradd -r -g vocx vocx && mkdir -p /app /data/vocx \
    && chown vocx:vocx /app /data/vocx
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=vocx:vocx services/vocx/app ./app
USER vocx
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--threads", "4", "--timeout", "120", \
     "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-"]
# --threads: the vocx pipeline endpoints run synchronously in the threadpool (STT,
# Claude, Register HTTP); --timeout 120 covers a first-use model download + a long clip.
