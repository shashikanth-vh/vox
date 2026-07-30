"""VocX voice pipeline — capture → transcript → extraction → resolution → gate → commit.

Package map (responsibility-based):

    mount.py       the FastAPI adapter exposing /v1/* (PRISM entry point)
    loader.py      packaged config.json + deployment env overrides (never secrets in-repo)
    core/          the pipeline engine (PoC lineage — relaxed lint, see pyproject):
                   pipeline · extract (Claude) · resolve (entity matching) · gate
                   (confidence) · server (framework-agnostic routing) · atlas/store
                   (the corpus model) · search
    speech/        stt (faster-whisper/API/stub) · audio_store (MinIO first, volume
                   fallback — a recording is never discarded)
    registry/      the PRISM Register adapters: store (the live corpus), writer
                   (idempotent svc_vox writes; interactions carry the Recording: ref)
    google/        per-RM Google: oauth (PKCE on the volume) · workspace
                   (Calendar/Drive clients) · notes · drive_writer (dormant Drive path)

Degradation is honest at every seam: no ANTHROPIC_API_KEY → stub extraction; no STT →
typed transcripts; no Google → register-only commits; no S3 → volume archiving.
"""
