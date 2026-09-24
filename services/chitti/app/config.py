"""Chitti configuration (environment prefix ``CHITTI_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment configuration for the public API and governed Register boundary."""

    model_config = SettingsConfigDict(
        env_prefix="CHITTI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "prism-chitti"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    log_pipeline: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    port: int = 8000

    # Local development follows the repository's checked-in dev-key posture. A real
    # deployment must override this through secret configuration.
    api_keys: str = "dev-chitti-key"
    public_model_id: str = "prism-chitti"

    # The Ledger pipeline can be enabled only with an explicit caller posture. Keeping
    # this off in API-contract tests preserves dependency-free health checks.
    pipeline_enabled: bool = False
    pipeline_stop_after: str = "answer_generation"
    capture_retrieval_trace: bool = False

    register_base_url: str = "http://register:8000"
    register_ca_file: str = ""
    register_api_key: str = ""
    register_tenant: str = "EVAM"
    register_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    internal_signing_secret: str = ""
    internal_signing_algorithm: str = "HS256"
    internal_token_ttl_seconds: int = Field(default=120, ge=10, le=600)
    require_delegation: bool = True

    debug_user_email: str = ""
    debug_user_id: str = ""
    debug_user_roles: str = ""

    page_size: int = Field(default=200, ge=1, le=500)
    max_pages_per_resource: int = Field(default=10, ge=1, le=100)
    max_records_per_request: int = Field(default=2000, ge=1, le=10000)
    max_resources_per_request: int = Field(default=8, ge=1, le=32)
    request_timeout_seconds: float = Field(default=240.0, gt=0, le=300)

    qualitative_max_records: int = Field(default=500, ge=1, le=5000)
    qualitative_max_chars_per_field: int = Field(default=4000, ge=100, le=20000)
    qualitative_max_total_chars: int = Field(default=100000, ge=1000, le=1000000)

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=180)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_completion_tokens: int = Field(default=32768, ge=1, le=131072)
    conversation_model: str = ""
    interpretation_model: str = ""
    grounding_model: str = ""
    answerability_model: str = ""
    planning_model: str = ""
    qualitative_model: str = ""
    answer_model: str = ""

    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    qdrant_collection_prefix: str = "chitti"
    model_cache_dir: str = "/models"
    model_offline: bool = True
    dense_model: str = ""
    dense_model_revision: str = ""
    dense_dimensions: int = Field(default=384, ge=1, le=4096)
    sparse_model: str = ""
    sparse_model_revision: str = ""
    rerank_model: str = ""
    rerank_model_revision: str = ""
    dense_candidates: int = Field(default=40, ge=1, le=100)
    sparse_candidates: int = Field(default=40, ge=1, le=100)
    fused_candidates: int = Field(default=40, ge=1, le=100)
    rerank_limit: int = Field(default=10, ge=1, le=50)
    grounding_rerank_limit: int = Field(default=24, ge=1, le=100)
    planning_rerank_limit: int = Field(default=12, ge=1, le=100)

    def api_key_list(self) -> list[str]:
        return [key.strip() for key in self.api_keys.split(",") if key.strip()]

    def debug_role_list(self) -> list[str]:
        return [role.strip() for role in self.debug_user_roles.split(",") if role.strip()]

    @model_validator(mode="after")
    def validate_identity_posture(self) -> Settings:
        if not self.pipeline_enabled:
            return self
        if not self.register_api_key:
            raise ValueError("CHITTI_REGISTER_API_KEY is required when the pipeline is enabled.")
        if not self.internal_signing_secret:
            raise ValueError(
                "CHITTI_INTERNAL_SIGNING_SECRET is required for exact-path Register delegation."
            )
        llm_values = {
            "CHITTI_LLM_BASE_URL": self.llm_base_url,
            "CHITTI_LLM_API_KEY": self.llm_api_key,
            "CHITTI_CONVERSATION_MODEL": self.conversation_model,
            "CHITTI_INTERPRETATION_MODEL": self.interpretation_model,
            "CHITTI_GROUNDING_MODEL": self.grounding_model,
            "CHITTI_ANSWERABILITY_MODEL": self.answerability_model,
            "CHITTI_PLANNING_MODEL": self.planning_model,
            "CHITTI_QUALITATIVE_MODEL": self.qualitative_model,
            "CHITTI_ANSWER_MODEL": self.answer_model,
        }
        missing_llm = [name for name, value in llm_values.items() if not value]
        if missing_llm:
            raise ValueError(f"Missing required model configuration: {', '.join(missing_llm)}")
        retrieval_values = {
            "CHITTI_DENSE_MODEL": self.dense_model,
            "CHITTI_DENSE_MODEL_REVISION": self.dense_model_revision,
            "CHITTI_SPARSE_MODEL": self.sparse_model,
            "CHITTI_SPARSE_MODEL_REVISION": self.sparse_model_revision,
            "CHITTI_RERANK_MODEL": self.rerank_model,
            "CHITTI_RERANK_MODEL_REVISION": self.rerank_model_revision,
        }
        missing_retrieval = [name for name, value in retrieval_values.items() if not value]
        if missing_retrieval:
            raise ValueError(
                f"Missing required retrieval configuration: {', '.join(missing_retrieval)}"
            )
        if self.environment != "local" and self.debug_user_email:
            raise ValueError("CHITTI_DEBUG_USER_EMAIL is forbidden outside the local environment.")
        if self.environment != "local" and self.pipeline_stop_after != "answer_generation":
            raise ValueError("CHITTI_PIPELINE_STOP_AFTER is a local-development diagnostic only.")
        if not self.require_delegation:
            if self.environment != "local":
                raise ValueError("Delegation may be disabled only in the local environment.")
            if not self.debug_user_email:
                raise ValueError(
                    "CHITTI_DEBUG_USER_EMAIL is required when local delegation is disabled."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
