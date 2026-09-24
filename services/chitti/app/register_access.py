"""Read-only, bounded Register access under a delegated caller context."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evam_backend_core.logging import get_logger
from evam_register_client import AsyncRegisterClient
from evam_register_client.config import RegisterClientConfig

from app.config import Settings
from app.evidence import (
    CanonicalRecord,
    Completeness,
    RegisterEvidence,
    RegisterRead,
    RetrievalWindow,
    utc_now,
)
from app.identity import CallerIdentity, mint_register_context


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    path: str
    api_name: str
    equality_filters: frozenset[str]
    search_fields: frozenset[str]


RESOURCE_SPECS: Final[dict[str, ResourceSpec]] = {
    "entities": ResourceSpec(
        "/v1/entities",
        "entities",
        frozenset(
            {
                "sector",
                "lens",
                "register_status",
                "entity_type",
                "state",
                "promoter_group_code",
                "code",
            }
        ),
        frozenset({"legal_name", "code", "display_name", "cin"}),
    ),
    "people": ResourceSpec(
        "/v1/people",
        "people",
        frozenset({"role", "inactive"}),
        frozenset({"name", "full_name", "email"}),
    ),
    "counterparties": ResourceSpec(
        "/v1/counterparties",
        "counterparties",
        frozenset({"counterparty_type", "is_active"}),
        frozenset({"name", "short_name"}),
    ),
    "leads": ResourceSpec(
        "/v1/leads",
        "leads",
        frozenset(
            {
                "status",
                "temperature",
                "sector",
                "rm",
                "source",
                "entity_id",
                "converted_deal_id",
                "lens",
                "company",
                "lead_no",
                "last_interaction_date",
            }
        ),
        frozenset({"company", "contact", "rm", "notes"}),
    ),
    "deals": ResourceSpec(
        "/v1/deals",
        "deals",
        frozenset(
            {
                "product_type",
                "stage",
                "temperature",
                "is_lending",
                "is_syndication",
                "is_asset_mon",
                "entity_id",
                "rm",
                "code",
                "analyst",
                "lens",
            }
        ),
        frozenset({"deal_no", "code", "rm", "analyst", "remarks"}),
    ),
    "lending": ResourceSpec(
        "/v1/lending",
        "lending",
        frozenset({"stage", "pending_with", "entity_id", "deal_id", "rm", "analyst", "stage_updated_at"}),
        frozenset({"tracker_no", "rm", "analyst", "remarks"}),
    ),
    "syndication": ResourceSpec(
        "/v1/syndication",
        "syndication",
        frozenset(
            {
                "status",
                "priority",
                "entity_id",
                "deal_id",
                "rm",
                "pending_with",
            }
        ),
        frozenset({"tracker_no", "remarks", "toi"}),
    ),
    "syndication_lenders": ResourceSpec(
        "/v1/syndication-lenders",
        "syndication-lenders",
        frozenset({"status", "syndication_id", "counterparty_id", "is_existing"}),
        frozenset({"lender_name", "note"}),
    ),
    "asset_monetisation": ResourceSpec(
        "/v1/asset-monetisation",
        "asset-monetisation",
        frozenset({
            "status", "nature", "entity_id", "deal_id", "state", "investor_type",
            "deal_type", "teaser_date", "rm", "analyst", "investor",
        }),
        frozenset({"investor", "notes"}),
    ),
}
RESOURCE_PATHS: Final[dict[str, str]] = {resource: spec.path for resource, spec in RESOURCE_SPECS.items()}
RESOURCE_LANGUAGE: Final[dict[str, tuple[str, ...]]] = {
    "entities": ("entity", "entities", "company master", "client master"),
    "people": ("person", "people", "employee directory"),
    "counterparties": ("counterparty", "counterparties", "lender directory"),
    "leads": ("lead", "leads", "pre-deal opportunity"),
    "deals": ("commercial deal", "commercial deals", "deal funnel"),
    "lending": ("lending", "lending tracker", "own-book facility", "own-book facilities"),
    "syndication": (
        "syndication",
        "syndication tracker",
        "partner-book financing",
        "off-book financing",
    ),
    "syndication_lenders": (
        "syndication lender",
        "syndication lenders",
        "lender submission",
    ),
    "asset_monetisation": (
        "asset monetisation",
        "asset monetization",
        "asset monetisation deal",
        "asset monetisation deals",
        "asset monetization deal",
        "asset monetization deals",
    ),
}
RESOURCE_FIELDS: Final[dict[str, frozenset[str]]] = {
    "leads": frozenset(
        {
            "id",
            "lead_no",
            "company",
            "entity_id",
            "sector",
            "lens",
            "source",
            "source_name",
            "rm",
            "status",
            "temperature",
            "contact",
            "last_interaction_date",
            "next_action",
            "next_action_date",
            "converted_deal_id",
            "conv",
            "notes",
            "created_at",
            "updated_at",
        }
    ),
    "lending": frozenset(
        {
            "id",
            "tracker_no",
            "entity_id",
            "deal_id",
            "amount_cr",
            "rm",
            "stage",
            "analyst",
            "stage_updated_at",
            "sanction_date",
            "proposed_disbursement_amount",
            "proposed_disbursement_date",
            "disbursed_amount",
            "disbursement_date",
            "pending_with",
            "remarks",
            "stage_history",
            "reconciliation_status",
            "created_at",
            "updated_at",
        }
    ),
    "entities": frozenset(
        {
            "id",
            "code",
            "legal_name",
            "display_name",
            "entity_type",
            "cin",
            "pan",
            "gstin",
            "sector",
            "sub_sector",
            "lens",
            "state",
            "location",
            "register_status",
            "lifecycle",
            "promoter_group_code",
            "about",
            "toi",
            "notes",
            "tags",
            "created_at",
            "updated_at",
        }
    ),
    "people": frozenset(
        {
            "id",
            "name",
            "full_name",
            "email",
            "phone",
            "role",
            "geography",
            "sectors",
            "started_on",
            "reports_to",
            "inactive",
            "notes",
            "created_at",
            "updated_at",
        }
    ),
    "counterparties": frozenset(
        {
            "id",
            "name",
            "short_name",
            "counterparty_type",
            "is_active",
            "sectors",
            "ticket_min_cr",
            "ticket_max_cr",
            "notes",
            "created_at",
            "updated_at",
        }
    ),
    "deals": frozenset(
        {
            "id",
            "deal_no",
            "entity_id",
            "code",
            "product_type",
            "is_lending",
            "is_syndication",
            "is_asset_mon",
            "rm",
            "analyst",
            "lens",
            "stage",
            "stage_history",
            "reconciliation_status",
            "temperature",
            "source",
            "source_detail",
            "source_name",
            "date_received",
            "ic_date",
            "sanction_date",
            "disbursement_date",
            "exit_date",
            "remarks",
            "created_at",
            "updated_at",
        }
    ),
    "syndication": frozenset(
        {
            "id",
            "tracker_no",
            "entity_id",
            "deal_id",
            "toi",
            "rm",
            "analyst",
            "lc",
            "priority",
            "status",
            "amount_cr",
            "line",
            "facility",
            "tenor",
            "mandate_status",
            "potential",
            "im_status",
            "sanctioned_lender",
            "ip_lender",
            "date_of_sanction",
            "month_of_sanction",
            "nature",
            "existing",
            "price",
            "syndication_type",
            "mandate_status3",
            "pending_with",
            "remarks",
            "status_history",
            "lenders",
            "reconciliation_status",
            "created_at",
            "updated_at",
        }
    ),
    "syndication_lenders": frozenset(
        {
            "id",
            "syndication_id",
            "counterparty_id",
            "lender_name",
            "is_existing",
            "status",
            "amount_cr",
            "since",
            "response_date",
            "chased_date",
            "note",
            "last_chase_note",
            "last_reply_note",
            "status_history",
            "created_at",
            "updated_at",
        }
    ),
    "asset_monetisation": frozenset(
        {
            "id",
            "tracker_no",
            "entity_id",
            "deal_id",
            "state",
            "indicative_value_cr",
            "size_mw",
            "nature",
            "deal_type",
            "investor",
            "investor_type",
            "rm",
            "analyst",
            "status",
            "status_history",
            "reconciliation_status",
            "teaser_date",
            "notes",
            "created_at",
            "updated_at",
        }
    ),
}

# Free-form storage does not make these fields safe for lexical interpretation. Their
# values encode governed multi-part business states and may include explicit negation.
GOVERNED_COMPOSITE_FIELDS: Final[frozenset[tuple[str, str]]] = frozenset({
    ("syndication", "mandate_status"),
})

def _ontology_unassessable_passages() -> dict[tuple[str, str], tuple[str, ...]]:
    """Load missing-value ownership from passage metadata, not application code."""
    path = Path(__file__).parent / "ontology" / "passages.json"
    ontology = json.loads(path.read_text())
    mapping: dict[tuple[str, str], list[str]] = {}
    for passage in ontology.get("passages", []):
        metadata = passage.get("metadata") or {}
        resource = metadata.get("resource")
        field = metadata.get("field")
        if metadata.get("missing_policy") != "report_unassessable" or not resource or not field:
            continue
        mapping.setdefault((str(resource), str(field)), []).append(str(passage["id"]))
    return {pair: tuple(ids) for pair, ids in mapping.items()}


REPORT_UNASSESSABLE_FILTER_PASSAGES: Final = _ontology_unassessable_passages()
REPORT_UNASSESSABLE_FILTER_FIELDS: Final = frozenset(REPORT_UNASSESSABLE_FILTER_PASSAGES)

# Register linkage keys are useful for evidence but are not reader-facing table columns.
# Keep this resource-specific: identifier semantics are a contract of each Register resource,
# not a convention inferred from a field name shared by unrelated resources.
REGISTER_OPAQUE_IDENTIFIER_FIELDS_BY_RESOURCE: Final[dict[str, frozenset[str]]] = {
    "entities": frozenset({"id"}),
    "people": frozenset({"id"}),
    "counterparties": frozenset({"id"}),
    "leads": frozenset({"id", "entity_id", "converted_deal_id"}),
    "deals": frozenset({"id", "entity_id"}),
    "lending": frozenset({"id", "entity_id", "deal_id"}),
    "syndication": frozenset({"id", "entity_id", "deal_id"}),
    "syndication_lenders": frozenset({"id", "syndication_id", "counterparty_id"}),
    "asset_monetisation": frozenset({"id", "entity_id", "deal_id"}),
}
REGISTER_OPAQUE_IDENTIFIER_FIELDS: Final[frozenset[tuple[str, str]]] = frozenset(
    (resource, field)
    for resource, fields in REGISTER_OPAQUE_IDENTIFIER_FIELDS_BY_RESOURCE.items()
    for field in fields
)

# Compatibility vocabulary for text values already present in imported Register rows.
# It is applied only at Chitti's read boundary; Register data and caller-visible reference
# data remain authoritative and unchanged. State primaries follow the abbreviations used
# by the imported Ledger (notably KE and TE), while common alternate codes remain readable.
STATE_ABBREVIATIONS: Final[dict[str, tuple[str, ...]]] = {
    "Andaman and Nicobar Islands": ("AN",),
    "Andhra Pradesh": ("AP",),
    "Arunachal Pradesh": ("AR",),
    "Assam": ("AS",),
    "Bihar": ("BR",),
    "Chandigarh": ("CH",),
    "Chhattisgarh": ("CG", "CT"),
    "Dadra and Nagar Haveli and Daman and Diu": ("DD", "DN"),
    "Delhi": ("DL",),
    "Goa": ("GA",),
    "Gujarat": ("GJ",),
    "Haryana": ("HR",),
    "Himachal Pradesh": ("HP",),
    "Jammu and Kashmir": ("JK",),
    "Jharkhand": ("JH",),
    "Karnataka": ("KA",),
    "Kerala": ("KE", "KL"),
    "Ladakh": ("LA",),
    "Lakshadweep": ("LD",),
    "Madhya Pradesh": ("MP",),
    "Maharashtra": ("MH",),
    "Manipur": ("MN",),
    "Meghalaya": ("ML",),
    "Mizoram": ("MZ",),
    "Nagaland": ("NL",),
    "Odisha": ("OD", "OR"),
    "Puducherry": ("PY",),
    "Punjab": ("PB",),
    "Rajasthan": ("RJ",),
    "Sikkim": ("SK",),
    "Tamil Nadu": ("TN",),
    "Telangana": ("TE", "TS", "TG"),
    "Tripura": ("TR",),
    "Uttar Pradesh": ("UP",),
    "Uttarakhand": ("UK", "UT"),
    "West Bengal": ("WB",),
}

LEGACY_STATE_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "Andaman & Nicobar Islands": ("AN",),
    "Dadra and Nagar Haveli": ("DN",),
    "Daman and Diu": ("DD",),
    "Jammu & Kashmir": ("JK",),
    "National Capital Territory of Delhi": ("DL",),
    "NCT of Delhi": ("DL",),
    "Orissa": ("OD", "OR"),
    "Pondicherry": ("PY",),
    "Tamilnadu": ("TN",),
    "Uttaranchal": ("UK", "UT"),
}

LEGACY_REFERENCE_ALIASES: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    # The ontology explicitly identifies this imported Lending Stage spelling as
    # compatibility vocabulary for Ready for Disbursement. Keep the equivalence
    # scoped to that category; it must not leak into other lifecycle fields.
    "Lending Stage": {
        "Disbursement Pending": ("Ready for Disbursement",),
    },
    "Sector": {
        "BESS/ Energy Storage": ("BESS / Energy Storage",),
        "CBG & Biomass": ("Biofuels / Biogas / CBG",),
        "EV": ("EV Mobility",),
        "EV Charging/CPO & Manufacturing": ("EV Mobility",),
        "EV Fleets": ("EV Mobility",),
        "Renewables - Solar": (
            "Solar - General",
            "Solar - EPC",
            "Solar - Developer",
            "Solar - Rooftop",
            "Solar - OEM",
        ),
        "Solar": (
            "Solar - General",
            "Solar - EPC",
            "Solar - Developer",
            "Solar - Rooftop",
            "Solar - OEM",
        ),
        "Urban WASH": ("Water Treatment / WASH",),
    },
    "State": {**STATE_ABBREVIATIONS, **LEGACY_STATE_NAMES},
    "Lender Status": {
        "IM in Prep": ("IM Under Preparation",),
        "IM under prep": ("IM Under Preparation",),
        "IM in preparation": ("IM Under Preparation",),
        "IM prep": ("IM Under Preparation",),
        "IM preparation": ("IM Under Preparation",),
        "IM sent": ("IM Circulated",),
        "IM submitted": ("IM Circulated",),
        "onhold": ("On Hold",),
        "hold": ("On Hold",),
        "drop": ("Dropped",),
        "approved": ("Sanctioned",),
        "final sanction received": ("Sanctioned",),
        "rejected": ("Declined",),
    },
}

# Register governs these in its lender transition API, rather than /v1/ref.
LENDER_STATUSES: Final[tuple[str, ...]] = (
    "Identified", "IM Under Preparation", "IM Circulated", "Docs Pending",
    "Queries Received", "IP Received", "Sanctioned", "Disbursed",
    "Declined", "Dropped", "On Hold",
)

# Complete single-valued vocabulary contract exposed by Chitti. ``State`` is a
# Chitti-local vocabulary because Register intentionally does not publish that category.
# Fields without approved historical-value equivalences remain absent until their
# meanings receive an explicit business decision.
CONTROLLED_REFERENCE_FIELDS: Final[dict[str, dict[str, str]]] = {
    "entities": {
        "sector": "Sector",
        "state": "State",
        "lens": "Lens",
        "register_status": "Register Status",
        "entity_type": "Entity Type",
        "lifecycle": "Entity Lifecycle",
    },
    "leads": {
        "sector": "Sector",
        "lens": "Lens",
        "status": "Lead Status",
        "temperature": "Temperature",
    },
    "deals": {
        "product_type": "Product Type",
        "lens": "Lens",
        "stage": "Deal Funnel Stage",
        "temperature": "Temperature",
    },
    "lending": {"stage": "Lending Stage"},
    "syndication_lenders": {"status": "Lender Status"},
    "syndication": {
        "status": "Status of Proposal",
        "priority": "Priority",
        "tenor": "Tenor",
        "im_status": "IM in Place",
        "syndication_type": "Syndication Type",
        "mandate_status3": "Mandate Status 3",
    },
    "asset_monetisation": {
        "status": "Asset Mon Status",
        "state": "State",
    },
}

CONTROLLED_READ_STRATEGIES: Final[dict[tuple[str, str], str]] = {
    (resource, field): (
        "compatibility"
        if (resource, field)
        in {
            ("entities", "sector"),
            ("entities", "state"),
            ("leads", "sector"),
            ("asset_monetisation", "state"),
            ("lending", "stage"),
            ("syndication_lenders", "status"),
        }
        else "exact"
        if field in RESOURCE_SPECS[resource].equality_filters
        else "local"
    )
    for resource, fields in CONTROLLED_REFERENCE_FIELDS.items()
    for field in fields
}
CONTROLLED_COMPATIBILITY_FIELDS: Final[frozenset[tuple[str, str]]] = frozenset(
    pair for pair, strategy in CONTROLLED_READ_STRATEGIES.items() if strategy == "compatibility"
)
LOCAL_CONTROLLED_CATEGORIES: Final[frozenset[str]] = frozenset({"State", "Lender Status"})


def _validate_controlled_contract() -> None:
    for resource, fields in CONTROLLED_REFERENCE_FIELDS.items():
        assert resource in RESOURCE_FIELDS
        for field in fields:
            assert field in RESOURCE_FIELDS[resource]
            strategy = CONTROLLED_READ_STRATEGIES[(resource, field)]
            assert strategy in {"exact", "compatibility", "local"}
            if strategy in {"exact", "compatibility"}:
                assert field in RESOURCE_SPECS[resource].equality_filters
    assert CONTROLLED_READ_STRATEGIES.keys() >= CONTROLLED_COMPATIBILITY_FIELDS


_validate_controlled_contract()

# Structural relationships exposed by the read-only Register contract and documented in
# the Chitti ontology. The unordered endpoint pairs permit either hash-join direction;
# they do not authorize transferring lifecycle or metric meaning between resources.
REGISTER_JOIN_RELATIONSHIPS: Final[frozenset[frozenset[tuple[str, str]]]] = frozenset(
    {
        frozenset({("leads", "entity_id"), ("entities", "id")}),
        frozenset({("deals", "entity_id"), ("entities", "id")}),
        frozenset({("lending", "entity_id"), ("entities", "id")}),
        frozenset({("syndication", "entity_id"), ("entities", "id")}),
        frozenset({("asset_monetisation", "entity_id"), ("entities", "id")}),
        frozenset({("leads", "converted_deal_id"), ("deals", "id")}),
        frozenset({("lending", "deal_id"), ("deals", "id")}),
        frozenset({("syndication", "deal_id"), ("deals", "id")}),
        frozenset({("asset_monetisation", "deal_id"), ("deals", "id")}),
        frozenset({("syndication_lenders", "syndication_id"), ("syndication", "id")}),
        frozenset({("syndication_lenders", "counterparty_id"), ("counterparties", "id")}),
        # The lender-approach ontology explicitly defines an exact-name set comparison,
        # including imported lender rows whose counterparty_id is absent.
        frozenset({("syndication_lenders", "lender_name"), ("counterparties", "name")}),
        *(
            frozenset({(resource, field), ("people", "name")})
            for resource, field in (
                ("leads", "rm"),
                ("deals", "rm"),
                ("deals", "analyst"),
                ("lending", "rm"),
                ("lending", "analyst"),
                ("syndication", "rm"),
                ("syndication", "analyst"),
                ("asset_monetisation", "rm"),
                ("asset_monetisation", "analyst"),
            )
        ),
    }
)
log = get_logger("chitti.register_access")


class RegisterPlanError(ValueError):
    pass


def _reference_tokens(value: Any) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _reference_value(candidate: Any) -> str | None:
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict) and isinstance(candidate.get("value"), str):
        return candidate["value"]
    return None


def controlled_reference_values(
    reference_values: dict[str, list[Any]] | None,
) -> dict[str, list[Any]]:
    """Return the request vocabulary augmented with explicit Chitti-local values."""

    values = dict(reference_values or {})
    values["State"] = list(STATE_ABBREVIATIONS)
    values["Lender Status"] = list(LENDER_STATUSES)
    return values


def canonical_controlled_value(
    resource: str,
    field: str,
    value: Any,
    reference_values: dict[str, list[Any]],
) -> str | None:
    """Resolve case/insignificant whitespace only when one canonical label matches."""

    category = CONTROLLED_REFERENCE_FIELDS.get(resource, {}).get(field)
    if category is None:
        return None
    tokens = _reference_tokens(value)
    if not tokens:
        return None
    if category == "State":
        matches = {
            state
            for state, abbreviations in STATE_ABBREVIATIONS.items()
            if tokens
            in {
                _reference_tokens(item)
                for item in {
                    state,
                    *abbreviations,
                    *(
                        legacy
                        for legacy, legacy_codes in LEGACY_STATE_NAMES.items()
                        if set(legacy_codes) & set(abbreviations)
                    ),
                }
            }
        }
        return next(iter(matches)) if len(matches) == 1 else None
    candidates = controlled_reference_values(reference_values).get(category, [])
    labels = {
        label
        for candidate in candidates
        if (label := _reference_value(candidate)) is not None
        and _reference_tokens(label) == tokens
    }
    if len(labels) == 1:
        return next(iter(labels))
    alias_targets = _alias_targets(value, category)
    visible_targets = {
        label
        for candidate in candidates
        if (label := _reference_value(candidate)) is not None
        and any(_reference_tokens(label) == _reference_tokens(target) for target in alias_targets)
    }
    return next(iter(visible_targets)) if len(visible_targets) == 1 else None


def _alias_targets(value: Any, category: str) -> tuple[str, ...]:
    tokens = _reference_tokens(value)
    for alias, targets in LEGACY_REFERENCE_ALIASES.get(category, {}).items():
        if _reference_tokens(alias) == tokens:
            return targets
    return ()


def _canonical_reference_value(value: Any, reference_values: list[Any], category: str) -> Any:
    """Resolve an exact value, explicit legacy alias, or unique abbreviation.

    The rule is intentionally data-driven and conservative: a shortened stored value is
    compatible only when it is the token prefix of exactly one current caller-visible
    reference label. Thus ``EV`` can resolve when only one current label starts with that
    token, while an ambiguous family word such as ``Solar`` remains unchanged.
    """

    tokens = _reference_tokens(value)
    if not tokens:
        return value
    labels = [label for candidate in reference_values if (label := _reference_value(candidate))]
    exact = [candidate for candidate in labels if _reference_tokens(candidate) == tokens]
    if len(exact) == 1:
        return exact[0]
    alias_targets = _alias_targets(value, category)
    if alias_targets and labels:
        visible_targets = [target for target in alias_targets if target in labels]
        if len(visible_targets) == 1:
            return visible_targets[0]
        return value
    extensions = [
        candidate
        for candidate in labels
        if len(_reference_tokens(candidate)) > len(tokens)
        and _reference_tokens(candidate)[: len(tokens)] == tokens
    ]
    return extensions[0] if len(extensions) == 1 else value


def _controlled_values_match(
    stored: Any,
    requested: Any,
    *,
    category: str,
    reference_values: list[Any],
) -> bool:
    """Compare current and legacy controlled values through their equivalence sets."""

    def equivalents(value: Any) -> set[tuple[str, ...]]:
        canonical = _canonical_reference_value(value, reference_values, category)
        values = {str(value), str(canonical), *_alias_targets(value, category)}
        return {_reference_tokens(item) for item in values if _reference_tokens(item)}

    return bool(equivalents(stored) & equivalents(requested))


def controlled_field_values_match(
    resource: str,
    field: str,
    established: Any,
    requested: Any,
) -> bool:
    """Compare a controlled predicate through its bounded compatibility vocabulary."""

    category = CONTROLLED_REFERENCE_FIELDS.get(resource, {}).get(field)
    if category is None:
        raise ValueError(f"{resource}.{field} is not a controlled reference field")
    if category == "State":
        def state_family(value: Any) -> set[tuple[str, ...]]:
            tokens = _reference_tokens(value)
            for state, abbreviations in STATE_ABBREVIATIONS.items():
                family = {state, *abbreviations}
                family.update(
                    legacy
                    for legacy, legacy_codes in LEGACY_STATE_NAMES.items()
                    if set(legacy_codes) & set(abbreviations)
                )
                family_tokens = {_reference_tokens(item) for item in family}
                if tokens in family_tokens:
                    return family_tokens
            return {tokens} if tokens else set()

        return bool(state_family(established) & state_family(requested))
    return _controlled_values_match(
        established,
        requested,
        category=category,
        reference_values=[],
    )


def canonicalize_controlled_filter_arguments(
    resource: str,
    field: str,
    arguments: dict[str, Any],
    reference_values: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Canonicalize a host filter using the governed field's bounded alias family."""

    category = CONTROLLED_REFERENCE_FIELDS.get(resource, {}).get(field)
    operator = str(arguments.get("operator") or "eq")
    if category is None or operator not in {"eq", "ne", "in", "not_in"}:
        return arguments

    supplied = arguments.get("values") if operator in {"in", "not_in"} else [arguments.get("value")]
    if not isinstance(supplied, list):
        return arguments
    canonical_values: list[Any] = []
    for value in supplied:
        canonical_value = canonical_controlled_value(resource, field, value, reference_values or {})
        resolved: list[Any] = [canonical_value if canonical_value is not None else value]
        if (resource, field) in CONTROLLED_COMPATIBILITY_FIELDS:
            resolved.extend(_alias_targets(value, category))
            resolved.extend(
                alias
                for alias, targets in LEGACY_REFERENCE_ALIASES.get(category, {}).items()
                if any(
                    _reference_tokens(canonical_value if canonical_value is not None else value)
                    == _reference_tokens(target)
                    for target in targets
                )
            )
        for candidate in resolved:
            if candidate not in canonical_values:
                canonical_values.append(candidate)

    canonical_arguments = dict(arguments)
    if operator in {"eq", "ne"} and len(canonical_values) == 1:
        canonical_arguments["value"] = canonical_values[0]
        return canonical_arguments
    canonical_arguments.pop("value", None)
    canonical_arguments["operator"] = "not_in" if operator in {"ne", "not_in"} else "in"
    canonical_arguments["values"] = canonical_values
    return canonical_arguments


