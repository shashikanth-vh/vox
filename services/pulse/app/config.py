"""PULSE configuration (env prefix ``PULSE_``). Stateless — no database.

Every setting can be supplied as an environment variable, e.g. ``PULSE_REGISTER_API_KEY``.
For local development a ``.env`` file next to the service works too. The pattern is the
same across every PRISM service, so once you have read one config module you have read
them all.
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import ValidationInfo, field_validator
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

    # ---- News Radar: search ------------------------------------------------
    # The desk's search runs HERE, not in the browser: Google News and GDELT send no
    # CORS headers, so the UI used to route around that through a public proxy — which
    # put the names of companies we are looking at through a third party. These knobs
    # were ATLAS_* keys in the old atlas_config.json; they are environment variables
    # now, so nothing configuration-shaped lives in the image.
    #
    # GDELT rate-limits hard and is the flakiest of the three sources. Turning it off
    # is a legitimate steady state — Google News + Bing still answer.
    disable_gdelt: bool = False
    search_timeout_s: float = 12.0
    search_cache_ttl_s: int = 900          # 15 minutes: news does not move faster
    search_cache_max: int = 500
    upstream_concurrency: int = 8          # bounded pool, so a sweep cannot fan out wildly

    # ---- News Radar: e-mail ------------------------------------------------
    # Blank user/password = email DISABLED (search still works, and every email route
    # says so plainly). Never put a real key in the repo: these come from the
    # environment, and deploy/compose/.env is git-ignored.
    smtp_host: str = ""
    smtp_port: int = 587                   # 587 = STARTTLS, 465 = implicit SSL
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""                    # defaults to smtp_user when blank
    smtp_from_name: str = "PRISM Notification"

    # ---- News Radar: schedules --------------------------------------------
    # Recurring digests. The file lives on a mounted volume so schedules survive a
    # container replacement; a handful of rows per tenant does not earn a schema.
    schedule_file: str = "/data/pulse/schedules.json"
    scheduler_enabled: bool = True
    scheduler_tick_s: int = 60

    # A hand-edited .env picks up stray punctuation — a trailing comma from a copied
    # block, a stray quote. On a NUMBER or a BOOLEAN that is unambiguously a typo: no
    # port is "587," and no flag is "1,". Cleaning it beats a container that crash-loops
    # with a pydantic traceback, which is what "PULSE_SMTP_PORT=587," used to earn.
    #
    # Deliberately NOT applied to the string settings: a password may legitimately end
    # in a comma, and silently trimming it would break authentication in a way nobody
    # could see. Those are passed through exactly as written.
    @field_validator("port", "watchlist_max_entities", "fetch_timeout_s", "disable_gdelt",
                     "search_timeout_s", "search_cache_ttl_s", "search_cache_max",
                     "upstream_concurrency", "smtp_port", "scheduler_enabled",
                     "scheduler_tick_s", "log_json", mode="before")
    @classmethod
    def _tolerate_stray_punctuation(cls, v: object, info: ValidationInfo) -> object:
        if not isinstance(v, str):
            return v
        cleaned = v.strip().strip(",;").strip().strip("'\"").strip()
        if not cleaned:
            # `PULSE_SMTP_PORT=` with nothing after it means "I did not set this",
            # which is the DEFAULT — not None, which would fail validation just as
            # loudly as the typo this validator exists to absorb.
            return cls.model_fields[info.field_name].default
        return cleaned

    # A HOSTNAME CANNOT CONTAIN A COMMA. The rule above deliberately leaves strings
    # alone because a PASSWORD may legitimately end in one — but that reasoning does not
    # extend to a host or a sender address, where a comma is never valid and is always
    # the same copy-paste artefact. Left as written, "smtp.gmail.com," is handed to the
    # resolver verbatim and comes back "[Errno -2] Name or service not known", which
    # reads as a mail outage rather than a typo one character long.
    #
    # smtp_pass is NOT in this list and must never be: silently trimming a real password
    # would break authentication in a way nobody could see.
    @field_validator("smtp_host", "smtp_from", "smtp_user", "register_base_url",
                     mode="before")
    @classmethod
    def _tolerate_stray_punctuation_in_names(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        return v.strip().strip(",;").strip().strip("'\"").strip()

    def smtp_password_looks_mangled(self) -> bool:
        """Does the password carry the same stray punctuation as everything around it?

        The password is the one SMTP setting that must NOT be cleaned automatically —
        a real one may legitimately end in a comma, and trimming it would break
        authentication invisibly. But when a hand-edited config gives every value a
        trailing comma (a pasted block; a YAML `KEY: value,` list), the password has one
        too, and the desk then sees "authentication failed" for a password they can see
        is correct. Say so instead of guessing.
        """
        return self.smtp_pass.rstrip().endswith((",", ";"))

    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    def source_list(self) -> list[dict]:
        return json.loads(self.sources)

    def red_word_list(self) -> list[str]:
        return [w.strip().lower() for w in self.red_words.split(",") if w.strip()]

    def green_word_list(self) -> list[str]:
        return [w.strip().lower() for w in self.green_words.split(",") if w.strip()]

    def smtp(self):  # noqa: ANN201 - the mailer's own dataclass, imported lazily
        from app.news.mailer import SmtpConfig

        return SmtpConfig(host=self.smtp_host, port=self.smtp_port, user=self.smtp_user,
                          password=self.smtp_pass, sender=self.smtp_from or self.smtp_user,
                          from_name=self.smtp_from_name)


@lru_cache
def get_settings() -> Settings:
    return Settings()
