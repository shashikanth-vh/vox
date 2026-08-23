"""The VOX schema contract — Phase 0 of the build specification.

One source of truth, three consumers: the structuring prompt is assembled from the
registry, the model's output is validated against it before ANY database write, and
the review renderer draws its blocks from it. There is no second copy to drift.
"""

from .registry import (
    RegistryError,
    latest_registry_version,
    latest_prompt_version,
    load_prompt,
    load_registry,
)
from .contract import ContractError, compute_data_quality_flags, validate_report

__all__ = [
    "RegistryError",
    "ContractError",
    "load_registry",
    "load_prompt",
    "latest_registry_version",
    "latest_prompt_version",
    "validate_report",
    "compute_data_quality_flags",
]
