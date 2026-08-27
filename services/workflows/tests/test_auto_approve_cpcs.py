"""The CPCS auto-approval must hang off the endpoint the checklist dialog calls.

Three field rounds of 'auto approve did not work' with a completely silent log came
down to one fact: the policy block was attached to the Advaya-handover endpoint, not
to POST /v1/workflows/cpcs-checklists — so it simply never ran. This test pins the
whole chain to the RIGHT door: send a checklist with the flag on, and the register
must receive the approve, the minted evidence, and the stage move, with no human.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.config import get_settings
from tests.test_maker_actions import _app

pytestmark = pytest.mark.asyncio

LID = "22222222-2222-2222-2222-222222222222"


class _Temporal:
    """Accepts the workflow start and hands back a handle with an id — the auto task
    never touches the handle again (it polls the register instead)."""

    async def start_workflow(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        return SimpleNamespace(id=kw.get("id") or "cpcs-x-v1")


class _Register:
    """The register as the auto path sees it: the poll finds the Completed row, the
    approve settles it, the evidence mints, the stage moves. Every write is recorded."""

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.posts: list[str] = []
        self.patches: list[dict] = []

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        assert "/v1/internal/cpcs-checklists" in str(url)
        return httpx.Response(200, request=httpx.Request("GET", str(url)), json=[
            {"id": "c1", "lending_id": LID, "checklist_version": 1,
             "status": "Completed", "items": self.items}])

    async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
        u = str(url)
        self.posts.append(u)
        if "/approve" in u:
            return httpx.Response(200, request=httpx.Request("POST", u), json={
                "id": "c1", "lending_id": LID, "checklist_version": 1,
                "status": "Approved", "items": self.items})
        if "/v1/evidence" in u:
            return httpx.Response(201, request=httpx.Request("POST", u), json={"id": "e1"})
        return httpx.Response(200, request=httpx.Request("POST", u), json={})

    async def patch(self, url, **kwargs):  # noqa: ANN001, ANN003
        self.patches.append(dict(kwargs.get("json") or {}))
        return httpx.Response(200, request=httpx.Request("PATCH", str(url)),
                              json={"id": LID, "stage": "CP/CS Completed"})


async def _send(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://orch") as c:
        return await c.post(
            "/v1/workflows/cpcs-checklists",
            json={"lending_id": LID, "requested_by": "maker@evamfinance.com",
                  "checklist_version": 1,
                  "items": [{"key": "cp1", "label": "Board resolution",
                             "condition_type": "CP", "status": "Completed"}]},
            headers={"X-API-Key": "k", "X-User-Email": "maker@evamfinance.com",
                     "X-User-Roles": "Credit Analyst"})


async def test_sending_the_checklist_auto_approves_when_the_flag_is_on(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k", WORKFLOWS_AUTO_APPROVE="true",
               WORKFLOWS_AUTO_POLL_SECONDS="0")
    app.state.temporal = _Temporal()
    reg = _Register([{"key": "cp1", "condition_type": "CP", "status": "Completed"}])
    app.state.http = reg

    r = await _send(app)
    assert r.status_code == 202, r.text
    assert "auto-approval (policy) queued" in r.json()["status"]

    # The policy runs in a held background task — reachable via app.state, by design.
    tasks = list(app.state.auto_tasks)
    assert tasks, "the auto-approval task must exist (and be held against GC)"
    await asyncio.gather(*tasks)

    assert any("/v1/internal/cpcs-checklists/c1/approve" in p for p in reg.posts), reg.posts
    assert any("/v1/evidence" in p for p in reg.posts), reg.posts
    assert reg.patches == [{"stage": "Ready for Disbursement"}], reg.patches
    get_settings.cache_clear()


async def test_the_flag_off_leaves_the_checker_gate_alone(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.temporal = _Temporal()
    reg = _Register([{"key": "cp1", "condition_type": "CP", "status": "Completed"}])
    app.state.http = reg

    r = await _send(app)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "prepared"
    assert not app.state.auto_tasks
    assert reg.posts == [] and reg.patches == []
    get_settings.cache_clear()


async def test_an_empty_checklist_is_accepted_and_auto_approves(monkeypatch):
    """An unconditional letter's checklist has zero items — the start endpoint takes it
    and the policy walks the same approve door; with no CS at all, the book will end at
    'Disbursed' (the stage still moves to Ready for Disbursement here)."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k", WORKFLOWS_AUTO_APPROVE="true",
               WORKFLOWS_AUTO_POLL_SECONDS="0")
    app.state.temporal = _Temporal()
    reg = _Register([])
    app.state.http = reg
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://orch") as c:
        r = await c.post(
            "/v1/workflows/cpcs-checklists",
            json={"lending_id": LID, "requested_by": "maker@evamfinance.com",
                  "checklist_version": 1, "items": []},
            headers={"X-API-Key": "k", "X-User-Email": "maker@evamfinance.com",
                     "X-User-Roles": "Credit Analyst"})
    assert r.status_code == 202, r.text
    await asyncio.gather(*list(app.state.auto_tasks))
    assert any("/approve" in p for p in reg.posts), reg.posts
    assert reg.patches == [{"stage": "Ready for Disbursement"}], reg.patches
    get_settings.cache_clear()


