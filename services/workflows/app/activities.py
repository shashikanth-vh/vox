"""Temporal activities — the side-effecting steps. Every Register write/read goes through
the shared ``evam-register-client``, so activities inherit auth, idempotency, optimistic
concurrency, retry and correlation for free.

An activity may run more than once (Temporal retries on failure). That is safe here because
writes carry an **idempotency key** derived from the workflow id, so a replay never
duplicates — see ``app.workflows``.
"""

from __future__ import annotations

from typing import Any

from evam_register_client import AsyncRegisterClient
from temporalio import activity

from app.config import get_settings
from app.types import InteractionInput


def _client() -> AsyncRegisterClient:
    s = get_settings()
    return AsyncRegisterClient(
        s.register_base_url, s.register_api_key,
        tenant=s.register_tenant, actor=s.register_actor,
    )


@activity.defn
async def write_interaction(inp: InteractionInput, idempotency_key: str) -> dict[str, Any]:
    """Record the interaction against its entity. Idempotent on ``idempotency_key``."""
    async with _client() as reg:
        return await reg.log_interaction(
            "Entity", inp.entity_id, inp.interaction_type,
            source=inp.source, summary=inp.summary, notes=inp.notes,
            performed_by=inp.performed_by,
            idempotency_key=idempotency_key,
            request_id=activity.info().workflow_id,   # correlate to the workflow
        )


@activity.defn
async def fetch_dossier(entity_id: str) -> dict[str, Any]:
    """Read the entity's 360° dossier (deals, financials, interactions, open intel)."""
    async with _client() as reg:
        return await reg.dossier(entity_id, request_id=activity.info().workflow_id)
