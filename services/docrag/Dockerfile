# Build context is the REPO ROOT:
#   docker build -f services/docrag/Dockerfile -t prism-docrag:0.2.0 .
#
# The two FastEmbed models (dense + sparse, ~65 MB, ONNX — no torch) are baked at BUILD
# time at their pinned revisions; the runtime serves them offline (HF_HUB_OFFLINE=1), so
# a deploy never depends on huggingface.co. The runtime also carries a headless JRE:
# OpenDataLoader, the structural PDF extractor, is Java. Vectors live in Qdrant.
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel
COPY packages/evam-backend-core /tmp/evam-backend-core
COPY services/docrag/pyproject.toml services/docrag/README.md ./
# Core WITH its dependencies, resolved together with the service's: core's error module
# imports SQLAlchemy, which a --no-deps install of core would silently leave out.
RUN pip install /tmp/evam-backend-core ".[odl]"
COPY services/docrag/app ./app
# Last step needs no network (deps installed above; build with the venv's setuptools).
RUN pip install --no-deps --no-build-isolation .

# Bake each model at its exact pinned commit into /opt/models/{dense,sparse}.
ENV DOCRAG_MODEL_DIR=/opt/models
RUN python -m app.rag.bake_models

FROM python:3.12-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 HOME=/tmp \
    DOCRAG_MODEL_DIR=/opt/models DOCRAG_MODEL_OFFLINE=true \
    DOCRAG_DATA_DIR=/data/docrag \
    JAVA_TOOL_OPTIONS="-Xmx1g"
# JAVA_TOOL_OPTIONS caps each OpenDataLoader JVM (inherited by the subprocess). Without it
# the JVM sizes its heap from the HOST's RAM (1/4 of it, per run, per ingest thread) on a
# VM shared with the whole stack. Raise it (env override) for very large PDFs.
RUN groupadd -r -g 1000 docrag && useradd -r -u 1000 -g docrag docrag \
    && mkdir -p /app /data/docrag && chown docrag:docrag /app /data/docrag
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=docrag:docrag /opt/models /opt/models
WORKDIR /app
COPY --chown=docrag:docrag services/docrag/app ./app
USER docrag
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
# ONE worker: the document registry and the ingestion queue live in this process (the
# vectors are in Qdrant). Ingestion parallelism comes from DOCRAG_INGEST_WORKERS threads.
CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", "--threads", "4", "--timeout", "120", \
     "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-"]
