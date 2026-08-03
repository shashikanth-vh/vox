"""The MAKER's half — `GET /v1/workflows/actions`.

The approver's half has always been server-described: `/v1/workflows/pending` hands back
the verbs and Today renders whatever it is given. The maker's half was not, so the UI had
no way to start a committee run, prepare a checklist or attest a handover — that whole
spine lived in Postman.

These tests pin the two properties that make the pattern worth having: the SEQUENCING
rules live here rather than in a client, and an unavailable step still comes back, with
the reason why.
"""

from __future__ import annotations

import httpx
import pytest

from app.api import _evaluate_action, _MAKER_ACTIONS
from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _action(key: str, subject: str = "Lending") -> dict:
    return next(a for a in _MAKER_ACTIONS[subject] if a["key"] == key)


# --------------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------------- #
def test_stage_gate_explains_the_sequence_rather_than_hiding_it():
    """The whole point of returning a disabled action: the reason teaches the process."""
    prepare = _action("cpcs.prepare")
    ok, reason = _evaluate_action(prepare, roles={"Credit Head"}, stage="Diligence",
                                  run_state="none")
    assert not ok
    assert reason == "Available once the committee has sanctioned this facility."
    ok, _ = _evaluate_action(prepare, roles={"Credit Head"}, stage="Sanctioned",
                             run_state="none")
    assert ok


def test_role_gate_names_who_does_the_step():
    """A refusal that names the role is actionable; "not permitted" is not."""
    attest = _action("advaya.attest")
    ok, reason = _evaluate_action(attest, roles={"BDRM"}, stage="Ready for Disbursement",
                                  run_state="none")
    assert not ok
    assert "Credit Head" in reason and "Management" in reason


def test_role_gate_is_checked_before_the_stage_gate():
    """Who you are does not change with the subject, so it is the more useful answer of
    the two when both fail."""
    attest = _action("advaya.attest")
    ok, reason = _evaluate_action(attest, roles={"BDRM"}, stage="Data Awaited",
                                  run_state="none")
    assert not ok and "Credit Head" in reason


def test_run_gates_cover_none_live_and_returned():
    start = _action("deal-structuring.start")
    revise = _action("deal-structuring.revise-credit-note")
    resubmit = _action("run.resubmit")

    # A second committee run must not be startable while one is open.
    assert _evaluate_action(start, roles={"Credit Head"}, stage="Diligence",
                            run_state="none")[0]
    ok, reason = _evaluate_action(start, roles={"Credit Head"}, stage="Diligence",
                                  run_state="live")
    assert not ok and "already open" in reason

    # Revise / resubmit are the RETURNED path and nothing else.
    for spec in (revise, resubmit):
        assert not _evaluate_action(spec, roles={"Credit Head"}, stage="Diligence",
                                    run_state="live")[0]
        assert _evaluate_action(spec, roles={"Credit Head"}, stage="Diligence",
                                run_state="returned")[0]


def test_every_action_can_be_rendered_by_a_client():
    """A client renders these blind, so each one must carry what it needs: a label, a
    method, a URL, and a form whose fields are all of a type the client knows."""
    known = {"text", "textarea", "number", "date", "select"}
    for subject, specs in _MAKER_ACTIONS.items():
        keys = [s["key"] for s in specs]
        assert len(keys) == len(set(keys)), f"duplicate action key in {subject}"
        for spec in specs:
            assert spec["label"] and spec["method"] in {"POST", "PATCH"}
            assert spec["url"].startswith("/v1/"), spec["key"]
            for field in spec["form"]:
                assert field["type"] in known, (spec["key"], field)
                if field["type"] == "select":
                    assert field.get("options"), (spec["key"], field["name"])
            # A URL naming a run must only appear on an action gated to a live run —
            # otherwise it renders with an empty workflow id.
            if "{workflow_id}" in spec["url"]:
                assert spec.get("run") in {"live", "returned"}, spec["key"]


# --------------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------------- #
class _Http:
    """Stands in for the Register: serves the one subject row under test."""

    def __init__(self, row: dict) -> None:
        self.row = row

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        return httpx.Response(200, json=self.row, request=httpx.Request("GET", url))


def _app(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from app.api import create_app

    app = create_app()
    app.state.oidc = None
    app.state.temporal = None       # no Temporal here: the run lookup is fail-soft
    app.state.http = None
    return app


async def _get(app, params, api_key="k"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://orch") as c:
        return await c.get("/v1/workflows/actions", params=params,
                           headers={"X-API-Key": api_key})


async def test_actions_lists_available_and_blocked_side_by_side(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _Http({"id": "22222222-2222-2222-2222-222222222222",
                            "deal_id": "33333333-3333-3333-3333-333333333333",
                            "stage": "Diligence"})
    r = await _get(app, {"subject_type": "Lending",
                         "subject_id": "22222222-2222-2222-2222-222222222222"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subject"]["stage"] == "Diligence"
    by_key = {a["key"]: a for a in body["actions"]}

    # Available now, and pre-filled with the ids the client should not have to know.
    send = by_key["deal-structuring.start"]
    assert send["enabled"] and send["method"] == "POST"
    assert send["body"]["deal_id"] == "33333333-3333-3333-3333-333333333333"

    # Blocked, and SAYS SO — this is the property that replaces a dropdown of four
    # stages the platform would always refuse.
    blocked = by_key["cpcs.prepare"]
    assert not blocked["enabled"]
    assert blocked["reason"] == "Available once the committee has sanctioned this facility."
    get_settings.cache_clear()


async def test_actions_refuses_an_unknown_subject_type(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    r = await _get(app, {"subject_type": "Nonsense",
                         "subject_id": "22222222-2222-2222-2222-222222222222"})
    assert r.status_code == 422 and "Unknown subject_type" in r.text
    get_settings.cache_clear()


async def test_actions_refuses_a_malformed_subject_id(monkeypatch):
    """The literal "null"/"undefined" an unset client variable sends — refused at the
    door rather than passed to the register as a query argument."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    for bad in ("null", "undefined", "{{lendingId}}"):
        r = await _get(app, {"subject_type": "Lending", "subject_id": bad})
        assert r.status_code == 422, (bad, r.text)
    get_settings.cache_clear()


async def test_syndication_and_am_carry_their_own_catalogues(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _Http({"id": "44444444-4444-4444-4444-444444444444",
                            "deal_id": "33333333-3333-3333-3333-333333333333",
                            "status": "Deal Sourced"})
    r = await _get(app, {"subject_type": "Syndication",
                         "subject_id": "44444444-4444-4444-4444-444444444444"})
    assert r.status_code == 200, r.text
    keys = {a["key"] for a in r.json()["actions"]}
    assert "syndication.start" in keys and "syndication.lender-update" in keys
    assert not any(k.startswith("deal-structuring") for k in keys)
    get_settings.cache_clear()
