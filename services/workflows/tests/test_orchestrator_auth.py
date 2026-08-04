"""Orchestrator approval security — identity must be token-derived and mandatory.

These tests never touch Temporal: the refusals happen in the auth layer, before any
workflow client call, so we build the app and set app.state manually instead of running
the (Temporal-connecting) lifespan.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _app(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from app.api import create_app

    app = create_app()
    # Stand in for what lifespan would set — no Temporal, no OIDC verifier.
    app.state.oidc = None
    app.state.temporal = None
    app.state.http = None
    return app


async def _post(app, path, json, api_key="k"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post(path, json=json, headers={"X-API-Key": api_key})


async def test_require_auth_refuses_approval_without_oidc(monkeypatch):
    """With require_auth on but no OIDC configured, an approval is REFUSED — the
    orchestrator will not trust a caller-supplied 'by'."""
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/leadconv-x/approve",
                    {"by": "attacker@example.com"})
    assert r.status_code == 401, r.text
    assert "verified identity" in r.text.lower()
    get_settings.cache_clear()


async def test_require_auth_refuses_conversion_request_without_oidc(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/lead-conversions",
                    {"lead_id": "11111111-1111-1111-1111-111111111111",
                     "requested_by": "attacker@example.com"})
    assert r.status_code == 401, r.text
    get_settings.cache_clear()


async def test_bad_api_key_blocks_before_anything(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        r = await c.post("/v1/workflows/leadconv-x/approve",
                         json={"by": "x"}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The three business-lifecycle workflow endpoints are equally auth-gated.
# --------------------------------------------------------------------------- #
async def test_business_workflow_starts_require_verified_identity(monkeypatch):
    """Starting qualification / structuring / document collection under require_auth (no OIDC)
    is refused — never started under a caller-supplied name."""
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    cases = [
        ("/v1/workflows/lead-qualifications", {"lead_id": "l1", "qualified_by": "a@x.com"}),
        ("/v1/workflows/deal-structurings", {"deal_id": "d1", "requested_by": "a@x.com"}),
        ("/v1/workflows/document-collections",
         {"subject_type": "Deal", "subject_id": "d1", "requested_by": "a@x.com"}),
    ]
    for path, body in cases:
        r = await _post(app, path, body)
        assert r.status_code == 401, f"{path}: {r.text}"
    get_settings.cache_clear()


async def test_committee_decision_requires_verified_identity(monkeypatch):
    """The Credit Committee decision is refused without a verified identity — a caller-supplied
    'by' can never manufacture a committee outcome at the door."""
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/struct-x/committee-decision",
                    {"by": "attacker@x.com", "approved": True})
    assert r.status_code == 401, r.text
    get_settings.cache_clear()


async def test_business_endpoints_reject_bad_api_key(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        for path, body in (
            ("/v1/workflows/deal-structurings", {"deal_id": "d1", "requested_by": "x"}),
            ("/v1/workflows/struct-x/committee-decision", {"by": "x", "approved": True}),
            ("/v1/workflows/docs-x/document-received", {"name": "kyc"}),
        ):
            r = await c.post(path, json=body, headers={"X-API-Key": "wrong"})
            assert r.status_code == 401, f"{path}: {r.text}"
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Id fields refuse client-variable garbage at the door.
# --------------------------------------------------------------------------- #
async def test_vox_rejects_null_string_entity_id(monkeypatch):
    """An unset Postman/JS variable arrives as the LITERAL string "null" — truthy, so
    it used to sail into the workflow and die deep in a register query as a 500. The
    door now refuses it as 422 with a message that names the problem."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    for bad in ("null", "undefined", "{{entityId}}"):
        r = await _post(app, "/v1/workflows/vox-touchpoints",
                        {"capture_id": "cap-1", "entity_id": bad})
        assert r.status_code == 422, f"{bad!r}: {r.status_code} {r.text}"
        assert "not a UUID" in r.text
    get_settings.cache_clear()


async def test_conversion_rejects_null_string_lead_id(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/lead-conversions",
                    {"lead_id": "null", "requested_by": "rm@evamfinance.com"})
    assert r.status_code == 422, r.text
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Pre-flight: a conversion that CANNOT succeed is refused at the door.
# --------------------------------------------------------------------------- #
# EntityCreate's field names, verbatim — services/register/app/schemas/resources.py.
# A register-side test asserts this set is still exactly what EntityCreate accepts, so
# the two cannot drift apart silently.
_ENTITY_CREATE_FIELDS = {
    "code", "legal_name", "display_name", "entity_type", "cin", "pan", "gstin",
    "sector", "sub_sector", "lens", "state", "location", "register_status", "lifecycle",
    "promoter_group_code", "about", "toi", "notes", "tags",
}


