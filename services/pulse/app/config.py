"""PULSE configuration (env prefix ``PULSE_``). Stateless — no database.

Every setting can be supplied as an environment variable, e.g. ``PULSE_REGISTER_API_KEY``.
For local development a ``.env`` file next to the service works too. The pattern is the
same across every PRISM service, so once you have read one config module you have read
them all.
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PULSE_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    app_name: str = "prism-pulse"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    port: int = 8000

    # PULSE's own front door. Empty = open (dev). In production set one or more keys
    # (comma-separated) and every caller must send X-API-Key.
    api_keys: str = ""

    # The Register (direct in-cluster, or via the gateway if preferred).
    register_base_url: str = "http://register:8000"
    register_api_key: str = "dev-local-key"
    # Default tenant when the caller does not send X-Tenant. PULSE is multi-tenant:
    # every endpoint accepts X-Tenant and scans/writes for that tenant only.
    register_tenant: str = "EVAM"

    # News sources, as a JSON list. Each entry: {"name": ..., "kind": "rss"|"json"|"sample",
    # "url": ...}. The built-in "sample" provider needs no URL and exists so the whole
    # pipeline can be exercised offline (dev, tests, demos).
    #   PULSE_SOURCES='[{"name":"et-energy","kind":"rss","url":"https://.../rss"}]'
    sources: str = '[{"name": "sample", "kind": "sample"}]'

    # Matching / signal classification knobs (comma-separated keyword lists).
    # A matched headline containing a red word becomes a RED signal, a green word GREEN,
    # anything else AMBER — a deliberately simple, explainable rule a human can audit.
    red_words: str = ("default,fraud,insolvency,nclt,bankrupt,downgrade,probe,raid,"
                      "arrest,scam,penalty,blacklist,winding up,liquidation")
    # Substring match, so "commission" covers commissions/commissioned; "award" covers
    # awarded/awards.
    green_words: str = ("commission,award,won,raise,funding,upgrade,record,"
                        "expansion,partnership,milestone,profit")

    # Cap on how many entities the watchlist loads per scan (protects the Register from
    # a runaway scan on very large tenants; raise deliberately if you need more).
    watchlist_max_entities: int = 2000
    # Timeout for fetching one external feed.
    fetch_timeout_s: float = 10.0

    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    def source_list(self) -> list[dict]:
        return json.loads(self.sources)

    def red_word_list(self) -> list[str]:
        return [w.strip().lower() for w in self.red_words.split(",") if w.strip()]

    def green_word_list(self) -> list[str]:
        return [w.strip().lower() for w in self.green_words.split(",") if w.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
