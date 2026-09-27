"""DocRAG configuration (env prefix ``DOCRAG_``).

No database: the reconstructed documents, their chunks and vectors live on a volume
(``data_dir``), one directory per tenant. The embedding model is baked into the image
at build time (``model_dir``) and the container runs with ``HF_HUB_OFFLINE=1``.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_SARVAM_PLACEHOLDER = "REPLACE_WITH_SARVAM_API_KEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCRAG_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    app_name: str = "prism-docrag"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    port: int = 8000

    # Front door: comma-separated keys, presented as X-API-Key or Bearer. Empty = open (dev).
    api_keys: str = ""
    default_tenant: str = "EVAM"
    # Browser origins allowed to call the API cross-origin (comma-separated). Needed only
    # when DocRAG is deployed on its own and a UI on another origin calls it directly;
    # behind the PRISM gateway the gateway owns CORS. Empty = no CORS headers.
    cors_origins: str = ""

    # --- storage / ingestion -------------------------------------------------
    # Originals, reconstructed knowledge and chunks (the source of truth; the Qdrant index
    # is derived from these and rebuilt from them when missing).
    data_dir: str = "/data/docrag"
    max_upload_bytes: int = 50 * 1024 * 1024
    ingest_workers: int = 2
    # TEMPORARY browser test console at /v1/dev-ui (edge: /docrag/v1/dev-ui).
    dev_ui: bool = False

    # --- vector index (Qdrant) ----------------------------------------------
    # false = extraction only (POST /v1/extract): no embedding models, no Qdrant; the
    # document-index and query endpoints answer 503. For deployments that need DocRAG
    # as the CAM workbench's extractor before search is wanted.
    index_enabled: bool = True
    # "http://qdrant:6333" in deployments; ":memory:" = in-process (tests / quick local runs).
    qdrant_url: str = "http://docrag-qdrant:6333"
    qdrant_api_key: str = ""
    qdrant_collection_prefix: str = "docrag"
    qdrant_timeout_seconds: float = 30.0

    # --- embeddings (FastEmbed, ONNX, CPU) -----------------------------------
    # "fastembed" (real) or "stub" (deterministic hashed vectors — tests/CI).
    embedder: str = "fastembed"
    # Revisions are pinned: the image bakes each model at exactly this commit, and the
    # collection name carries the model identity, so a model change lands in a NEW
    # collection that is rebuilt from the saved chunks — vectors never silently mix.
    dense_model: str = "BAAI/bge-small-en-v1.5"
    dense_model_revision: str = "52398278842ec682c6f32300af41344b1c0b0bb2"
    dense_dimensions: int = 384
    sparse_model: str = "Qdrant/bm25"
    sparse_model_revision: str = "22b8d2af71a76161e18dd432d2cee0eefa66e412"
    model_dir: str = ""                       # image: /opt/models (baked); empty = FastEmbed default
    model_offline: bool = False               # image: true — serve only what was baked
    preload: bool = True

    # --- Sarvam (Doc AI OCR for scanned pages + optional generative answers) -
    sarvam_api_key: str = ""
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_timeout_seconds: float = 120.0
    # /v1/chat/completions accepts only a plain-string `content` (no image parts).
    sarvam_chat_path: str = "/v1/chat/completions"
    sarvam_chat_model: str = "sarvam-105b"
    # Doc AI: async job API — submit, poll status, fetch per-page Markdown.
    sarvam_docai_digitise_path: str = "/doc-ai/v1/job/digitise"
    sarvam_docai_job_path: str = "/doc-ai/v1/job"
    sarvam_docai_max_wait_seconds: float = 600.0

    # --- OpenDataLoader (primary structural PDF extractor; needs Java) --------
    use_odl: bool = True
    odl_bin: str = ""                         # empty = `opendataloader-pdf` on PATH
    odl_java_home: str = ""                   # empty = the `java` already on PATH
    odl_timeout_seconds: float = 300.0

    # --- page-difficulty routing ---------------------------------------------
    min_chars_per_page: int = 40
    max_garbled_ratio: float = 0.35

    # --- chunking ------------------------------------------------------------
    max_chunk_chars: int = 1500
    # The embedding model truncates at 256 tokens (~1000 chars); a larger table chunk
    # would have most of its rows silently excluded from the vector index.
    max_table_chunk_chars: int = 900

    # --- hybrid retrieval ----------------------------------------------------
    top_k_vector: int = 20
    top_k_bm25: int = 20
    top_k_final: int = 5

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    def sarvam_configured(self) -> bool:
        key = self.sarvam_api_key.strip()
        return bool(key) and key != _SARVAM_PLACEHOLDER

    def odl_binary(self) -> str | None:
        if self.odl_bin:
            return self.odl_bin if Path(self.odl_bin).exists() else None
        return shutil.which("opendataloader-pdf")

    def java_env(self) -> dict[str, str]:
        """Environment for the ODL subprocess, with JAVA_HOME on PATH if configured."""
        env = os.environ.copy()
        if self.odl_java_home and Path(self.odl_java_home).exists():
            env["JAVA_HOME"] = self.odl_java_home
            env["PATH"] = f"{Path(self.odl_java_home) / 'bin'}:{env.get('PATH', '')}"
        return env


@lru_cache
def get_settings() -> Settings:
    return Settings()
