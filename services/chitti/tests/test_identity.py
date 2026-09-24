from __future__ import annotations

import pytest
from evam_backend_core.errors import ForbiddenError
from evam_backend_core.internal_token import mint_internal_context, verify_internal_context
from starlette.requests import Request

from app.config import Settings
from app.identity import mint_register_context, resolve_caller

SECRET = "test-signing-secret-is-long-enough"


def _settings(**overrides) -> Settings:
    values = {
        "pipeline_enabled": True,
        "register_api_key": "svc-key",
        "internal_signing_secret": SECRET,
        "require_delegation": True,
        "llm_base_url": "https://llm.test/v1",
        "llm_api_key": "llm-key",
        "conversation_model": "conversation-model",
        "interpretation_model": "interpretation-model",
        "grounding_model": "grounding-model",
        "answerability_model": "answerability-model",
        "planning_model": "planning-model",
        "qualitative_model": "qualitative-model",
        "answer_model": "answer-model",
        "dense_model": "dense",
        "dense_model_revision": "dense-revision",
        "sparse_model": "sparse",
        "sparse_model_revision": "sparse-revision",
        "rerank_model": "rerank",
        "rerank_model_revision": "rerank-revision",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _request(*, path: str = "/v1/chat/completions", token: str | None = None) -> Request:
    headers = []
    if token:
        headers.append((b"x-internal-context", token.encode()))
    return Request({"type": "http", "method": "POST", "path": path, "headers": headers})


def _token(*, path: str = "/v1/chat/completions") -> str:
    return mint_internal_context(
        signing_key=SECRET,
        tenant="EVAM",
        email="person@evamfinance.com",
        user_id="caller-1",
        roles=["BDRM"],
        report_ids=["report-1"],
        report_emails=["report@evamfinance.com"],
        effective_views={"leads": "SCOPED"},
        effective_operations={},
        matrix_version=7,
        method="POST",
        path=path,
    )


def test_verified_context_requires_exact_chitti_route_and_preserves_grants():
    identity = resolve_caller(_request(token=_token()), _settings())

    assert identity.email == "person@evamfinance.com"
    assert identity.tenant == "EVAM"
    assert identity.effective_views == {"leads": "SCOPED"}
    assert identity.matrix_version == 7

    with pytest.raises(ForbiddenError, match="not bound"):
        resolve_caller(_request(token=_token(path="/wrong")), _settings())


def test_local_debug_posture_is_explicit_and_forbidden_outside_local():
    settings = _settings(
        require_delegation=False,
        debug_user_email="admin@evamfinance.com",
        debug_user_roles="Admin,Management",
    )
    identity = resolve_caller(_request(), settings)
    assert identity.posture == "local_debug"
    assert identity.roles == ("Admin", "Management")
    assert identity.display_scope == "DEBUG_FULL"

    with pytest.raises(ValueError, match="forbidden outside"):
        _settings(
            environment="production",
            require_delegation=False,
            debug_user_email="admin@evamfinance.com",
        )


def test_register_context_is_get_only_and_exact_path_bound():
    identity = resolve_caller(
        _request(),
        _settings(
            require_delegation=False,
            debug_user_email="admin@evamfinance.com",
            debug_user_roles="Admin,Management",
        ),
    )
    token = mint_register_context(identity, _settings(), method="GET", path="/v1/leads")
    verified = verify_internal_context(token, verify_key=SECRET)
    assert (verified.method, verified.path, verified.tenant) == ("GET", "/v1/leads", "EVAM")

    with pytest.raises(ValueError, match="only for GET"):
        mint_register_context(identity, _settings(), method="POST", path="/v1/leads")
