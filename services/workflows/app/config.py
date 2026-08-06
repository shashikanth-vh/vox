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

    # The CAM workbench engine: "provider:model". Anthropic is the only provider today;
    # with no API key the deterministic stub runs, so dev/CI exercise the full lifecycle
    # without a vendor account. Per-document text is bounded before it reaches the engine.
    cam_engine: str = "anthropic:claude-haiku-4-5"
    anthropic_api_key: str = ""
    # Sized for a REAL CAM: the reference Pinnacle CAM extracts to ~150k chars, and an
    # "update this CAM" turn must carry the whole document, not the first 40% of it.
    cam_max_doc_chars: int = 200_000

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

    # --- Increment 2: VOX + lead lifecycle -------------------------------------------- #
    # Park an ambiguous company capture (close candidates, no exact match) for the RM to
    # confirm instead of silently creating a possible duplicate.
    vox_confirm_ambiguous_company: bool = False
    # Ask when several active leads TIE at the top of the ranking (RM > lens > sector).
    vox_confirm_lead_selection: bool = False
    # How long a parked capture waits for its confirmation.
    vox_confirmation_timeout_hours: float = 72.0
    # Deployment-wide qualification checklist definitions, as JSON:
    #   [{"key": "kyc", "label": "KYC complete", "required": true}, ...]
    # A qualification request supplies per-item results; the orchestrator merges them with
    # these definitions and the workflow COMPUTES the outcome. Empty = legacy passed flag.
    qualification_checklist: str = ""

    # Production switch. When true, the requester of a conversion and the approver/rejecter
    # MUST present a verified OIDC token — the orchestrator refuses to trust a
    # caller-supplied identity string. Leave false only for local dev without an IdP.
    require_auth: bool = False

    # The decision-delivery reconciler (python -m app.reconciler).
    reconciler_interval_seconds: int = 30      # sweep cadence
    reconciler_batch: int = 50                 # deliveries claimed per tenant per sweep
    reconciler_lease_seconds: int = 60         # how long a claimed row is leased
    reconciler_backoff_seconds: int = 60       # backoff for a still-running / errored delivery

    # --- Increment 7: notifications / calendar / document expiry ---------------------- #
    # Master switch: when true, operational events that name recipients ALSO land as
    # durable in-app notifications in the Register (and fan out to the channels below).
    # ON by default: the bell and Today's inbox strip are how a maker learns their
    # request was returned, rejected or applied — with this off, every workflow-lane
    # outcome (committee return, conversion decision, run-control) silently became
    # log-only and "the approver returned it but I got nothing". External channels
    # stay opt-in below; the in-app row is core UX, not an integration.
    notifications_enabled: bool = True
    # External channels to request on every notification, comma-separated subset of
    # "email,sms,webhook". In-app is implicit (the durable row IS the in-app channel).
    # Channels missing their transport config below are skipped with a warning.
    notify_channels: str = ""
    # email channel — SMTP relay. Empty host = channel unavailable.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = "prism@localhost"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_timeout_s: float = 10.0
    # sms channel — a provider-agnostic HTTP hook: POST {to, body, event} as JSON.
    # (Twilio/MSG91/… adapters terminate behind this URL.) Empty = channel unavailable.
    sms_webhook_url: str = ""
    # webhook channel target for notifications (falls back to ops_webhook_url).
    notify_webhook_url: str = ""
    notify_timeout_s: float = 10.0
    # The notifier sweep (python -m app.notifier): claim cadence + retry policy.
    # Backoff is exponential: base * 2^(attempts-1), capped — then dead-letter.
    notifier_interval_seconds: int = 30
    notifier_batch: int = 50
    notifier_lease_seconds: int = 60
    notifier_max_attempts: int = 8
    notifier_backoff_base_seconds: int = 60
    notifier_backoff_cap_seconds: int = 3600
    # Create a first-class calendar event (Register calendar_events) for a VOX capture's
    # next-meeting date, instead of only the meta.calendar hand-off note.
    calendar_events_enabled: bool = False
    # The document-expiry monitor workflow: sweep cadence and the warn-ahead window.
    doc_expiry_interval_hours: float = 24.0
    doc_expiry_warn_days: int = 7
    # WHO is told when a run parks awaiting a governance decision (committee /
    # conversion / syndication / closure) — comma-separated recipient identities
    # (emails). Empty = fall back to notifying the requester only.
    approver_notify: str = ""

    # --- Increment 8: covenants + EWS ------------------------------------------------- #
    # The covenant monitor workflow: sweep cadence and how far ahead observations are
    # generated from each covenant's schedule.
    covenant_interval_hours: float = 24.0
    covenant_horizon_days: int = 30
    # EWS case SLAs: an unassigned case is flagged after assign_sla; an investigation
    # that outlives investigation_sla is AUTO-ESCALATED; an escalated case re-alerts
    # every escalated_reminder until someone closes it. 0 disables the corresponding leg.
    ews_assign_sla_hours: float = 24.0
    ews_investigation_sla_hours: float = 72.0
    ews_escalated_reminder_hours: float = 48.0

    def notify_channel_list(self) -> list[str]:
        return [c.strip() for c in self.notify_channels.split(",") if c.strip()]

    def approver_notify_list(self) -> list[str]:
        return [c.strip() for c in self.approver_notify.split(",") if c.strip()]

    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
