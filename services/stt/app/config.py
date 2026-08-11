"""STT configuration (env prefix ``STT_``). Stateless — no database, no Register.

The model is baked into the image at build time (``STT_MODEL_DIR``); at runtime the
container runs with ``HF_HUB_OFFLINE=1`` so serving NEVER depends on huggingface.co.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STT_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    app_name: str = "prism-stt"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    port: int = 8000

    # Front door: comma-separated Bearer keys (the callers' ``Authorization: Bearer <k>``
    # or ``X-API-Key`` must match one, constant-time). Empty = open (dev only).
    api_keys: str = ""

    # --- model ---------------------------------------------------------------
    # Must match what the Dockerfile baked (ARG STT_MODEL_SIZE) — with HF_HUB_OFFLINE=1
    # a size that is not in model_dir fails fast at startup instead of downloading.
    model_size: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    vad_filter: bool = True
    # CPU parallelism for the decoder. 0 hands the choice to CTranslate2, which reads the
    # HOST's core count and knows nothing about the container's CPU quota — so a container
    # limited to 2 cores can spawn a thread per host core, and those threads then fight
    # each other for the 2 it actually has and decode SLOWER than 2 threads would. Set
    # this to the cores the container is really allowed (docker `cpus:` / k8s limit) and
    # a long clip gets meaningfully faster with NO change to the model, the beam width or
    # the transcript — accuracy is untouched, only the scheduling is.
    cpu_threads: int = 0
    model_dir: str = "/opt/models"
    # Load the model at STARTUP (readiness gates on it) instead of on the first request.
    preload: bool = True

    # --- limits --------------------------------------------------------------
    max_audio_bytes: int = 25 * 1024 * 1024   # matches VocX's inbound cap

    # Tests/CI: skip the model entirely and answer with this fixed text.
    stub_text: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
