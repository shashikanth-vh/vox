"""Shared protocol values for ontology, retrieval, and execution boundaries."""

from typing import Final

AUDIENCE_GROUNDING: Final = "grounding"
AUDIENCE_PLANNING: Final = "planning"
AUDIENCES: Final = frozenset({AUDIENCE_GROUNDING, AUDIENCE_PLANNING})
ROLE_SEMANTIC: Final = "semantic"
ROLE_INVARIANT: Final = "invariant"
ROLES: Final = frozenset({ROLE_SEMANTIC, ROLE_INVARIANT})
PRESENTATION_OPERATORS: Final = frozenset({"exclude_from_combination", "keep_separate"})