class _HealRegister:
    """An imported lending line with NO Deals row: the send must create the deal,
    link the line, and only then carry on. The mint step answers 500 here so the
    test stops cleanly after the healing it is about."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[dict] = []

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        return httpx.Response(200, request=httpx.Request("GET", str(url)), json={
            "id": LID, "entity_id": "e-777", "stage": "Diligence", "rm": "Shubh Dave"})

    async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
        u = str(url)
        self.posts.append((u, dict(kwargs.get("json") or {})))
        if u.endswith("/v1/deals"):
            return httpx.Response(201, request=httpx.Request("POST", u), json={"id": "d-heal"})
        return httpx.Response(500, request=httpx.Request("POST", u), json={})

    async def patch(self, url, **kwargs):  # noqa: ANN001, ANN003
        self.patches.append(dict(kwargs.get("json") or {}))
        return httpx.Response(200, request=httpx.Request("PATCH", str(url)),
                              json={"id": LID, "deal_id": "d-heal"})


async def test_send_to_committee_heals_a_line_with_no_deal(monkeypatch):
    """An imported line answered 'deal_id: Field required' to the committee send — a
    field name the user cannot act on. With lending_id in the payload the send now
    creates the Deals row, links the line, and proceeds."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.temporal = _Temporal()
    reg = _HealRegister()
    app.state.http = reg
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://orch") as c:
        r = await c.post("/v1/workflows/deal-structurings",
                         json={"lending_id": LID, "requested_by": "da@evamfinance.com"},
                         headers={"X-API-Key": "k", "X-User-Email": "da@evamfinance.com",
                                  "X-User-Roles": "Deal Analyst"})
        # The mint intentionally 500s in this fake — what matters is the healing that
        # happened before it, not the status of this particular send.
        deal_posts = [(u, b) for u, b in reg.posts if u.endswith("/v1/deals")]
        assert deal_posts and deal_posts[0][1]["entity_id"] == "e-777", reg.posts
        assert deal_posts[0][1]["is_lending"] is True
        assert reg.patches == [{"deal_id": "d-heal"}], reg.patches
        assert r.status_code != 422, r.text

        # And with NEITHER id the refusal names the way out.
        bad = await c.post("/v1/workflows/deal-structurings",
                           json={"requested_by": "da@evamfinance.com"},
                           headers={"X-API-Key": "k", "X-User-Email": "da@evamfinance.com",
                                    "X-User-Roles": "Deal Analyst"})
        assert bad.status_code == 422 and "lending_id" in bad.text
    get_settings.cache_clear()
