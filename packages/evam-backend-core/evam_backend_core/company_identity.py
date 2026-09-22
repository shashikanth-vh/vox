"""One answer to "is this the same company?" — shared by every plane.

The register (linking a new lead to the client master), the workflow plane
(conversion pre-flight, VOX touchpoints) and any future caller must agree on
the comparison key and on the code a brand-new client is born with. Two
implementations of this rule is how GREENPILLREN-2 happened: one plane's
"match" was another plane's "new company". The rule lives here once.
"""

from __future__ import annotations

import hashlib
import re

_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|llp|india|co|company)\b\.?", re.IGNORECASE)


def canonical_name(name: str) -> str:
    """'EcoSoch Solar Pvt. Ltd' → 'ecosoch solar' — the comparison key for matching."""
    return re.sub(r"\s+", " ", _SUFFIXES.sub(" ", name)).strip().lower()


def entity_code(name: str) -> str:
    """A deterministic code for a NEW entity: name slug + a short stable hash, so a
    retried create derives the same code (and the idempotency key dedupes anyway)."""
    slug = re.sub(r"[^A-Z0-9]", "", canonical_name(name).upper())[:12] or "ENTITY"
    return f"{slug}-{hashlib.sha256(name.encode()).hexdigest()[:4].upper()}"