def _normalize_controlled_fields(
    resource: str,
    fields: dict[str, Any],
    reference_values: dict[str, list[Any]],
) -> dict[str, Any]:
    normalized = dict(fields)
    reference_values = controlled_reference_values(reference_values)
    for field, category in CONTROLLED_REFERENCE_FIELDS.get(resource, {}).items():
        if normalized.get(field) is not None:
            if (resource, field) in CONTROLLED_COMPATIBILITY_FIELDS:
                normalized[field] = (
                    canonical_controlled_value(
                        resource, field, normalized[field], reference_values
                    )
                    or _canonical_reference_value(
                        normalized[field], reference_values.get(category, []), category
                    )
                )
            else:
                normalized[field] = (
                    canonical_controlled_value(
                        resource, field, normalized[field], reference_values
                    )
                    or normalized[field]
                )
    return normalized


def _normalize_register_evidence(
    evidence: RegisterEvidence,
    reference_values: dict[str, list[Any]],
) -> RegisterEvidence:
    """Normalize deferred evidence and count only values in the accepted corpus."""

    issues = _controlled_value_issue_counts(evidence.read.resource, evidence.records, reference_values)
    normalized_records: list[CanonicalRecord] = []
    for record in evidence.records:
        fields = _normalize_controlled_fields(record.resource, record.fields, reference_values)
        normalized_records.append(record.model_copy(update={"fields": fields}))
    window = evidence.window.model_copy(update={"controlled_value_issues": issues})
    return evidence.model_copy(update={"records": normalized_records, "window": window})


