"""loader.py — VOX runtime config: packaged defaults + deployment env overrides.

The packaged ``config.json`` carries TUNING (thresholds, templates, defaults) and is
safe to commit. Everything deployment- or secret-shaped comes from VocX settings /
environment variables and is merged here, so no secret can ever end up in the repo:

    VOCX_GOOGLE_CLIENT_SECRET_FILE  → google.client_secret_file  (mounted secret; empty = Google off)
    VOCX_OAUTH_REDIRECT_URI           → google.redirect_uri        (the EDGE url, e.g.
                                      https://<host>:8443/vocx/v1/auth/callback)
    VOCX_TOKENS_DIR             → google.tokens_dir          (persistent volume)
    VOCX_STT_BACKEND            → stt.backend                (faster_whisper | api | stub)
    ANTHROPIC_API_KEY               → read directly by the extractor (name comes from
                                      config.anthropic_api_key_env)
"""

from __future__ import annotations

import json
import os
from typing import Any


def build_vox_config(settings: Any) -> dict[str, Any]:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "config.json"), encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)

    g = config.setdefault("google", {})
    if getattr(settings, "google_client_secret_file", ""):
        g["client_secret_file"] = settings.google_client_secret_file
    if getattr(settings, "oauth_redirect_uri", ""):
        g["redirect_uri"] = settings.oauth_redirect_uri
    if getattr(settings, "tokens_dir", ""):
        g["tokens_dir"] = settings.tokens_dir

    if getattr(settings, "stt_backend", ""):
        config.setdefault("stt", {})["backend"] = settings.stt_backend
    # Archive captured audio onto the tokens volume (the one writable mount).
    if getattr(settings, "tokens_dir", ""):
        stt = config.setdefault("stt", {})
        stt.setdefault("archive_dir", os.path.join(settings.tokens_dir, "captures"))
        # Whisper models download ONCE onto the volume; restarts and new replicas reuse it.
        stt.setdefault("model_cache_dir", os.path.join(settings.tokens_dir, "models"))
    return config
