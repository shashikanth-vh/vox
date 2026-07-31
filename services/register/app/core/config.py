"""Register service configuration.

Inherits every cross-cutting knob (identity, HTTP, DB pool, timeouts, retry, pagination)
from ``evam_backend_core.config.BaseServiceSettings`` and adds only the Register-specific
settings (API keys, tenancy, idempotency, RLS). Values come from ``REGISTER_``-prefixed
environment variables (12-factor); nothing secret is hard-coded.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from evam_backend_core.config import BaseServiceSettings
from pydantic import Field, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="REGISTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Register-specific identity/DB defaults.
    app_name: str = "prism-register"
    db_name: str = "register"
    db_user: str = "register"
    db_password: str = "register"

    # ---- Security --------------------------------------------------------
    # Comma-separated API keys accepted by the service (X-API-Key header).
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["dev-local-key"])
    require_api_key: bool = True
    # A SEPARATE, higher-privilege credential for tenant administration (create/list/
    # (de)activate tenants). Presented via X-Admin-Key. When set, tenant-admin routes
    # require it (not the shared data-plane key). Empty = compatibility mode: the shared
    # key still works but any forwarded human identity must hold the Admin role.
    admin_api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Named SERVICE principals: "svc_pulse:key1,svc_vox:key2,svc_workflows:key3". A machine
    # caller presenting one of these keys is bound to that service's operation allowlist
    # (least privilege). Keys here are ALSO accepted as valid API keys. Format is
    # name:secret pairs; parsed into {secret: name}.
    service_api_keys: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    default_tenant_code: str = "EVAM"
    # Enforce PostgreSQL row-level security using the request tenant. Off by default for
    # the single-tenant local build; turn on in multi-tenant deployments.
    enforce_rls: bool = False

    # ---- Behaviour -------------------------------------------------------
    idempotency_ttl_hours: int = 48

    # ---- Advaya integration (DEFAULT OFF) --------------------------------
    # PRISM's terminal for the current product scope is 'Disbursed'; there is no Advaya
    # integration. When (and only when) a real acknowledgement channel exists, set this on to
    # re-enable the dormant path: the internal advaya-handoffs router is registered, the workflow
    # service is granted attach_advaya_evidence, and the onward disbursement states/gate become
    # reachable. Enabling it WITHOUT a configured endpoint is a misconfiguration and is refused at
    # startup (see ``validate_startup``), so the path can never be half-enabled.
    advaya_integration_enabled: bool = False
    advaya_integration_url: str = ""

    def validate_startup(self) -> None:
        """Fail closed on an incomplete future-integration configuration. Called at app startup."""
        if self.advaya_integration_enabled and not self.advaya_integration_url.strip():
            raise ValueError(
                "REGISTER_ADVAYA_INTEGRATION_ENABLED is on but REGISTER_ADVAYA_INTEGRATION_URL is "
                "empty — the Advaya acknowledgement path may not be enabled without a configured "
                "endpoint.")

    # ---- User management / RBAC (ATLAS RBAC spec) -------------------------
    # Users' e-mail addresses must belong to this domain (spec: SSO integrity).
    user_email_domain: str = "evamfinance.com"
    # When on, operations gated by the RBAC matrix REQUIRE a user context
    # (X-User-Email). Off (default) keeps machine-to-machine API-key flows working;
    # requests that DO carry a user are always checked either way.
    enforce_rbac: bool = False
    # Shared secret proving identity headers came from the gateway. Empty (dev/local) =
    # trust headers as sent; set in any real deployment so identity can't be spoofed by
    # a caller that reaches the Register directly. (Legacy path; prefer the signed
    # internal context below.)
    gateway_shared_secret: str = ""

    # Signed internal context (the production identity channel). When set, a request
    # carrying X-Internal-Context is authenticated by VERIFYING the signature, and the
    # caller's identity + LIVE effective permissions come from the token — not from
    # plaintext headers, and not re-derived from the compiled static matrix. HS256 uses
    # this value as the shared secret; RS256 uses it as the PEM PUBLIC key.
    internal_signing_secret: str = ""
    internal_signing_algorithm: str = "HS256"

    # ---- Access service (assignee verification) -------------------------
    # When set, the Register VERIFIES an assignee's identity + role against the Access
    # service before placing an assignment (so a service can't assign an arbitrary UUID,
    # nor a role the assignee doesn't hold). Empty (dev) falls back to the local Person
    # roster. The api key is presented to Access; the tenant code scopes the lookup.
    access_url: str = ""
    access_api_key: str = "dev-local-key"
    access_verify_ttl_s: float = 30.0
    # ONLINE REVALIDATION for SENSITIVE operations (delete/restore, assignments, imports,
    # evidence break-glass): before acting, re-resolve the caller against Access LIVE and
    # require the operation still granted AND the revocation epoch unchanged. Fails CLOSED
    # (503) when Access is unreachable. Off in dev; ON in the production posture.
    online_revalidation: bool = False

    # ---- Documents (Data Register) --------------------------------------
    # Max size (bytes) of a document kept INLINE in Postgres via ``content_base64``.
    # Larger files must be uploaded to object storage and registered by ``storage_uri``.
    # Mirrors the ATLAS "files up to 400 KB stay viewable" affordance.
    documents_inline_max_bytes: int = 400 * 1024

    # ---- Object storage (S3 / MinIO) ------------------------------------
    # Where document BYTES live. "inline" keeps small files in Postgres (dev default);
    # "s3" uploads them to an S3-compatible store (AWS S3 or MinIO) and stores only the
    # reference. The catalog row is identical either way.
    storage_backend: str = "inline"  # inline | s3
    s3_bucket: str = "prism-documents"
    # MinIO / non-AWS endpoint, e.g. http://minio:9000. None → real AWS S3.
    s3_endpoint_url: str | None = None
    # Endpoint browsers should use for presigned URLs (when the in-cluster endpoint above
    # isn't reachable from outside). Falls back to s3_endpoint_url.
    s3_public_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_use_ssl: bool = True
    s3_path_style: bool = True  # MinIO needs path-style addressing; AWS accepts it too
    s3_presign_expiry_seconds: int = 3600
    s3_auto_create_bucket: bool = True
    # Stream bytes back through the API instead of redirecting to a presigned URL. Use when
    # the object store isn't reachable by the client directly.
    s3_stream_through_api: bool = False

    @field_validator("storage_backend")
    @classmethod
    def _check_storage_backend(cls, v: str) -> str:
        allowed = {"inline", "s3"}
        if v not in allowed:
            raise ValueError(f"storage_backend must be one of {allowed}, got '{v}'.")
        return v

    @field_validator("api_keys", "admin_api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("service_api_keys", mode="before")
    @classmethod
    def _parse_service_keys(cls, v: object) -> object:
        if isinstance(v, str):
            out: dict[str, str] = {}
            for pair in v.split(","):
                pair = pair.strip()
                if not pair or ":" not in pair:
                    continue
                name, secret = pair.split(":", 1)
                if name.strip() and secret.strip():
                    out[secret.strip()] = name.strip()
            return out
        return v

    def all_api_keys(self) -> list[str]:
        """Every accepted data-plane key: the generic keys plus every service key."""
        return [*self.api_keys, *self.service_api_keys.keys()]

    def service_for_key(self, provided: str | None) -> str | None:
        if not provided:
            return None
        return self.service_api_keys.get(provided)


@lru_cache
def get_settings() -> Settings:
    return Settings()
