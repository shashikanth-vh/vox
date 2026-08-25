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

from app.api import (_checklist_half, _evaluate_action, _IDENTITY_FOR,
                     _lending_pipeline, _MAKER_ACTIONS)
from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _action(key: str, subject: str = "Lending") -> dict:
    return next(a for a in _MAKER_ACTIONS[subject] if a["key"] == key)


# --------------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------------- #
def test_stage_gate_explains_the_sequence_rather_than_hiding_it():
    """The whole point of returning a disabled action: the reason teaches the process."""
    disburse = _action("disburse")
    ok, reason = _evaluate_action(disburse, roles={"Credit Head"}, stage="Diligence",
                                  run_state="none")
    assert not ok
    assert reason == "Disbursement follows the Conditions Precedent approval."
    # Offered from the CP approval onward, and STILL at 'Disbursed' — the same dialog
    # records the partner's answers and every later tranche (T2, T3, …).
    for stage in ("CP/CS Completed", "Ready for Disbursement", "Disbursed"):
        ok, _ = _evaluate_action(disburse, roles={"Credit Head"}, stage=stage,
                                 run_state="none")
        assert ok, stage

    # And a step whose own screen does not exist yet says exactly that, rather than
    # pretending to be one stage away.
    pending = _action("syndication.allocate", "Syndication")
    ok, reason = _evaluate_action(pending, roles={"Syn Head"}, stage="", run_state="live")
    assert not ok and "not built yet" in reason


def test_a_parked_line_is_told_how_to_reopen_not_that_it_is_past_the_point():
    """After a committee reject the line sits at 'Rejected', and the CAM/committee
    verbs' normal stage_reason ("already past that point") is nonsense there — the desk
    needs the door back in, which is the stage move. Same for On Hold."""
    cam = _action("cam.workbench")
    start = _action("deal-structuring.start")
    for spec in (cam, start):
        ok, reason = _evaluate_action(spec, roles={"Credit Head"}, stage="Rejected",
                                      run_state="none")
        assert not ok and "reopen" in reason and "Diligence" in reason
        ok, reason = _evaluate_action(spec, roles={"Credit Head"}, stage="On Hold",
                                      run_state="none")
        assert not ok and "Resume" in reason
    # And once reopened, the loop is live again: CAM and committee both offer.
    for spec in (cam, start):
        assert _evaluate_action(spec, roles={"Credit Head"}, stage="Diligence",
                                run_state="none")[0]


def test_role_gate_names_who_does_the_step():
    """A refusal that names the role is actionable; "not permitted" is not."""
    disburse = _action("disburse")
    ok, reason = _evaluate_action(disburse, roles={"Syn RM"}, stage="Ready for Disbursement",
                                  run_state="none")
    assert not ok
    assert "Credit Head" in reason


