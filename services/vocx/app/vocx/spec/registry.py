"""Registry and prompt loading — versioned files, cached, sanity-checked on load.

The registry lives in the repo as ``schema_registry/vX.json`` and the canonical
prompt as ``prompts/vX.md`` (Build Specification 9.6 / 11). Every conversation row
stores the versions it was processed under, so a registry bump never mutates
existing rows and quality can be correlated with versions.
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import lru_cache

_SERVICE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_REGISTRY_DIR = os.path.join(_SERVICE_ROOT, "schema_registry")
_PROMPTS_DIR = os.path.join(_SERVICE_ROOT, "prompts")
_VERSION_RE = re.compile(r"^v(\d+)$")
_lock = threading.Lock()


class RegistryError(RuntimeError):
    """The registry file is missing or structurally broken — a deploy problem, not
    a per-conversation one. Raised loudly at load, never swallowed."""


def _versions_in(directory: str, suffix: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    out = []
    for name in os.listdir(directory):
        stem = name[: -len(suffix)] if name.endswith(suffix) else None
        if stem and _VERSION_RE.match(stem):
            out.append(stem)
    return sorted(out, key=lambda v: int(v[1:]))


def latest_registry_version() -> str:
    versions = _versions_in(_REGISTRY_DIR, ".json")
    if not versions:
        raise RegistryError(f"No registry files found in {_REGISTRY_DIR}")
    return versions[-1]


def latest_prompt_version() -> str:
    versions = _versions_in(_PROMPTS_DIR, ".md")
    if not versions:
        raise RegistryError(f"No prompt files found in {_PROMPTS_DIR}")
    return versions[-1]


def _sanity(reg: dict, version: str) -> None:
    """A broken registry must fail the service at startup, not one conversation at
    a time. These checks encode the locked cross-cutting rules of Section 9."""
    problems: list[str] = []
    if reg.get("registry_version") != version:
        problems.append(f"registry_version field {reg.get('registry_version')!r} != file version {version!r}")
    if not isinstance(reg.get("use_cases"), list) or not reg["use_cases"]:
        problems.append("use_cases missing/empty")
    common = reg.get("common")
    if not isinstance(common, list) or not common:
        problems.append("common fields missing")
    else:
        keys = [f.get("key") for f in common]
        if len(keys) != len(set(keys)):
            problems.append("duplicate common field keys")
    blocks = reg.get("blocks") or {}
    for uc in reg.get("use_cases", []):
        if uc not in blocks:
            problems.append(f"use case {uc!r} has no block definition")
    taxonomy = reg.get("taxonomy") or {}
    if len(taxonomy) != 6:
        problems.append(f"taxonomy must hold the six locked sectors, found {len(taxonomy)}")
    canon = reg.get("subsector_canonicals") or {}
    all_subsectors = {s for subs in taxonomy.values() for s in subs}
    missing = all_subsectors - set(canon)
    orphans = set(canon) - all_subsectors
    if missing:
        problems.append(f"subsectors without canonical fields: {sorted(missing)}")
    if orphans:
        problems.append(f"canonical fields for unknown subsectors: {sorted(orphans)}")
    for sub, fields in canon.items():
        for f in fields:
            if f.get("conf") not in ("hi", "md"):
                problems.append(f"{sub}.{f.get('key')}: conf must be hi|md")
    if problems:
        raise RegistryError("Registry sanity failed: " + "; ".join(problems))


@lru_cache(maxsize=8)
def load_registry(version: str | None = None) -> dict:
    with _lock:
        v = version or latest_registry_version()
        path = os.path.join(_REGISTRY_DIR, f"{v}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                reg = json.load(fh)
        except FileNotFoundError as exc:
            raise RegistryError(f"Registry {v} not found at {path}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryError(f"Registry {v} is not valid JSON: {exc}") from exc
        _sanity(reg, v)
        return reg


@lru_cache(maxsize=8)
def load_prompt(version: str | None = None) -> str:
    v = version or latest_prompt_version()
    path = os.path.join(_PROMPTS_DIR, f"{v}.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError as exc:
        raise RegistryError(f"Prompt {v} not found at {path}") from exc