class _LeadHttp:
    """A stand-in Register: serves GET /v1/leads/{id}, the entity search, and records
    the client-create / lead-link writes the conversion pre-flight makes."""

    def __init__(self, row: dict | None, entities: list | None = None,
                 people: dict | None = None) -> None:
        self.row = row
        self.entities = entities or []
        # name (lowercased) → the roster rows it denotes, as /v1/people/resolve answers.
        self.people = people or {}
        self.created: list[dict] = []
        self.patched: list[dict] = []

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        if "/v1/people/resolve" in str(url):
            wanted = ((kwargs.get("params") or {}).get("name") or "").strip().lower()
            rows = self.people.get(wanted, [])
            return httpx.Response(200, json={
                "query": wanted,
                "resolved": rows[0] if len(rows) == 1 else None,
                "candidates": rows}, request=httpx.Request("GET", url))
        if "/v1/entities" in str(url):
            return httpx.Response(200, json={"items": self.entities},
                                  request=httpx.Request("GET", url))
        if self.row is None:
            return httpx.Response(404, json={"error": "missing"},
                                  request=httpx.Request("GET", url))
        return httpx.Response(200, json=self.row,
                              request=httpx.Request("GET", url))

    async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
        body = kwargs.get("json") or {}
        self.created.append(body)
        # The real EntityCreate FORBIDS extra fields, and this fake used to accept
        # anything — which is how `industry_type` (the register calls it `toi`) shipped
        # and refused every genuinely-new company with a 422 the moment it was pushed.
        extra = sorted(set(body) - _ENTITY_CREATE_FIELDS)
        if extra:
            return httpx.Response(422, json={"error": {
                "type": "validation_error", "title": "Validation failed",
                "detail": "One or more fields are invalid.",
                "errors": [{"type": "extra_forbidden", "loc": ["body", f],
                            "msg": "Extra inputs are not permitted"} for f in extra]}},
                request=httpx.Request("POST", url))
        return httpx.Response(201, json={"id": "ent-new", **body},
                              request=httpx.Request("POST", url))

    async def patch(self, url, **kwargs):  # noqa: ANN001, ANN003
        self.patched.append(kwargs.get("json") or {})
        return httpx.Response(200, json={"id": "l1", **(kwargs.get("json") or {})},
                              request=httpx.Request("PATCH", url))


async def _post_conv(app, lead_id="11111111-1111-1111-1111-111111111111"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post("/v1/workflows/lead-conversions",
                            json={"lead_id": lead_id, "requested_by": "rm@evamfinance.com"},
                            headers={"X-API-Key": "k", "X-Tenant": "EVAM"})


async def test_created_client_uses_only_fields_the_register_accepts(monkeypatch):
    """Every key of the client-create body must be a real EntityCreate field.

    `industry_type` was not one — the register calls it `toi` — so the create branch
    answered 422 "One or more fields are invalid." for every genuinely-new company, and
    the dialog showed nothing more useful than that.
    """
    app = _app(monkeypatch)
    http = _LeadHttp({"id": "l1", "lead_no": "LD-140", "company": "Brand New Co",
                      "entity_id": None, "status": "Active"})
    app.state.http = http
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://orch") as c:
            await c.post("/v1/workflows/lead-conversions",
                         json={"lead_id": "11111111-1111-1111-1111-111111111111",
                               "requested_by": "rm@evamfinance.com",
                               "company_name": "Brand New Co", "sector": "Other",
                               "lens": "Mitigation", "state": "Karnataka",
                               "industry": "EPC", "about": "good company"},
                         headers={"X-API-Key": "k", "X-Tenant": "EVAM"})
    assert http.created, "no client was created"
    body = http.created[0]
    assert set(body) <= _ENTITY_CREATE_FIELDS, sorted(set(body) - _ENTITY_CREATE_FIELDS)
    assert body["toi"] == "EPC"                      # not industry_type
    assert http.patched and http.patched[0]["entity_id"] == "ent-new"
    get_settings.cache_clear()


async def test_unsearchable_client_master_refuses_rather_than_duplicating(monkeypatch):
    """A search that did NOT RUN must not read as "no such client".

    Falling through to the create branch on a failed search puts a second copy of a
    company on the register — the one thing the canonical match exists to prevent.
    """
    app = _app(monkeypatch)

    class _Blind(_LeadHttp):
        async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
            if "/v1/entities" in str(url):
                return httpx.Response(503, json={"error": "down"},
                                      request=httpx.Request("GET", url))
            return await super().get(url, **kwargs)

    http = _Blind({"id": "l1", "lead_no": "LD-141", "company": "Unsearchable Co",
                   "entity_id": None, "status": "Active"})
    app.state.http = http
    r = await _post_conv(app)
    assert r.status_code == 503, r.text
    assert not http.created, "a failed search must not create a duplicate client"
    get_settings.cache_clear()


async def test_conversion_creates_the_client_for_an_unlinked_lead(monkeypatch):
    """The Push-to-Deals promise — "one save: client + deal + product rows". A lead with
    no entity_id used to start a run that FAILED 0.5s later (a deal must belong to a
    company) and reached no approver. Now the client is created from the lead's company
    and linked, so the conversion proceeds."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    http = _LeadHttp({"id": "l1", "lead_no": "LD-139", "company": "Solar Mukesh",
                      "entity_id": None, "status": "Active"})
    app.state.http = http
    # Temporal is absent here, so the START raises after the pre-flight — which is exactly
    # what these assertions cover: the company is settled BEFORE any run is attempted.
    with contextlib.suppress(Exception):
        await _post_conv(app)
    assert http.created and http.created[0]["legal_name"] == "Solar Mukesh"
    assert http.created[0]["register_status"] == "Pipeline"
    assert http.patched and http.patched[0]["entity_id"] == "ent-new"
    get_settings.cache_clear()


async def test_conversion_links_an_existing_client_canonically(monkeypatch):
    """A near-name match reuses the EXISTING client instead of minting a near-duplicate:
    'Solar Mukesh Pvt Ltd' is the same company as 'Solar Mukesh Private Limited'."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    http = _LeadHttp({"id": "l1", "lead_no": "LD-139",
                      "company": "Solar Mukesh Pvt Ltd", "entity_id": None,
                      "status": "Active"},
                     entities=[{"id": "ent-existing",
                                "legal_name": "Solar Mukesh Private Limited"}])
    app.state.http = http
    with contextlib.suppress(Exception):
        await _post_conv(app)
    assert not http.created                       # nothing new was minted
    assert http.patched[0]["entity_id"] == "ent-existing"
    get_settings.cache_clear()