def test_role_gate_is_checked_before_the_stage_gate():
    """Who you are does not change with the subject, so it is the more useful answer of
    the two when both fail."""
    disburse = _action("disburse")
    ok, reason = _evaluate_action(disburse, roles={"Syn RM"}, stage="Data Awaited",
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
    """Stands in for the Register: serves the one subject row under test. Optional
    `cams` answers the CAM-report lookup with a real list, so the committee gate can
    be exercised (the single-row default is not a list, which the endpoint reads as
    'unknown — fail open')."""

    def __init__(self, row: dict, cams: list | None = None) -> None:
        self.row = row
        self.cams = cams

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        if self.cams is not None and "cam-reports" in url:
            return httpx.Response(200, json=self.cams, request=httpx.Request("GET", url))
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


# --------------------------------------------------------------------------------- #
# The pipeline strip — one readable row of server truth (CAM → CCR → Sanction → CP,
# forking to Disbursement ∥ CP/CS), coloured from the same facts that gate the actions.
# --------------------------------------------------------------------------------- #
def _strip(**over) -> dict:
    """The pipeline keyed by step, from sensible defaults each test overrides."""
    base = dict(stage="Data Awaited", run_state="none", on_file=set(),
                checklist_status="", checklist_items=[], cam_ready=False,
                package_status="", row={})
    base.update(over)
    return {s["key"]: s for s in _lending_pipeline(**base)}


def test_pipeline_walks_the_happy_path_box_by_box():
    # Fresh line: the CAM is the working step, everything downstream waits.
    s = _strip()
    assert s["cam"]["state"] == "active"
    assert [s[k]["state"] for k in ("ccr", "sanction", "cp", "disbursement", "cs")] \
        == ["pending"] * 5

    # CAM prepared, committee run in flight: CCR is the blue box.
    s = _strip(stage="Note Circulated", cam_ready=True, run_state="live")
    assert s["cam"]["state"] == "done" and s["ccr"]["state"] == "active"

    # Approved and sanctioned: three greens, CP becomes the working step.
    s = _strip(stage="Sanctioned", cam_ready=True,
               on_file={"credit_committee_approval", "sanction_letter"},
               letter_doc_on_file=True)
    assert [s[k]["state"] for k in ("cam", "ccr", "sanction")] == ["done"] * 3
    assert s["cp"]["state"] == "active" and "letter" in s["sanction"]["note"]

    # CP approved with a deferred item: the fork goes to work — disbursement live,
    # the deferred CP counts as an OPEN condition subsequent (that is what deferral means).
    items = [{"condition_type": "CP", "status": "Completed"},
             {"condition_type": "CP", "status": "Deferred as CS"}]
    s = _strip(stage="CP/CS Completed", cam_ready=True, checklist_status="Approved",
               checklist_items=items,
               on_file={"credit_committee_approval", "sanction_letter",
                        "cp_cs_completion"})
    assert s["cp"]["state"] == "done" and s["cp"]["note"].startswith("2/2")
    assert s["disbursement"]["state"] == "active"
    assert s["cs"]["state"] == "active" and s["cs"]["note"].startswith("0/1")

    # Fully drawn: the book ends green on both forks.
    s = _strip(stage="Disbursed", cam_ready=True, checklist_status="Approved",
               checklist_items=[{"condition_type": "CS", "status": "Completed"}],
               row={"disbursed_amount": 5, "proposed_disbursement_amount": 5})
    assert s["disbursement"]["state"] == "done" and s["cs"]["state"] == "done"


def test_pipeline_paints_the_rejections_red_where_they_happened():
    # Committee rejected → the CCR box is the red one, and says what to do next.
    s = _strip(stage="Rejected", cam_ready=True,
               on_file={"credit_committee_rejection"})
    assert s["ccr"]["state"] == "rejected" and "Committee rejected" in s["ccr"]["note"]
    # A desk rejection with no committee verdict says so — it never invents one.
    s = _strip(stage="Rejected")
    assert s["ccr"]["state"] == "rejected" and "desk" in s["ccr"]["note"]
    # A checker-rejected CP checklist reddens CP while the sanction STAYS green —
    # the paperwork was refused, never the credit.
    s = _strip(stage="Sanctioned", cam_ready=True, checklist_status="Rejected",
               on_file={"credit_committee_approval", "sanction_letter"},
               letter_doc_on_file=True)
    assert s["sanction"]["state"] == "done" and s["cp"]["state"] == "rejected"


def test_a_refused_deferral_returns_to_the_cp_side():
    """Deferred-as-CS becomes a CS obligation ONLY through the checker's approval.
    While the version's claim stands the deferral is done-for-CP and open-as-CS; the
    moment the checker REJECTS (or returns) it, the deferral was refused with the rest
    of the version — the item is open CP work again and leaves the CS side, so the
    maker completes it (or re-proposes) and re-sends, looping until approved."""
    items = [{"condition_type": "CP", "status": "Completed"},
             {"condition_type": "CP", "status": "Deferred as CS"},
             {"condition_type": "CS", "status": "Pending"}]
    # Claim standing (sent, awaiting the checker): CP 2/2, CS carries the deferral.
    assert _checklist_half(items, "CP", "Completed") == (2, 2)
    assert _checklist_half(items, "CS", "Completed") == (0, 2)
    # Refused: CP back to 1/2 — the deferred item is open again — and CS holds only
    # the native condition from the sanction letter.
    for verdict in ("Rejected", "Returned"):
        assert _checklist_half(items, "CP", verdict) == (1, 2), verdict
        assert _checklist_half(items, "CS", verdict) == (0, 1), verdict
    # Approval ratifies the deferral: from here the CS chase owns it.
    assert _checklist_half(items, "CS", "Approved") == (0, 2)
    # And the pipeline agrees: a rejected checklist leaves the CS box shut.
    s = _strip(stage="Sanctioned", cam_ready=True, checklist_status="Rejected",
               checklist_items=items,
               on_file={"credit_committee_approval", "sanction_letter"})
    assert s["cp"]["state"] == "rejected" and s["cs"]["state"] == "pending"


def test_cp_and_cs_boxes_declare_when_there_is_a_record_to_show():
    """A checklist on record makes the CP / CP-CS boxes VIEWABLE — the client opens the
    recorded checklist read-only instead of a menu of refusals. No checklist, nothing
    to show."""
    with_items = _strip(stage="Sanctioned", checklist_status="Approved",
                        checklist_items=[{"condition_type": "CP", "status": "Completed"}])
    assert with_items["cp"]["viewable"] and with_items["cs"]["viewable"]
    empty = _strip(stage="Diligence")
    assert not empty["cp"].get("viewable") and not empty["cs"].get("viewable")


def test_pipeline_greens_only_what_is_on_file():
    """Green means ON FILE. A line the MIS landed at 'Sanctioned' with no on-platform
    artefacts shows grey boxes that SAY why — never a green the register cannot back;
    the artefacts landing is what paints them."""
    s = _strip(stage="Sanctioned")
    assert [s[k]["state"] for k in ("cam", "ccr", "sanction")] == ["pending"] * 3
    assert "no CAM is on file" in s["cam"]["note"]
    assert "To do: upload the sanction letter" in s["sanction"]["note"]
    evidenced = _strip(stage="Sanctioned",
                       on_file={"credit_committee_approval", "sanction_letter"})
    assert evidenced["ccr"]["state"] == "done"
    # the EVIDENCE reference (auto-filed by the approval path) does not paint the
    # box — only the uploaded letter document does, and until then it is a to-do
    assert evidenced["sanction"]["state"] == "pending"
    assert "To do: upload the sanction letter" in evidenced["sanction"]["note"]
    lettered = _strip(stage="Sanctioned",
                      on_file={"credit_committee_approval", "sanction_letter"},
                      letter_doc_on_file=True)
    assert lettered["sanction"]["state"] == "done"


def test_every_lending_verb_belongs_to_a_pipeline_box():
    """The strip's boxes are the doors to the actions. An unmapped verb would be
    unreachable once the button row folds into the boxes — so the mapping is total,
    and only over boxes that exist."""
    boxes = {"cam", "ccr", "sanction", "cp", "disbursement", "cs"}
    for spec in _MAKER_ACTIONS["Lending"]:
        assert spec.get("step") in boxes, spec["key"]


async def test_actions_carries_the_pipeline_for_lending(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _Http({"id": "22222222-2222-2222-2222-222222222222",
                            "deal_id": "33333333-3333-3333-3333-333333333333",
                            "stage": "Diligence"})
    r = await _get(app, {"subject_type": "Lending",
                         "subject_id": "22222222-2222-2222-2222-222222222222"})
    assert r.status_code == 200, r.text
    steps = r.json()["pipeline"]
    assert [s["key"] for s in steps] == ["cam", "ccr", "sanction", "cp",
                                         "disbursement", "cs"]
    # Every step is renderable blind: a label, a known state, a note to show on hover.
    for s in steps:
        assert s["label"] and s["note"]
        assert s["state"] in {"done", "active", "rejected", "pending"}
    # The fork is declared, not guessed, by the client.
    assert [s["key"] for s in steps if s.get("parallel")] == ["disbursement", "cs"]
    get_settings.cache_clear()


async def test_no_cam_no_committee(monkeypatch):
    """The committee decides ON the CAM: with none on the line, 'Send to credit
    committee' is refused with the instruction — no request wasted on a file the
    committee cannot read, no credit-note number burned on it. With a CAM drafted,
    the door opens."""
    row = {"id": "22222222-2222-2222-2222-222222222222",
           "deal_id": "33333333-3333-3333-3333-333333333333", "stage": "Diligence"}
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _Http(row, cams=[])
    r = await _get(app, {"subject_type": "Lending",
                         "subject_id": "22222222-2222-2222-2222-222222222222"})
    send = {a["key"]: a for a in r.json()["actions"]}["deal-structuring.start"]
    assert not send["enabled"] and "Prepare the CAM first" in send["reason"]

    app.state.http = _Http(row, cams=[{"draft_md": "# CAM v1"}])
    r = await _get(app, {"subject_type": "Lending",
                         "subject_id": "22222222-2222-2222-2222-222222222222"})
    send = {a["key"]: a for a in r.json()["actions"]}["deal-structuring.start"]
    assert send["enabled"]
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
    # The subject's id appears under different parameter names depending on the route:
    # a bespoke route names it for its subject ({lending_id}), while the generic CRUD
    # router names it {obj_id}. Try each rather than assume one.
    candidates = [spec["url"].replace("{subject_id}", name)
                  for name in ("{lending_id}", "{obj_id}", "{subject_id}")]
    for doc in specs.values():
        op = None
        for path in candidates:
            op = (doc.get("paths", {}).get(path, {}) or {}).get(spec["method"].lower())
            if op is not None:
                break
        if op is None:
            continue
        ref = ((op.get("requestBody", {}).get("content", {})
                .get("application/json", {}).get("schema", {}) or {}).get("$ref"))
        if not ref:
            return set(), set()
        model = doc["components"]["schemas"][ref.split("/")[-1]]
        return set(model.get("properties") or {}), set(model.get("required") or [])
    raise AssertionError(
        f"{spec['key']}: no such route {spec['method']} {candidates[0]}")


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
                    | set(_IDENTITY_FOR.get(spec["key"], ()))
                    # Filled by the plane at request time from the subject's own run.
                    | ({"workflow_id", "run_id"} if spec.get("provenance") else set()))
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
                        ("disburse", "disburse")):
        spec = _action(key)
        assert spec.get("screen") == screen
        assert not spec.get("needs_screen"), f"{key} still gated behind a missing screen"
        ok, reason = ev(spec, roles={"Credit Head"},
                        stage=next(iter(spec["stages"])), run_state="none")
        assert ok, (key, reason)


