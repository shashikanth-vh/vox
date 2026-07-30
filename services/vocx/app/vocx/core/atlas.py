"""
core.atlas — read-only view over the ATLAS register (the system of record).

ATLAS persists its entire state as one JSON object (the `DATA` / `S` blob in
ATLAS_EVAM_v15.html, mirrored in localStorage). VOX never invents its own copy
of the schema — it loads that same blob and reads the exact fields ATLAS uses:

    S.clients{code -> {name, sector, lens, state, about, notes, toi, ...}}
    S.leads[  {id: 'LD-###', company, sector, lens, source, rm, status,
               temp, contact, phone, last, next, nextDate, conv, notes, ...} ]
    S.deals[  {code, rm, an, lend, syn, am, temp, source, sourceDetail, ...} ]
    S.interactions[ {interactionId, refId, refType, occurredAt, loggedAt,
                     person, interactionType, direction, lenderName, notes,
                     nextAction, nextActionDate} ]

Clients carry no `rm` of their own — the owning RM lives on the matching deal
(same `code`) and, failing that, on a lending row. `AtlasStore` resolves that so
the entity resolver can apply the own-client boost.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

ACTIVE_LEAD_STATUS = "Active"

DEFAULT_INTERACTION_TYPES = [
    "In-Person Meeting", "Virtual Meeting / Video Call", "Phone Call",
    "WhatsApp / Text message", "Email / Written Correspondence",
    "Site Visit / Due Diligence", "Management Presentation",
    "Term Sheet Negotiation", "Internal Review / Credit Committee",
]


@dataclass
class Candidate:
    """A resolvable ATLAS entity (existing client or active lead)."""
    kind: str                 # 'client' | 'lead'
    ref_id: str               # client code  or  lead id (LD-###)
    ref_type: str             # 'Deal' (clients ↔ deals share a code) | 'Lead'
    name: str                 # canonical display name
    rm: str = ""              # owning RM (short name, e.g. 'Chetan')
    sector: str = ""
    lens: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class AtlasStore:
    """Loads an ATLAS register export and exposes the entities VOX resolves against."""

    def __init__(self, data: dict[str, Any]):
        self.data = data or {}
        self.clients: dict[str, Any] = self.data.get("clients", {}) or {}
        self.leads: list[dict[str, Any]] = self.data.get("leads", []) or []
        self.deals: list[dict[str, Any]] = self.data.get("deals", []) or []
        self.lending: list[dict[str, Any]] = self.data.get("lending", []) or []
        self.interactions: list[dict[str, Any]] = self.data.get("interactions", []) or []
        self.interaction_types: list[str] = self.data.get("interactionTypes", []) or []
        self.ref: dict[str, Any] = self.data.get("ref", {}) or {}
        self._rm_by_code = self._index_rm_by_code()

    # ---- loading -----------------------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> AtlasStore:
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    @classmethod
    def default(cls) -> AtlasStore:
        """Load the bundled ATLAS fixture (real v15 export) next to this file."""
        here = os.path.dirname(os.path.abspath(__file__))
        return cls.from_file(os.path.join(here, "fixtures", "atlas_data.json"))

    # ---- rm resolution -----------------------------------------------------
    def _index_rm_by_code(self) -> dict[str, str]:
        rm: dict[str, str] = {}
        # deals are the primary carrier of the owning RM for a client code
        for d in self.deals:
            code, who = d.get("code"), (d.get("rm") or "").strip()
            if code and who and not rm.get(code):
                rm[code] = who
        # lending rows are a fallback
        for l in self.lending:
            code, who = l.get("code"), (l.get("rm") or "").strip()
            if code and who and not rm.get(code):
                rm[code] = who
        return rm

    def rm_for_client(self, code: str) -> str:
        return self._rm_by_code.get(code, "")

    # ---- candidate set -----------------------------------------------------
    def candidates(self) -> list[Candidate]:
        """Everything an utterance could resolve to: all clients + all ACTIVE leads."""
        out: list[Candidate] = []
        for code, c in self.clients.items():
            out.append(Candidate(
                kind="client", ref_id=code, ref_type="Deal",
                name=c.get("name") or code,
                rm=self.rm_for_client(code),
                sector=c.get("sector", ""), lens=c.get("lens", ""),
                raw=c,
            ))
        for l in self.leads:
            if (l.get("status") or "").strip() != ACTIVE_LEAD_STATUS:
                continue
            out.append(Candidate(
                kind="lead", ref_id=l.get("id", ""), ref_type="Lead",
                name=l.get("company") or l.get("id", ""),
                rm=(l.get("rm") or "").strip(),
                sector=l.get("sector", ""), lens=l.get("lens", ""),
                raw=l,
            ))
        return out

    # ---- id minting --------------------------------------------------------
    def next_vox_lead_id(self, prefix: str = "LD-V", pad: int = 2) -> str:
        """Mint the next VOX lead id (LD-V##), mirroring mail-intake's LD-M##.

        Scans every existing lead id sharing the prefix and increments the max,
        so ids never collide with already-created VOX leads.
        """
        n = 0
        for l in self.leads:
            lid = str(l.get("id", ""))
            if lid.startswith(prefix):
                tail = lid[len(prefix):]
                if tail.isdigit():
                    n = max(n, int(tail))
        return f"{prefix}{str(n + 1).zfill(pad)}"
