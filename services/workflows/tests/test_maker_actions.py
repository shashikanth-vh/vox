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

from app.api import _evaluate_action, _IDENTITY_FOR, _MAKER_ACTIONS
from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _action(key: str, subject: str = "Lending") -> dict:
    return next(a for a in _MAKER_ACTIONS[subject] if a["key"] == key)


# --------------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------------- #
def test_stage_gate_explains_the_sequence_rather_than_hiding_it():
    """The whole point of returning a disabled action: the reason teaches the process."""
    submit = _action("handover.submit")
    ok, reason = _evaluate_action(submit, roles={"Credit Head"}, stage="Diligence",
                                  run_state="none")
    assert not ok
    assert reason == "Available once the handover package has been approved."
    ok, _ = _evaluate_action(submit, roles={"Credit Head"}, stage="CP/CS Completed",
                             run_state="none")
    assert ok

    # And a step whose own screen does not exist yet says exactly that, rather than
    # pretending to be one stage away.
    pending = _action("syndication.allocate", "Syndication")
    ok, reason = _evaluate_action(pending, roles={"Syn Head"}, stage="", run_state="live")
    assert not ok and "not built yet" in reason


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


# --------------------------------------------------------------------------------- #
# The catalogue must match the endpoints it points at
# --------------------------------------------------------------------------------- #
def _openapi() -> dict:
    """The generated specs — the only honest description of what each endpoint accepts."""
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "docs" / "openapi"
    return {"orchestrator": json.loads((root / "orchestrator.openapi.json").read_text()),
            "register": json.loads((root / "register.openapi.json").read_text())}


def _schema_for(spec: dict, specs: dict) -> tuple[set[str], set[str]] | None:
    """(accepted field names, required field names) for an action's endpoint, or None
    when the route takes no body."""
    path = (spec["url"].replace("{workflow_id}", "{workflow_id}")
            .replace("{subject_id}", "{lending_id}"))
    for doc in specs.values():
        op = (doc.get("paths", {}).get(path, {}) or {}).get(spec["method"].lower())
        if op is None:
            continue
        ref = ((op.get("requestBody", {}).get("content", {})
                .get("application/json", {}).get("schema", {}) or {}).get("$ref"))
        if not ref:
            return set(), set()
        model = doc["components"]["schemas"][ref.split("/")[-1]]
        return set(model.get("properties") or {}), set(model.get("required") or [])
    raise AssertionError(f"{spec['key']}: no such route {spec['method']} {path}")


def test_every_action_matches_its_endpoints_schema():
    """The form + prefill + constants must BE a valid body for the endpoint.

    The first version of this catalogue was written from the endpoint names rather than
    their schemas, and got most of the bodies wrong — the first thing a user saw on
    "Send to credit committee" was `requested_by: Field required; amount_cr: Extra inputs
    are not permitted`. A catalogue that a client renders blind has to be checked against
    the thing it describes, not against memory.
    """
    specs = _openapi()
    for subject, actions in _MAKER_ACTIONS.items():
        for spec in actions:
            got = _schema_for(spec, specs)
            assert got is not None, spec["key"]
            accepted, required = got
            if not accepted:
                assert not spec["form"], f"{spec['key']}: body-less route with a form"
                continue

            sent = ({f["name"] for f in spec["form"]}
                    | set(spec.get("prefill") or {})
                    | set(spec.get("constant") or {})
                    | set(_IDENTITY_FOR.get(spec["key"], ())))
            extra = sent - accepted
            assert not extra, f"{spec['key']} ({subject}) sends unknown field(s): {sorted(extra)}"

            # Required fields must be covered by SOMETHING — a form field, a prefilled id,
            # a constant, or the server-injected identity. An action that cannot satisfy
            # them is allowed only if it is gated off pending its own screen.
            missing = required - sent
            if missing:
                # A dedicated screen collects what a flat form cannot (a checklist's
                # items, a package's document set); anything with neither a screen nor a
                # field for a required key would fail on first use.
                assert spec.get("screen") or spec.get("needs_screen"), (
                    f"{spec['key']} ({subject}) cannot satisfy required {sorted(missing)} "
                    "and has no screen to collect it")


def test_identity_is_server_filled_and_never_asked_of_the_user():
    """WHO did this comes from the verified token. A form field for it would be a text box
    asserting an identity — exactly what the approval routes refuse."""
    for actions in _MAKER_ACTIONS.values():
        for spec in actions:
            names = {f["name"] for f in spec["form"]}
            assert not (names & {"requested_by", "by"}), spec["key"]
            assert spec["key"] in _IDENTITY_FOR, f"{spec['key']} missing from _IDENTITY_FOR"


def test_actions_needing_a_screen_are_offered_but_disabled():
    """Honest about what is not built: listed, greyed, and the reason says where it will
    live. Silently omitting them would make the flow look complete when it is not."""
    from app.api import _evaluate_action as ev

    gated = [s for acts in _MAKER_ACTIONS.values() for s in acts if s.get("needs_screen")]
    assert gated, "expected some steps to be pending their own screen"
    for spec in gated:
        ok, reason = ev(spec, roles={"Admin"}, stage=next(iter(spec.get("stages") or {""})),
                        run_state="returned" if spec.get("run") == "returned" else "live")
        assert not ok and "not built yet" in reason, spec["key"]


def test_screen_backed_actions_are_offered_and_name_their_screen():
    """The CP/CS checklist and the handover package have their own screens now, so they
    must be OFFERED (not gated) and must tell the client which screen to open."""
    from app.api import _evaluate_action as ev

    for key, screen in (("cpcs.prepare", "cpcs-checklist"),
                        ("handover.prepare", "handover-package")):
        spec = _action(key)
        assert spec.get("screen") == screen
        assert not spec.get("needs_screen"), f"{key} still gated behind a missing screen"
        ok, reason = ev(spec, roles={"Credit Head"},
                        stage=next(iter(spec["stages"])), run_state="none")
        assert ok, (key, reason)