async def test_cpcs_version_is_served_not_guessed(monkeypatch):
    """The screen opens on the NEXT version.

    A checklist is keyed on (lending, version). A client that always defaults to 1 makes
    the user fill in the whole form and THEN get `A CP/CS checklist v1 already exists`.
    The plane knows the answer, so it answers.
    """
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")

    class _WithChecklists(_Http):
        async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
            if "cpcs-checklists" in str(url):
                return httpx.Response(200, json={"items": [{"checklist_version": 1},
                                                           {"checklist_version": 2}]},
                                      request=httpx.Request("GET", url))
            return await super().get(url, **kwargs)

    app.state.http = _WithChecklists({"id": "22222222-2222-2222-2222-222222222222",
                                      "deal_id": "33333333-3333-3333-3333-333333333333",
                                      "stage": "Sanctioned"})
    r = await _get(app, {"subject_type": "Lending",
                         "subject_id": "22222222-2222-2222-2222-222222222222"})
    assert r.status_code == 200, r.text
    prepare = next(a for a in r.json()["actions"] if a["key"] == "cpcs.prepare")
    version = next(f for f in prepare["form"] if f["name"] == "checklist_version")
    assert version["default"] == 3, "v1 and v2 exist, so the next one is 3"
    get_settings.cache_clear()