def _controlled_value_issue_counts(
    resource: str,
    records: list[CanonicalRecord],
    reference_values: dict[str, list[Any]],
) -> dict[str, int]:
    mapped_fields = CONTROLLED_REFERENCE_FIELDS.get(resource, {})
    issues: dict[str, int] = {}
    for record in records:
        for field in mapped_fields:
            if controlled_value_issue_count(
                resource,
                field,
                [record.fields.get(field)],
                reference_values,
            ):
                issues[field] = issues.get(field, 0) + 1
    return issues


def controlled_value_issue_count(
    resource: str,
    field: str,
    values: list[Any],
    reference_values: dict[str, list[Any]],
) -> int:
    """Count unresolved controlled values within an already selected cohort."""

    category = CONTROLLED_REFERENCE_FIELDS.get(resource, {}).get(field)
    if category is None:
        return 0
    governed = controlled_reference_values(reference_values).get(category, [])
    labels = [_reference_value(item) for item in governed]
    if not labels:
        return 0
    return sum(
        raw_value is not None
        and canonical_controlled_value(resource, field, raw_value, reference_values) is None
        and (
            (resource, field) not in CONTROLLED_COMPATIBILITY_FIELDS
            or _canonical_reference_value(raw_value, governed, category) == raw_value
        )
        for raw_value in values
    )


