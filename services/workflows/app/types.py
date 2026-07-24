"""Workflow inputs/outputs. Plain dataclasses so Temporal's default data converter can
serialise them, and so they're safe to import inside the workflow sandbox."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InteractionInput:
    """A field interaction to record durably against an entity (e.g. from VOX)."""

    entity_id: str
    interaction_type: str
    summary: str | None = None
    notes: str | None = None
    performed_by: str | None = None
    source: str = "Temporal"


@dataclass
class IngestResult:
    interaction_id: str
    dossier_counts: dict