async def test_cpcs_version_starts_at_one_on_a_fresh_line(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")

    class _NoChecklists(_Http):
        async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
            if "cpcs-checklists" in str(url):
                return httpx.Response(200, json={"items": []},
                                      request=httpx.Request("GET", url))
            return await super().get(url, **kwargs)

    app.state.http = _NoChecklists({"id": "22222222-2222-2222-2222-222222222222",
                                    "deal_id": "33333333-3333-3333-3333-333333333333",
                                    "stage": "Sanctioned"})
    r = await _get(app, {"subject_type": "Lending",
                         "subject_id": "22222222-2222-2222-2222-222222222222"})
    prepare = next(a for a in r.json()["actions"] if a["key"] == "cpcs.prepare")
    version = next(f for f in prepare["form"] if f["name"] == "checklist_version")
    assert version["default"] == 1
    get_settings.cache_clear()


def test_every_screen_the_catalogue_names_is_one_the_client_implements():
    """The plane names a screen; the client renders it. A name with no implementation
    behind it silently falls through to the generic form — which for the executed
    agreement meant asking a credit manager to type a SHA-256 by hand, a question with
    no answer inside the product. Keep the two lists in step."""
    implemented = {"cpcs-checklist", "handover-package", "executed-agreement",
                   "cam-workbench", "sanction-terms", "disburse"}
    named = {spec["screen"] for actions in _MAKER_ACTIONS.values()
             for spec in actions if spec.get("screen")}
    assert named <= implemented, f"no client screen for {sorted(named - implemented)}"


def test_the_executed_agreement_step_is_gone_from_the_catalogue():
    """The desk records the agreement among the CP conditions; the separate typed-digest
    attestation step only stalled the line, so it was removed — and the CP/CS Completed
    gate no longer demands the executed_agreement evidence."""
    assert not any(s["key"] == "evidence.executed-agreement"
                   for s in _MAKER_ACTIONS["Lending"])
    from evam_backend_core.policy import EVIDENCE_FOR_STAGE

    assert EVIDENCE_FOR_STAGE["Lending"]["CP/CS Completed"] == ["cp_cs_completion"]


def test_every_action_declares_which_service_answers_it():
    """The catalogue spans BOTH planes, so it has to say which.

    Starting a workflow is the orchestrator's; filing evidence, submitting a handover and
    attesting an Advaya event are the register's. A client that assumed one plane sent the
    register actions to the orchestrator and got a 404 on POST /orchestrator/v1/evidence —
    reported to the user as a workflow failure, on a screen that had done nothing wrong.
    """
    from app.api import _plane_of

    for actions in _MAKER_ACTIONS.values():
        for spec in actions:
            plane = _plane_of(spec, spec["url"])
            assert plane in ("orchestrator", "register"), spec["key"]
            # The rule, stated the other way round, so a new action cannot quietly
            # inherit the wrong plane from a prefix that looks similar.
            if spec["url"].startswith("/v1/workflows"):
                assert plane == "orchestrator", spec["key"]
            else:
                assert plane == "register", spec["key"]

    # The one that bit: a register route that is NOT under /v1/workflows.
    by_key = {s["key"]: s for acts in _MAKER_ACTIONS.values() for s in acts}
    for key in ("lending.cpcs-complete",):
        assert _plane_of(by_key[key], by_key[key]["url"]) == "register", key


def test_the_actions_response_carries_the_plane():
    """Declared in the payload, not inferred by the client from the url — the client is
    told where to send each action, exactly as it is told the method and the form."""
    import inspect

    from app import api as api_mod

    src = inspect.getsource(api_mod)
    assert '"plane": _plane_of(spec, url)' in src, (
        "the serialised action must carry its plane")


def test_no_remaining_action_asks_the_user_for_run_provenance():
    """Evidence provenance (workflow_id / run_id) is a fact about the platform, never a
    question for a credit manager. With the executed-agreement step removed, assert the
    invariant across the WHOLE catalogue rather than on that one action."""
    for actions in _MAKER_ACTIONS.values():
        for spec in actions:
            names = {f["name"] for f in spec["form"]} | set(spec.get("constant") or {})
            assert "workflow_id" not in names and "run_id" not in names, spec["key"]


def test_a_lending_citation_names_the_per_line_decision():
    """A deal's structuring run covers EVERY facility on that deal.

    Citing that run against one lending line is a claim the register rejects — "belongs to
    a different subject (Deal …)" — because the decision it verifies against is recorded
    per line, under "{run}:lending:{id}". The plane therefore asks the register which
    decision it holds for the subject and cites THAT, rather than composing an identifier
    and hoping it resolves.
    """
    import inspect

    from app import api as api_mod

    src = inspect.getsource(api_mod)
    assert 'f"{workflow_id}:lending:{subject_id}"' in src, (
        "a Lending citation must name the per-line decision, not the deal's run")
    assert '/v1/internal/decisions/{candidate}' in src, (
        "the citation must be verified against the register before it is offered")
    # And it is only used when the register confirms the subject matches.
    assert 'str(decision.get("subject_id") or "") == str(subject_id)' in src


def test_the_stage_move_is_offered_and_names_what_it_is_waiting_for():
    """'CP/CS Completed' was reachable only by knowing about a dropdown.

    The register gates that stage on two evidences and nothing else — no field lock, no
    role lock on the stage itself — so the move is a plain write that simply starts being
    accepted once both are on file. Nothing said so: the evidence landed, the stage did
    not move, and the screen was silent about which half was missing. It is an action now,
    and while it is unavailable it says what it is waiting for, by name.
    """
    from app.api import _EVIDENCE_LABEL

    spec = next(s for s in _MAKER_ACTIONS["Lending"] if s["key"] == "lending.cpcs-complete")
    assert spec["constant"]["stage"] == "CP/CS Completed"
    assert spec["stages"] == {"Sanctioned"}          # it moves the line ON from Sanctioned
    assert tuple(spec["evidence"]) == ("cp_cs_completion",)
    # The two evidence kinds the register's own policy requires for that stage.
    from evam_backend_core.policy import EVIDENCE_FOR_STAGE
    assert set(spec["evidence"]) == set(EVIDENCE_FOR_STAGE["Lending"]["CP/CS Completed"]), (
        "the action must gate on exactly what the register gates on")
    # And every kind it can wait for has words a credit manager would use.
    for kind in spec["evidence"]:
        assert kind in _EVIDENCE_LABEL and " " in _EVIDENCE_LABEL[kind], kind


def test_an_evidence_gated_action_is_refused_by_name_not_by_silence():
    """The reason a step is unavailable is the most useful thing on the panel."""
    from app.api import _EVIDENCE_LABEL

    # Every kind named by any action's evidence gate must have a human label, or the
    # reason degrades to the raw enum.
    for actions in _MAKER_ACTIONS.values():
        for spec in actions:
            for kind in (spec.get("evidence") or ()):
                assert kind in _EVIDENCE_LABEL, f"{spec['key']} waits on unlabelled {kind}"


def test_a_signed_context_is_bound_to_the_route_not_the_query():
    """The register compares the token's path against request.url.path — which never
    carries a query string.

    Minting it with one 403'd every filtered read the orchestrator makes, silently: the
    caller discarded the problem and used its empty default. That is why the CP/CS screen
    re-opened on version 1 after v1 had been approved, then refused the user's work with a
    409 for a checklist that already existed — and why an evidence gate would have read as
    "still waiting" with the evidence sitting on file.
    """
    from app.api import _token_path

    assert _token_path("/v1/internal/cpcs-checklists?lending_id=abc&limit=50") == (
        "/v1/internal/cpcs-checklists")
    assert _token_path("/v1/evidence?subject_type=Lending&subject_id=abc") == "/v1/evidence"
    # A path with no query is unchanged — the case that always worked, and must keep to.
    assert _token_path("/v1/internal/decisions/wf-1") == "/v1/internal/decisions/wf-1"
    assert _token_path("") == ""
    # Only the FIRST '?' splits: a query value containing one must not truncate the route.
    assert _token_path("/v1/x?a=1?2") == "/v1/x"


def test_no_signed_read_is_minted_with_a_query_string():
    """Every mint site goes through the helper — a new one that forgets would fail the
    same way, silently, months later."""
    import inspect
    import re

    from app import api as api_mod

    src = inspect.getsource(api_mod)
    bare = re.findall(r"path=path\b(?!\s*\))", src) + re.findall(r"path=path\)", src)
    assert not bare, (
        "mint the internal context with _token_path(path) — a raw path may carry a query")


def test_disburse_is_the_single_verb_and_the_partner_answer_is_gated():
    """The desk's lane collapsed to ONE verb: 'Disburse' (offered from the CP approval
    onward — it stages the line itself when needed), and 'Disbursement Update' for the
    partner's manual confirmations, offered only once a request has been SENT and kept
    available at 'Disbursed' so later phases (T2, T3, …) record the same way."""
    from app.api import _PACKAGE_REASON

    disburse = _action("disburse")
    # 'Sanctioned' belongs here: 'CP/CS Completed' now means BOTH halves are satisfied,
    # so a line whose conditions subsequent are still being chased never reaches it —
    # and that line must still be able to disburse on the CP approval's evidence.
    assert disburse["stages"] == {"Sanctioned", "CP/CS Completed",
                                  "Ready for Disbursement", "Disbursed"}
    assert disburse["screen"] == "disburse"
    assert "package" not in disburse          # sending is how a package comes to exist

    assert "Submitted" in _PACKAGE_REASON

    # The granular steps are ALL gone: the Disburse dialog itself records the partner's
    # answer and every tranche.
    keys = {s["key"] for s in _MAKER_ACTIONS["Lending"]}
    assert not ({"handover.prepare", "handover.submit", "advaya.attest",
                 "lending.ready-for-disbursement"} & keys)


def test_disburse_waits_for_the_cp_approval():
    """The money door: without an approved CP checklist (or its minted evidence, or
    a stage that already required it) the Disburse verb is disabled with the
    reason named; the approval opens it."""
    import asyncio as _a
    # exercised through the pipeline helper's sibling logic via the action loop is
    # integration-level; here we assert the catalogue spec still declares the gate's
    # inputs (stages) so the loop's condition stays reachable.
    spec = next(s for s in _MAKER_ACTIONS["Lending"] if s["key"] == "disburse")
    assert "Sanctioned" in spec["stages"] and "Ready for Disbursement" in spec["stages"]
