"""A mock Register so activities can be tested without a running Register (or Postgres)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from evam_register_client import AsyncRegisterClient

from app import activities


def build_mock() -> FastAPI:
    app = FastAPI()
    app.state.written = []       # every interaction body, for assertions
    app.state.entities = []      # rows the entity search returns
    app.state.leads = {}         # id -> lead row
    app.state.decisions = {}       # workflow_id -> decision row (single-winner)
    app.state.decision_fail = 0    # >0 → the next N decision READS fail with 503 (transient)
    app.state.decision_write_fail = False  # True → decision WRITES fail with 503

    @app.post("/v1/interactions", status_code=201)
    async def interactions(request: Request):
        if request.headers.get("x-api-key") != "test-key":
            return JSONResponse(
                {"error": {"type": "unauthorized", "title": "bad key", "status": 401,
                           "detail": "x", "request_id": None}}, status_code=401)
        body = await request.json()
        app.state.written.append(body)
        row = {"id": uuid.uuid4().hex, "version": 1, "entity_id": body["subject_id"], **body}
        return row

    # -- The dedicated single-winner decision resource ---------------------
    @app.post("/v1/internal/decisions", status_code=201)
    async def record_decision(request: Request):
        if app.state.decision_write_fail:            # simulate a Register outage on write
            return JSONResponse(
                {"error": {"type": "unavailable", "title": "unavailable", "status": 503,
                           "detail": "try again", "request_id": None}}, status_code=503)
        body = await request.json()
        wf, dec = body["workflow_id"], body["decision"]
        existing = app.state.decisions.get(wf)
        if existing is not None:
            if existing["decision"] == dec:
                return existing                       # idempotent replay
            return JSONResponse(                      # opposite decision → 409
                {"error": {"type": "conflict", "title": "conflict", "status": 409,
                           "detail": "different decision exists", "request_id": None}},
                status_code=409)
        # Provenance is server-set in the real Register from the signed context; the mock
        # records a representative approver so downstream assertions have identity + grant.
        row = {"id": uuid.uuid4().hex, "workflow_id": wf, "decision": dec,
               "lead_id": body.get("lead_id"), "decided_by": "head@evamfinance.com",
               "decided_by_id": "u-1", "roles": ["BD Head"],
               "operations": {"push_lead_to_deals": "FULL"}, "views": {},
               "note": body.get("note"),
               "conditions": body.get("conditions"), "valid_days": body.get("valid_days")}
        app.state.decisions[wf] = row
        return row

    @app.get("/v1/internal/decisions/{workflow_id:path}")
    async def get_decision(workflow_id: str):
        if app.state.decision_fail > 0:               # simulate a transient Register outage
            app.state.decision_fail -= 1
            return JSONResponse(
                {"error": {"type": "unavailable", "title": "unavailable", "status": 503,
                           "detail": "try again", "request_id": None}}, status_code=503)
        if workflow_id in app.state.decisions:
            return app.state.decisions[workflow_id]
        return JSONResponse(
            {"error": {"type": "not_found", "title": "missing", "status": 404,
                       "detail": "x", "request_id": None}}, status_code=404)

    @app.get("/v1/entities/{eid}/dossier")
    async def dossier(eid: str):
        return {"entity": {"id": eid}, "counts": {"interactions": 1, "deals": 0}}

    @app.get("/v1/entities")
    async def list_entities():
        # The real Register does trigram search; the mock returns everything and lets
        # the activity's canonical comparison do the matching.
        return {"items": app.state.entities, "next_cursor": None}

    @app.post("/v1/entities", status_code=201)
    async def create_entity(request: Request):
        body = await request.json()
        row = {"id": uuid.uuid4().hex, "version": 1, **body}
        app.state.entities.append(row)
        return row

    @app.get("/v1/leads")
    async def list_leads(request: Request):
        # Honour entity_id/status like the real Register — a mock that ignores
        # filters is how the wrong-company lead bug slipped past the tests.
        rows = list(app.state.leads.values())
        eid = request.query_params.get("entity_id")
        if eid:
            rows = [r for r in rows if str(r.get("entity_id")) == eid]
        status = request.query_params.get("status")
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return {"items": rows, "next_cursor": None}

    @app.post("/v1/leads", status_code=201)
    async def create_lead(request: Request):
        body = await request.json()
        row = {"id": uuid.uuid4().hex, "version": 1, "status": "Active", **body}
        app.state.leads[row["id"]] = row
        return row

    @app.get("/v1/leads/{lid}")
    async def get_lead(lid: str):
        return app.state.leads[lid]

    @app.patch("/v1/leads/{lid}")
    async def patch_lead(lid: str, request: Request):
        body = await request.json()
        app.state.leads[lid] = {**app.state.leads[lid], **body}
        return app.state.leads[lid]

    # -- Deals + governance evidence (business-lifecycle workflows) ---------
    app.state.deals = {}          # id -> deal row
    app.state.evidence = []       # list of evidence rows (append-only, like the real store)
    app.state.committee = {}      # workflow_id -> committee decision (single-winner, verified)

    @app.post("/v1/internal/decisions", status_code=201)
    async def record_committee(request: Request):
        body = await request.json()
        wf = body["workflow_id"]
        # Single-winner: replaying the same outcome returns the original; opposite → 409.
        existing = app.state.committee.get(wf)
        if existing is not None:
            if existing["decision"] == body["decision"]:
                return existing
            return JSONResponse(
                {"error": {"type": "conflict", "title": "conflict", "status": 409,
                           "detail": "different decision", "request_id": None}}, status_code=409)
        row = {"id": uuid.uuid4().hex, "workflow_id": wf, "decision": body["decision"],
               "subject_type": body.get("subject_type"), "subject_id": body.get("subject_id"),
               "run_id": body.get("run_id"), "decided_by": "ch@evamfinance.com",
               "roles": ["Credit Head"],     # committee authority (server-set provenance)
               "committee_reference": body.get("committee_reference"),
               "sanction_letter_reference": body.get("sanction_letter_reference"),
               "note": body.get("note")}
        app.state.committee[wf] = row
        return row

    @app.get("/v1/internal/decisions/{workflow_id:path}")
    async def get_committee_decision(workflow_id: str):
        # The DealStructuring workflow reads the AUTHORITATIVE committee decision from here and
        # derives the outcome/approver/references — never the signal.
        if workflow_id in app.state.committee:
            return app.state.committee[workflow_id]
        return JSONResponse(
            {"error": {"type": "not_found", "title": "missing", "status": 404,
                       "detail": "no committee decision", "request_id": None}}, status_code=404)

    @app.post("/v1/deals", status_code=201)
    async def create_deal(request: Request):
        body = await request.json()
        row = {"id": uuid.uuid4().hex, "version": 1, "stage": body.get("stage", "Data Awaited"),
               **body}
        app.state.deals[row["id"]] = row
        return row

    @app.get("/v1/deals/{did}")
    async def get_deal(did: str):
        return app.state.deals[did]

    @app.patch("/v1/deals/{did}")
    async def patch_deal(did: str, request: Request):
        body = await request.json()
        deal = app.state.deals[did]
        # Enforce the evidence gate the REAL Register enforces: a deal may reach 'Sanctioned' only
        # once both the committee-approval and sanction-letter evidence are on file — so a test
        # proves the WORKFLOW files them first, not just that it PATCHes the stage.
        if body.get("stage") == "Sanctioned" and deal.get("stage") != "Sanctioned":
            kinds = {e["evidence_kind"] for e in app.state.evidence
                     if e["subject_type"] == "Deal" and e["subject_id"] == did}
            missing = {"credit_committee_approval", "sanction_letter"} - kinds
            if missing:
                return JSONResponse(
                    {"error": {"type": "validation", "title": "evidence required", "status": 422,
                               "detail": f"missing evidence: {sorted(missing)}",
                               "request_id": None}}, status_code=422)
        app.state.deals[did] = {**deal, **body}
        return app.state.deals[did]

    @app.get("/v1/evidence")
    async def list_evidence(request: Request):
        st = request.query_params.get("subject_type")
        sid = request.query_params.get("subject_id")
        rows = [e for e in app.state.evidence
                if (st is None or e["subject_type"] == st)
                and (sid is None or e["subject_id"] == sid)]
        return {"items": rows, "next_cursor": None}

    _COMMITTEE_KINDS = {"credit_committee_approval": "Approved",
                        "credit_committee_rejection": "Rejected",
                        "sanction_letter": "Approved"}

    # -- Lending lines + Advaya handoffs (disbursement leg) -----------------
    app.state.lending = {}          # id -> lending row
    app.state.syndication = {}      # id -> syndication row (mandate + lender rows)
    app.state.asset_mon = {}        # id -> asset_monetisation row (mandate + buyer rows)
    app.state.handoffs = {}         # handoff_key -> authoritative handoff record
    app.state.handover_packages = {}  # lending_id -> handover package

    @app.post("/v1/internal/handover-packages", status_code=201)
    async def create_handover_package(request: Request):
        body = await request.json()
        lid = body["lending_id"]
        row = app.state.lending.get(lid)
        if row is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "not found", "status": 404,
                           "detail": "no lending", "request_id": None}}, status_code=404)
        if lid in app.state.handover_packages:
            return app.state.handover_packages[lid]      # idempotent
        # Mirror the Register: must be Ready for Disbursement, with CP/CS + executed evidence.
        if row.get("stage") != "Ready for Disbursement":
            return JSONResponse(
                {"error": {"type": "conflict", "title": "conflict", "status": 409,
                           "detail": f"stage is {row.get('stage')!r}", "request_id": None}},
                status_code=409)
        kinds = {e["evidence_kind"] for e in app.state.evidence
                 if e["subject_type"] == "Lending" and e["subject_id"] == lid}
        missing = {"cp_cs_completion", "executed_agreement"} - kinds
        if missing:
            return JSONResponse(
                {"error": {"type": "validation", "title": "evidence required", "status": 422,
                           "detail": f"missing evidence: {sorted(missing)}", "request_id": None}},
                status_code=422)
        pkg = {"id": uuid.uuid4().hex, "handover_key": f"advaya-handover:{lid}",
               "lending_id": lid, "status": "Prepared",
               "facility_amount": row.get("amount_cr"),
               "proposed_disbursement_amount": row.get("proposed_disbursement_amount"),
               "proposed_disbursement_date": row.get("proposed_disbursement_date"),
               "package_sha256": "f" * 64,
               "package_reference": f"handover/EVAM/{lid}.json", **body}
        app.state.handover_packages[lid] = pkg
        # Prepared only — the maker's draft does NOT advance the stage. A checker must approve.
        return pkg

    @app.post("/v1/internal/handover-packages/{lid}/approve", status_code=200)
    async def approve_handover_package(lid: str):
        pkg = app.state.handover_packages.get(lid)
        if pkg is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "not found", "status": 404,
                           "detail": "no package", "request_id": None}}, status_code=404)
        pkg["status"] = "HandedOver"
        row = app.state.lending.get(lid)
        if row is not None:
            app.state.lending[lid] = {**row, "stage": "Disbursed"}
        return pkg

    app.state.cpcs = {}   # checklist id -> record

    @app.post("/v1/internal/cpcs-checklists", status_code=201)
    async def create_cpcs(request: Request):
        body = await request.json()
        if not body.get("items"):
            return JSONResponse(
                {"error": {"type": "validation", "title": "invalid", "status": 422,
                           "detail": "checklist needs >= 1 item", "request_id": None}},
                status_code=422)
        row = {"id": uuid.uuid4().hex, "status": body.get("status", "Completed"), **body}
        app.state.cpcs[row["id"]] = row
        return row

    @app.get("/v1/lending/{lid}/handover-package")
    async def get_handover_package(lid: str):
        pkg = app.state.handover_packages.get(lid)
        if pkg is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "not found", "status": 404,
                           "detail": "no package", "request_id": None}}, status_code=404)
        return pkg

    @app.post("/v1/internal/advaya-handoffs", status_code=201)
    async def record_handoff(request: Request):
        body = await request.json()
        hk = body["handoff_key"]
        existing = app.state.handoffs.get(hk)
        if existing is not None:
            if (existing["status"] == body["status"]
                    and existing["payload_sha256"] == body["payload_sha256"]):
                return existing
            return JSONResponse(
                {"error": {"type": "conflict", "title": "conflict", "status": 409,
                           "detail": "contradictory handoff", "request_id": None}}, status_code=409)
        row = {"id": uuid.uuid4().hex, **body}
        app.state.handoffs[hk] = row
        return row

    @app.get("/v1/syndication")
    async def list_syndication(request: Request):
        rows = list(app.state.syndication.values())
        did = request.query_params.get("deal_id")
        if did:
            rows = [r for r in rows if str(r.get("deal_id")) == did]
        return {"items": rows, "next_cursor": None}

    @app.get("/v1/syndication/{sid}")
    async def get_syndication(sid: str):
        row = app.state.syndication.get(sid)
        if row is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "missing", "status": 404,
                           "detail": "no syndication", "request_id": None}}, status_code=404)
        return row

    @app.patch("/v1/syndication/{sid}")
    async def patch_syndication(sid: str, request: Request):
        from evam_backend_core import policy as core_policy
        body = await request.json()
        row = app.state.syndication.get(sid)
        if row is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "missing", "status": 404,
                           "detail": "no syndication", "request_id": None}}, status_code=404)
        # Mirror the REAL Register: lifecycle writes go through the shared policy engine
        # (ordered transitions + the syndication_sanction evidence gate).
        if "status" in body and body["status"] != row.get("status"):
            have = {e["evidence_kind"] for e in app.state.evidence
                    if e["subject_type"] == "Syndication" and e["subject_id"] == sid}
            v = core_policy.check_write(
                "Syndication", current=row, changes={"status": body["status"]},
                roles=None, evidence=have)
            if v is not None:
                return JSONResponse(
                    {"error": {"type": "validation_failed", "title": "refused",
                               "status": 422, "detail": v.message, "request_id": None}},
                    status_code=422)
        app.state.syndication[sid] = {**row, **body,
                                      "version": int(row.get("version", 1)) + 1}
        return app.state.syndication[sid]

    @app.get("/v1/asset-monetisation")
    async def list_asset_mon(request: Request):
        rows = list(app.state.asset_mon.values())
        did = request.query_params.get("deal_id")
        if did:
            rows = [r for r in rows if str(r.get("deal_id")) == did]
        return {"items": rows, "next_cursor": None}

    @app.get("/v1/asset-monetisation/{aid}")
    async def get_asset_mon(aid: str):
        row = app.state.asset_mon.get(aid)
        if row is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "missing", "status": 404,
                           "detail": "no asset-monetisation", "request_id": None}},
                status_code=404)
        return row

    @app.patch("/v1/asset-monetisation/{aid}")
    async def patch_asset_mon(aid: str, request: Request):
        from evam_backend_core import policy as core_policy
        body = await request.json()
        row = app.state.asset_mon.get(aid)
        if row is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "missing", "status": 404,
                           "detail": "no asset-monetisation", "request_id": None}},
                status_code=404)
        if "status" in body and body["status"] != row.get("status"):
            have = {e["evidence_kind"] for e in app.state.evidence
                    if e["subject_type"] == "AssetMonetisation" and e["subject_id"] == aid}
            v = core_policy.check_write(
                "AssetMonetisation", current=row, changes={"status": body["status"]},
                roles=None, evidence=have)
            if v is not None:
                return JSONResponse(
                    {"error": {"type": "validation_failed", "title": "refused",
                               "status": 422, "detail": v.message, "request_id": None}},
                    status_code=422)
        app.state.asset_mon[aid] = {**row, **body,
                                    "version": int(row.get("version", 1)) + 1}
        return app.state.asset_mon[aid]

    @app.get("/v1/lending")
    async def list_lending(request: Request):
        deal_id = request.query_params.get("deal_id")
        rows = [r for r in app.state.lending.values()
                if deal_id is None or r.get("deal_id") == deal_id]
        return {"items": rows, "next_cursor": None}

    @app.get("/v1/lending/{lid}")
    async def get_lending(lid: str):
        return app.state.lending[lid]

    @app.patch("/v1/lending/{lid}")
    async def patch_lending(lid: str, request: Request):
        body = await request.json()
        row = app.state.lending[lid]
        # Mirror the Register: 'Sanctioned' requires the committee approval + sanction letter on
        # file for THIS line (the deal-level credit stage is deprecated — the gate keys on Lending).
        if body.get("stage") == "Sanctioned" and row.get("stage") != "Sanctioned":
            kinds = {e["evidence_kind"] for e in app.state.evidence
                     if e["subject_type"] == "Lending" and e["subject_id"] == lid}
            missing = {"credit_committee_approval", "sanction_letter"} - kinds
            if missing:
                return JSONResponse(
                    {"error": {"type": "validation", "title": "evidence required", "status": 422,
                               "detail": f"missing evidence: {sorted(missing)}",
                               "request_id": None}}, status_code=422)
        # Mirror the Register: 'CP/CS Completed' requires CP/CS + executed agreement.
        if body.get("stage") == "CP/CS Completed" and row.get("stage") != "CP/CS Completed":
            kinds = {e["evidence_kind"] for e in app.state.evidence
                     if e["subject_type"] == "Lending" and e["subject_id"] == lid}
            missing = {"cp_cs_completion", "executed_agreement"} - kinds
            if missing:
                return JSONResponse(
                    {"error": {"type": "validation", "title": "evidence required", "status": 422,
                               "detail": f"missing evidence: {sorted(missing)}",
                               "request_id": None}}, status_code=422)
        app.state.lending[lid] = {**row, **body}
        return app.state.lending[lid]

    # -- Increment 7: notifications / calendar / document expiry ------------
    app.state.notifications = {}     # dedupe_key (or id) -> notification row
    app.state.calendar = {}          # id -> calendar event row
    app.state.expiry_report = {"expired": [], "expiring": []}  # next sweep's payload
    app.state.expiry_sweeps = 0

    @app.post("/v1/internal/notifications", status_code=201)
    async def create_notification(request: Request):
        # Mirror the real Register: idempotent by dedupe_key (a replay returns the
        # ORIGINAL row) + one delivery row per requested external channel.
        body = await request.json()
        key = body.get("dedupe_key")
        if key and key in app.state.notifications:
            return app.state.notifications[key]
        row = {"id": uuid.uuid4().hex, "read_at": None,
               "deliveries": [{"id": uuid.uuid4().hex, "channel": ch,
                               "status": "pending"}
                              for ch in body.get("channels", [])],
               **body}
        app.state.notifications[key or row["id"]] = row
        return row

    @app.get("/v1/calendar-events")
    async def list_calendar_events(request: Request):
        rows = list(app.state.calendar.values())
        wf = request.query_params.get("workflow_id")
        if wf:
            rows = [r for r in rows if r.get("workflow_id") == wf]
        return {"items": rows, "next_cursor": None}

    @app.post("/v1/calendar-events", status_code=201)
    async def create_calendar_event(request: Request):
        body = await request.json()
        row = {"id": uuid.uuid4().hex, "status": "Scheduled", "version": 1, **body}
        app.state.calendar[row["id"]] = row
        return row

    @app.post("/v1/internal/documents/expiry-sweep")
    async def expiry_sweep(request: Request):
        # Mirror the real sweep's idempotency: a document is reported as NEWLY expired
        # exactly once (it is 'Expired' afterwards and drops out of later sweeps).
        await request.json()
        app.state.expiry_sweeps += 1
        report = {"swept_on": "2026-08-01",
                  "expired": list(app.state.expiry_report["expired"]),
                  "expiring": list(app.state.expiry_report["expiring"])}
        app.state.expiry_report["expired"] = []
        return report

    # -- Increment 8: covenants + EWS ---------------------------------------
    app.state.covenant_report = {"generated": 0, "overdue": [],
                                 "waivers_expired": []}   # next sweep's payload
    app.state.covenant_sweeps = 0
    app.state.ews = {}               # case id -> case row

    @app.post("/v1/internal/covenants/run-sweep")
    async def covenant_sweep(request: Request):
        # Mirror the real sweep's report-once semantics: an overdue flip / waiver expiry
        # is returned exactly once (the status change drops it from later sweeps).
        await request.json()
        app.state.covenant_sweeps += 1
        report = {"swept_on": "2026-08-01",
                  "generated": app.state.covenant_report["generated"],
                  "overdue": list(app.state.covenant_report["overdue"]),
                  "waivers_expired": list(app.state.covenant_report["waivers_expired"])}
        app.state.covenant_report.update(generated=0, overdue=[], waivers_expired=[])
        return report

    @app.get("/v1/ews-cases/{cid}")
    async def get_ews_case(cid: str):
        row = app.state.ews.get(cid)
        if row is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "missing", "status": 404,
                           "detail": "no case", "request_id": None}}, status_code=404)
        return row

    @app.post("/v1/internal/ews-cases/{cid}/auto-escalate")
    async def auto_escalate_ews(cid: str, request: Request):
        body = await request.json()
        row = app.state.ews.get(cid)
        if row is None:
            return JSONResponse(
                {"error": {"type": "not_found", "title": "missing", "status": 404,
                           "detail": "no case", "request_id": None}}, status_code=404)
        # Mirror the real route: closed/escalated cases come back unchanged (idempotent).
        if row["status"] not in ("Closed", "Escalated"):
            row = {**row, "status": "Escalated", "escalated_by": "system:sla",
                   "escalation_note": body.get("reason")}
            app.state.ews[cid] = row
        return row

    @app.post("/v1/evidence", status_code=201)
    async def attach_evidence(request: Request):
        body = await request.json()
        kind = body["evidence_kind"]
        # Mirror the real Register: committee/sanction evidence is VERIFIED against a durable
        # committee decision; the Advaya ack against an Accepted handoff (matching digest).
        want = _COMMITTEE_KINDS.get(kind)
        if want is not None:
            dec = app.state.committee.get(body.get("decision_ref"))
            if (dec is None or dec["decision"] != want
                    or dec.get("subject_id") != body.get("subject_id")):
                return JSONResponse(
                    {"error": {"type": "validation", "title": "unverified provenance",
                               "status": 422, "detail": "evidence not backed by a committee "
                               "decision for this subject", "request_id": None}}, status_code=422)
        if kind == "advaya_acknowledgement":
            h = app.state.handoffs.get(body.get("decision_ref"))
            if (h is None or h["status"] != "Accepted"
                    or h["lending_id"] != body.get("subject_id")
                    or h["payload_sha256"] != body.get("sha256")):
                return JSONResponse(
                    {"error": {"type": "validation", "title": "unverified ack", "status": 422,
                               "detail": "ack not backed by an accepted handoff",
                               "request_id": None}}, status_code=422)
        row = {"id": uuid.uuid4().hex, **body}
        app.state.evidence.append(row)
        return row

    return app


@pytest.fixture
def mock_register(monkeypatch) -> FastAPI:
    app = build_mock()

    def _client(caller=None) -> AsyncRegisterClient:  # noqa: ANN001 - mirrors activities._client
        # Honour the caller's tenant when present (the tenant-propagation contract), else
        # the default — so tests can assert the workflow ran in the right tenant.
        tenant = caller.tenant if (caller is not None and getattr(caller, "tenant", "")) else "EVAM"
        return AsyncRegisterClient("http://reg", "test-key", tenant=tenant, actor="workflows",
                                   transport=httpx.ASGITransport(app=app))

    monkeypatch.setattr(activities, "_client", _client)
    return app
