"""VocX configuration (env prefix ``VOCX_``). Stateless — no database."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOCX_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    app_name: str = "prism-vocx"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    port: int = 8000

    # VocX's OWN front door: the credential the gateway injects. When set, every inbound
    # capture must present it (constant-time compared) — so VocX is not an open endpoint any
    # pod can call directly (defence in depth alongside a NetworkPolicy). Comma-separated.
    api_keys: str = ""
    # Production: REFUSE a user-triggered capture that carries no valid delegated identity,
    # instead of silently writing as VocX's own service key. Implied for the signed channel.
    require_delegation: bool = False

    # The Register (direct in-cluster, or via the gateway if preferred).
    register_base_url: str = "http://register:8000"
    register_api_key: str = "dev-local-key"
    register_tenant: str = "EVAM"

    # The Orchestrator (workflow plane). When set, captures that carry a company_name
    # (instead of a resolved subject id) run as durable VoxTouchpointWorkflows —
    # resolve-or-create company + lead, full-fidelity interaction, follow-up. Captures
    # with an explicit subject keep the direct fast path below.
    orchestrator_url: str = ""
    orchestrator_api_key: str = ""

    # Signed internal context. When set, VocX VERIFIES the caller context the gateway minted
    # for it and RE-MINTS one bound to the Register interaction write — so the HUMAN's live
    # grant (not VocX's service key) is the authorization identity at the Register. Must
    # equal the Register's internal_signing_secret. Empty = dev (service key is the actor).
    internal_signing_secret: str = ""
    internal_signing_algorithm: str = "HS256"
    internal_token_ttl_seconds: int = 120

    # --- The voice-capture pipeline (app/vocx) ------------------------------
    # Master switch for the /v1/* endpoints (capture preview, commit, search,
    # per-RM Google auth). The pipeline itself degrades gracefully: no ANTHROPIC_API_KEY
    # ⇒ offline extraction stub; no STT backend ⇒ typed transcripts only; no Google
    # client secret ⇒ register-only writes (calendar ops report skipped).
    vocx_pipeline_enabled: bool = True
    # Where per-RM Google refresh tokens + VOX side-state live (mount a VOLUME here —
    # losing it just means every RM reconnects Google).
    tokens_dir: str = "/data/vocx"
    # Mounted Google OAuth client secret JSON. Empty = Google integration off.
    google_client_secret_file: str = ""
    # The OAuth redirect as the BROWSER reaches it — the edge URL, e.g.
    # https://<host>:8443/vocx/v1/auth/callback (add it in Google Cloud Console).
    oauth_redirect_uri: str = ""
    # Override the packaged STT backend (faster_whisper | api | stub). faster_whisper
    # needs the optional [stt] extra installed in the image.
    stt_backend: str = ""
    # How long the Register-derived resolution corpus is cached between captures.
    register_cache_ttl_s: int = 30
    # --- recorded-audio storage (bytes in object storage, references in the record) ----
    # With an endpoint + bucket set, captures PUT to MinIO/S3 and the interaction carries
    # the s3://bucket/key reference; unset = the volume directory; an S3 failure degrades
    # to the volume rather than discarding a recording.
    s3_endpoint_url: str = ""
    # The endpoint as the BROWSER reaches it — presigned playback URLs are signed
    # against this (e.g. http://<vm-ip>:9000). Empty = same as s3_endpoint_url.
    s3_public_endpoint_url: str = ""
    s3_bucket: str = "prism-vocx-captures"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_auto_create_bucket: bool = True
    # Days to keep recordings. 0 = forever. With S3 this becomes a bucket LIFECYCLE rule
    # (enforced by the store, not an app cron); locally an opportunistic sweep.
    audio_retention_days: int = 0
    # TEMPORARY browser test console at /v1/dev-ui (edge: /vocx/v1/dev-ui) — record,
    # preview, commit, reports against the live pipeline. Dev only: OFF by default,
    # and the prod-posture overlay pins it off. Delete app/vocx/dev_ui.html + the
    # dev-ui block in mount.py to remove it permanently.
    dev_ui: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
