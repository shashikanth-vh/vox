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
    assert reg.patches == [{"stage": "CP/CS Completed"}], reg.patches
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
