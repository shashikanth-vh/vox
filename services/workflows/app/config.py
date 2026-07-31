"""Settings for the workflows service (env prefix: WORKFLOWS_)."""

from __future__ import annotations

from functools import lru_cache

from evam_backend_core.config import BaseServiceSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="WORKFLOWS_", extra="ignore")
    app_name: str = "prism-workflows"

    # Temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "prism-workflows"

    # Where activities reach the Register (through NGINX in a real deployment).
    register_base_url: str = "http://localhost:8000"
    register_api_key: str = "dev-local-key"
    register_tenant: str = "EVAM"     # DEFAULT tenant only — a caller's tenant overrides it
    register_actor: str = "workflows"

    # Signed internal context. When set, the worker RE-MINTS a short-lived signed context
    # from the caller identity carried in the workflow input, so the Register authorizes
    # writes as the HUMAN (with their scope) — not the worker's service key. Must equal the
    # Register's internal_signing_secret. Empty = dev (writes run as the service key).
    internal_signing_secret: str = ""
    internal_signing_algorithm: str = "HS256"
    internal_token_ttl_seconds: int = 120

    # The Orchestrator API (python -m app.api) — the HTTP front door that starts
    # workflows / delivers signals. Empty api_keys = open (dev); set in production.
    api_host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    api_port: int = 8000
    api_keys: str = ""

    # Verified identity for approvals. With an OIDC issuer set, approve/reject derive
    # the decider from the bearer TOKEN (not a caller-supplied 'by' field), and the
    # Access service confirms they hold an approver role for the subject's vertical.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_email_claim: str = "email"
    # Accept SEVERAL issuers in one deployment: "issuer|audience,issuer2|audience2".
    # Takes precedence over oidc_issuer/oidc_audience above; empty = single-issuer behaviour.
    # e.g. Google for people AND Dex for CI against the same staging environment.
    oidc_issuers: str = ""
    # Comma-separated e-mail domains permitted to authenticate. Empty = no restriction (dev).
    # REQUIRED in production once a consumer IdP (Google) is an accepted issuer: a valid token
    # proves the account is real, not that it belongs to your organisation.
    oidc_allowed_domains: str = ""
    access_url: str = ""             # e.g. http://prism-access — for role checks
    access_api_key: str = "dev-local-key"

    # ------------------------------------------------------------------ #
    # Release-1 workflow-foundation feature flags. Every one defaults to  #
    # its safe/off posture so existing deployments change nothing until   #
    # an operator opts in.                                                #
    # ------------------------------------------------------------------ #
    # Operational events (SLA reminders, escalations, control actions) are always written to
    # the structured log; when this URL is set they are ALSO posted as JSON to it (Slack /
    # Teams / any webhook receiver). Delivery is best-effort with bounded retry — an
    # unreachable webhook never fails a workflow.
    ops_webhook_url: str = ""
    ops_webhook_timeout_s: float = 5.0
    ops_webhook_retries: int = 2
    # Sensitive-payload encryption at rest in Temporal: base64url 32-byte key → AES-256-GCM
    # PayloadCodec on every workflow input/result/activity argument. Empty = plaintext (dev).
    payload_encryption_key: str = ""
    # Prometheus scrape endpoint for the WORKER's Temporal SDK metrics (task latencies,
    # failures, cache). Empty = metrics off. e.g. "0.0.0.0:9464".
    metrics_bind_address: str = ""
    # Upsert per-run search attributes (PrismBusinessStatus / PrismSubject) so ops can filter
    # runs in the Temporal UI/CLI. Requires the attributes to be REGISTERED on the server
    # first (see services/workflows/README.md) — hence opt-in.
    search_attributes_enabled: bool = False
    # Worker build identity. Setting a build id stamps runs; enabling versioning additionally
    # routes tasks only to compatible workers (requires server-side rules — see README).
    worker_build_id: str = ""
    use_worker_versioning: bool = False

    # Production switch. When true, the requester of a conversion and the approver/rejecter
    # MUST present a verified OIDC token — the orchestrator refuses to trust a
    # caller-supplied identity string. Leave false only for local dev without an IdP.
    require_auth: bool = False

    # The decision-delivery reconciler (python -m app.reconciler).
    reconciler_interval_seconds: int = 30      # sweep cadence
    reconciler_batch: int = 50                 # deliveries claimed per tenant per sweep
    reconciler_lease_seconds: int = 60         # how long a claimed row is leased
    reconciler_backoff_seconds: int = 60       # backoff for a still-running / errored delivery

    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