class RegisterAccess:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def validate_read(request: RegisterRead) -> ResourceSpec:
        spec = RESOURCE_SPECS.get(request.resource)
        if spec is None:
            raise RegisterPlanError(f"Unknown Register resource '{request.resource}'.")
        unknown_filters = sorted(set(request.filters) - spec.equality_filters)
        if unknown_filters:
            raise RegisterPlanError(
                f"Unsupported filter(s) for '{request.resource}': {', '.join(unknown_filters)}."
            )
        return spec

    async def read(
        self,
        request: RegisterRead,
        *,
        identity: CallerIdentity,
        request_id: str,
        reference_values: dict[str, list[Any]] | None = None,
        defer_controlled_normalization: bool = False,
    ) -> RegisterEvidence:
        spec = self.validate_read(request)
        started = utc_now()
        mapped_fields = CONTROLLED_REFERENCE_FIELDS.get(request.resource, {})
        controlled_fields = {
            field: category
            for field, category in mapped_fields.items()
            if field in request.filters
            and (request.resource, field) in CONTROLLED_COMPATIBILITY_FIELDS
        }
        if defer_controlled_normalization and controlled_fields:
            raise RegisterPlanError(
                "Deferred controlled normalization cannot be used with controlled filters."
            )
        reference_values = controlled_reference_values(
            reference_values
            if reference_values is not None
            else (
                await self.reference_values(identity=identity, request_id=request_id)
                if mapped_fields and not defer_controlled_normalization
                else {}
            )
        )
        log.info(
            "register_read_started",
            extra={
                "resource": request.resource,
                "q": request.q,
                "filters": request.filters,
                "request_id": request_id,
                "caller": identity.email,
                "scope": identity.display_scope,
            },
        )
        records: list[CanonicalRecord] = []
        # Controlled text filters are evaluated after conservative read-boundary
        # normalization. Sending the current label to Register first would exclude rows
        # that still hold a uniquely resolvable predecessor value.
        # Register interprets commas as IN separators. Preserve literal scalar equality
        # locally, including values resolved from dependent reads. Keep all other safe
        # predicates on the server and retain the normal pagination/completeness limits.
        literal_filters = {
            field: value for field, value in request.filters.items()
            if field not in controlled_fields and isinstance(value, str) and "," in value
        }
        filters: dict[str, Any] = {
            field: value for field, value in request.filters.items()
            if field not in controlled_fields and field not in literal_filters
        }
        controlled_filters = {
            field: value for field, value in request.filters.items() if field in controlled_fields
        }
        cursor: str | None = None
        pages = 0
        completeness = Completeness.COMPLETE

        while pages < self.settings.max_pages_per_resource:
            # The signed token is freshly bound to this exact GET call. The client is
            # likewise short-lived so caller context can never leak across requests.
            token = mint_register_context(identity, self.settings, method="GET", path=spec.path)
            config = RegisterClientConfig(
                base_url=self.settings.register_base_url,
                ca_file=self.settings.register_ca_file,
                api_key=self.settings.register_api_key,
                tenant=identity.tenant,
                actor="chitti",
                connect_timeout_s=min(5.0, self.settings.register_timeout_seconds),
                read_timeout_s=self.settings.register_timeout_seconds,
                extra_headers={"X-Internal-Context": token},
            )
            async with AsyncRegisterClient(config=config) as client:
                page = await client.list(
                    spec.api_name,
                    limit=self.settings.page_size,
                    cursor=cursor,
                    q=request.q,
                    request_id=request_id,
                    **filters,
                )
            pages += 1
            remaining = self.settings.max_records_per_request - len(records)
            for raw_row in page.items:
                if any(raw_row.get(field) != value for field, value in literal_filters.items()):
                    continue
                if any(
                    not _controlled_values_match(
                        raw_row.get(field),
                        requested,
                        category=controlled_fields[field],
                        reference_values=reference_values.get(controlled_fields[field], []),
                    )
                    for field, requested in controlled_filters.items()
                ):
                    continue
                record_id = str(raw_row.get("id") or "")
                if not record_id:
                    raise RegisterPlanError(
                        f"Register resource '{request.resource}' returned a record without id."
                    )
                records.append(
                    CanonicalRecord(resource=request.resource, record_id=record_id, fields=dict(raw_row))
                )
                if len(records) >= self.settings.max_records_per_request:
                    break
            cursor = page.next_cursor
            if len(records) >= self.settings.max_records_per_request:
                if cursor or len(page.items) > remaining:
                    completeness = Completeness.PARTIAL_LIMIT
                break
            if not cursor:
                break

        if cursor and pages >= self.settings.max_pages_per_resource:
            completeness = Completeness.PARTIAL_LIMIT

        result = RegisterEvidence(
            read=request,
            records=records,
            window=RetrievalWindow(
                resource=request.resource,
                started_at=started,
                completed_at=utc_now(),
                pages_retrieved=pages,
                records_retrieved=len(records),
                completeness=completeness,
                next_cursor_present=bool(cursor),
                controlled_value_issues={},
            ),
        )
        log.info(
            "register_read_completed",
            extra={
                "resource": request.resource,
                "pages": pages,
                "records": len(records),
                "completeness": completeness,
                "request_id": request_id,
            },
        )
        return _normalize_register_evidence(result, reference_values)

    async def reference_values(self, *, identity: CallerIdentity, request_id: str) -> dict[str, list[Any]]:
        """Read current caller-visible controlled vocabulary from Register."""

        path = "/v1/ref"
        token = mint_register_context(identity, self.settings, method="GET", path=path)
        config = RegisterClientConfig(
            base_url=self.settings.register_base_url,
            ca_file=self.settings.register_ca_file,
            api_key=self.settings.register_api_key,
            tenant=identity.tenant,
            actor="chitti",
            connect_timeout_s=min(5.0, self.settings.register_timeout_seconds),
            read_timeout_s=self.settings.register_timeout_seconds,
            extra_headers={"X-Internal-Context": token},
        )
        async with AsyncRegisterClient(config=config) as client:
            result = await client.ref(request_id=request_id)
        if not isinstance(result, dict) or not all(
            isinstance(category, str) and isinstance(values, list) for category, values in result.items()
        ):
            raise RegisterPlanError("Register reference data returned an invalid shape.")
        log.info(
            "register_reference_values_completed",
            extra={
                "categories": len(result),
                "values": sum(len(values) for values in result.values()),
                "request_id": request_id,
            },
        )
        return result
