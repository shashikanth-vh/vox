"""The RBAC CATALOG — role identifiers, the access-level enum, and the policy version.

These are the platform's non-editable identifiers: what a role IS, what the ordered
access levels ARE, and which approved ATLAS RBAC version this codebase transcribes.
They are versioned CODE, reviewed in pull requests — never edited at runtime.

Authority model (release 1):
  ATLAS is the approved design-time policy. PostgreSQL (`access_grants`) is the runtime
  authority for human access. Access resolves it once, the Gateway issues a short-lived
  SIGNED authorization context, and downstream services verify and enforce that context.
  Code retains the non-editable security invariants and service-principal boundaries.
"""

from __future__ import annotations

from enum import IntEnum

# The approved ATLAS RBAC policy version this package transcribes. Propagated into every
# signed authorization context (claim: policy_version) and stamped on seeds and drift
# reports, so an authorization decision can always answer "under which policy?".
POLICY_VERSION = "3.3"



class Access(IntEnum):
    """Ordered so that role stacking = max() across held roles."""

    NONE = 0
    READ = 1
    SCOPED = 2   # read-write on rows in the user's own scope (book / vertical / assignment)
    FULL = 3     # read + write, no scope restriction within the module
    APPROVE = 4  # not a data write — an approve/reject decision (operations matrix only)


# The 10 catalogue roles (tier, vertical) — spec "Legend & Roles".
ROLES: dict[str, dict[str, str]] = {
    "Admin":       {"tier": "Leadership", "vertical": "System"},
    "Management":  {"tier": "Leadership", "vertical": "All"},
    "BD Head":     {"tier": "Head",       "vertical": "BD"},
    "Credit Head": {"tier": "Head",       "vertical": "Credit"},
    "Syn Head":    {"tier": "Head",       "vertical": "Syndication"},
    "AM Head":     {"tier": "Head",       "vertical": "Asset Monetisation"},
    "BDRM":        {"tier": "IC",         "vertical": "BD"},
    "Deal Analyst": {"tier": "IC",        "vertical": "Credit"},
    "Syn RM":      {"tier": "IC",         "vertical": "Syndication"},
    "AM RM":       {"tier": "IC",         "vertical": "Asset Monetisation"},
}

_ROLE_ORDER = ["Admin", "Management", "BD Head", "BDRM", "Credit Head", "Deal Analyst",
               "Syn Head", "Syn RM", "AM Head", "AM RM"]

