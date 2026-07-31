# Build context is the REPO ROOT:
#   docker build -f services/stt/Dockerfile -t prism-stt:0.1.0 .
#
# The Whisper model is downloaded at BUILD time (ARG STT_MODEL_SIZE) and the runtime
# stage sets HF_HUB_OFFLINE=1 — serving never talks to huggingface.co. To change the
# model:  docker build --build-arg STT_MODEL_SIZE=medium ...  (and keep the runtime
# STT_MODEL_SIZE env matching, which the ENV below does automatically).
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY packages/evam-backend-core /tmp/evam-backend-core
RUN pip install --upgrade pip && pip install --no-deps /tmp/evam-backend-core
COPY services/stt/pyproject.toml services/stt/README.md ./
RUN pip install ".[whisper]"
COPY services/stt/app ./app
RUN pip install ".[whisper]"

# Bake the model. HF_HOME keeps the hub's side caches inside the same throwaway layer.
ARG STT_MODEL_SIZE=small
ENV HF_HOME=/opt/models/hf
RUN python -c "import os; from faster_whisper.utils import download_model; \
    download_model(os.environ['STT_MODEL_SIZE'], cache_dir='/opt/models')" \
    && rm -rf /opt/models/hf

FROM python:3.12-slim AS runtime
ARG STT_MODEL_SIZE=small
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 STT_MODEL_DIR=/opt/models STT_MODEL_SIZE=${STT_MODEL_SIZE}
RUN groupadd -r stt && useradd -r -g stt stt && mkdir -p /app && chown stt:stt /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=stt:stt /opt/models /opt/models
WORKDIR /app
COPY --chown=stt:stt services/stt/app ./app
USER stt
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
# ONE worker = one model in memory (the whole point of the split); threads keep
# health/readiness responsive while a decode holds the inference lock. Long timeout:
# a 10-minute clip on CPU is legitimate work, not a hang.
CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", "--threads", "4", "--timeout", "300", \
     "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-"]