async def test_conversion_refuses_a_lead_with_no_company_name(monkeypatch):
    """The one case that cannot be settled: no company NAME anywhere. Refused at the
    door with the remedy, never as a dead workflow."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _LeadHttp({"id": "l1", "lead_no": "LD-139", "company": "",
                                "entity_id": None, "status": "Active"})
    r = await _post_conv(app)
    assert r.status_code == 422, r.text
    assert "no company name" in r.text and "LD-139" in r.text
    get_settings.cache_clear()


async def test_conversion_refuses_an_already_converted_lead(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _LeadHttp({"id": "l1", "lead_no": "LD-140", "company": "x",
                                "entity_id": "e-1", "status": "Converted"})
    r = await _post_conv(app)
    assert r.status_code == 409 and "already" in r.text
    get_settings.cache_clear()


async def test_conversion_reports_a_missing_lead(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _LeadHttp(None)
    r = await _post_conv(app)
    assert r.status_code == 404
    get_settings.cache_clear()


async def _guarded_post(app):
    """POST a conversion and return the response, or None when the request got as far as
    the Temporal start (there is no Temporal here — reaching it IS the pass condition for
    a pre-flight guard that must not fire)."""
    try:
        return await _post_conv(app)
    except Exception:                                    # noqa: BLE001 — no Temporal here
        return None


def _passed_the_guard(resp) -> bool:                     # noqa: ANN001
    return resp is None or resp.status_code != 422


async def test_a_conversion_naming_an_unknown_rm_is_refused_before_anyone_approves(
        monkeypatch):
    """The register validates rm/analyst on the CONVERT call — which is on the far side
    of a human approval.

    So a lead whose RM was never added under People was accepted, parked, shown to an
    approver, approved, and only THEN failed: the approver was handed
    "Unknown rm 'e2e.rm'" about somebody else's data, with the run dead behind it. The
    same rule, asked of the same roster, before the run starts.
    """
    app = _app(monkeypatch)
    app.state.http = _LeadHttp(
        {"id": "l1", "lead_no": "LD-9", "company": "Known Co", "entity_id": "ent-1",
         "status": "Active", "rm": "e2e.rm"},
        people={"priya": [{"name": "Priya", "full_name": "Priya Nair",
                           "email": "priya@evamfinance.com"}]})
    r = await _post_conv(app)
    assert r.status_code == 422, r.text
    detail = r.json()["error"]["detail"]
    assert "e2e.rm" in detail and "People" in detail, detail
    get_settings.cache_clear()


async def test_a_conversion_naming_a_person_on_record_still_starts(monkeypatch):
    """The guard must not become the new way conversions fail: a name the roster DOES
    know passes straight through it."""
    app = _app(monkeypatch)
    app.state.http = _LeadHttp(
        {"id": "l1", "lead_no": "LD-9", "company": "Known Co", "entity_id": "ent-1",
         "status": "Active", "rm": "Priya"},
        people={"priya": [{"name": "Priya", "full_name": "Priya Nair",
                           "email": "priya@evamfinance.com"}]})
    assert _passed_the_guard(await _guarded_post(app))
    get_settings.cache_clear()


async def test_a_roster_that_cannot_be_read_does_not_refuse_the_conversion(monkeypatch):
    """A lookup that did not RUN is not a refusal. The register being unreachable must
    not block a conversion the roster would have allowed — the convert call checks
    again anyway, where a failure is recoverable."""
    class _Down(_LeadHttp):
        async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
            if "/v1/people/resolve" in str(url):
                raise httpx.ConnectError("register down")
            return await super().get(url, **kwargs)

    app = _app(monkeypatch)
    app.state.http = _Down({"id": "l1", "lead_no": "LD-9", "company": "Known Co",
                            "entity_id": "ent-1", "status": "Active", "rm": "Priya"})
    assert _passed_the_guard(await _guarded_post(app))
    get_settings.cache_clear()
