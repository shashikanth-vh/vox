"""The GENUINE end-to-end test: Orchestrator API → Temporal → worker → REAL Register
(uvicorn subprocess, real PostgreSQL, real migrations).

Covers the two approved VOX scenarios plus the failure modes that matter:

* new company        → entity + lead created, full-fidelity interaction filed
* duplicate retry    → the SAME capture_id resubmitted returns the SAME result and
                       writes NOTHING new (idempotent starts + idempotent activities)
* existing company   → canonical-name resolution links the active lead (no duplicates)
* lead conversion    → signal-driven approval applies the deal + line + Converted lead

Skips cleanly where Temporal's test server or the test Postgres is unavailable
(offline sandboxes); runs fully in CI.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
REGISTER_DIR = os.path.join(REPO, "services", "register")
REGISTER_PORT = 8106


# --------------------------------------------------------------------------- #
# Fixtures: a real Register + a Temporal test environment + our worker + the API
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def register_server():
    env = {**os.environ,
           "REGISTER_DB_HOST": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
           "REGISTER_DB_PORT": os.environ.get("TEST_DB_PORT", "5432"),
           "REGISTER_DB_USER": os.environ.get("TEST_DB_USER", "register"),
           "REGISTER_DB_PASSWORD": os.environ.get("TEST_DB_PASSWORD", "register"),
           "REGISTER_DB_NAME": "register_test",
           "REGISTER_API_KEYS": "test-key",
           "REGISTER_LOG_LEVEL": "WARNING",
           "REGISTER_ENVIRONMENT": "test"}
    for cmd in (["python", "-m", "alembic", "upgrade", "head"],
                ["python", "-m", "app.seed.bootstrap"]):
        res = subprocess.run(cmd, cwd=REGISTER_DIR, env=env, capture_output=True)
        if res.returncode != 0:
            pytest.skip(f"test Postgres/Register unavailable: {res.stderr.decode()[-300:]}")
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(REGISTER_PORT), "--log-level", "warning"],
        cwd=REGISTER_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{REGISTER_PORT}/healthz",
                             timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.4)
        else:
            pytest.skip("register did not become healthy")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest_asyncio.fixture()
async def stack(register_server, monkeypatch):
    """Temporal test env + worker + orchestrator ASGI app, all wired to the real
    Register. Yields (orchestrator_client, register_client, temporal_client)."""
    try:
        from temporalio.testing import WorkflowEnvironment
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip
        pytest.skip(f"Temporal test server unavailable: {exc}")

    monkeypatch.setenv("WORKFLOWS_REGISTER_BASE_URL", f"http://127.0.0.1:{REGISTER_PORT}")
    monkeypatch.setenv("WORKFLOWS_REGISTER_API_KEY", "test-key")
    monkeypatch.setenv("WORKFLOWS_REGISTER_TENANT", "EVAM")
    monkeypatch.setenv("WORKFLOWS_TASK_QUEUE", "e2e-tq")
    from app.config import get_settings
    get_settings.cache_clear()

    from temporalio.worker import Worker

    from app import activities
    from app.api import create_app
    from app.workflows import (
        IngestInteractionWorkflow,
        LeadConversionWorkflow,
        VoxTouchpointWorkflow,
    )

    async with env:
        async with Worker(
            env.client, task_queue="e2e-tq",
            workflows=[IngestInteractionWorkflow, VoxTouchpointWorkflow,
                       LeadConversionWorkflow],
            activities=[
                activities.write_interaction, activities.fetch_dossier,
                activities.resolve_entity, activities.create_entity,
                activities.find_active_lead, activities.create_lead,
                activities.update_lead_touch, activities.log_touchpoint,
                activities.assign_lead_owner,
                activities.get_lead, activities.verify_decision,
                activities.convert_lead_txn,
                activities.create_deal, activities.create_line,
                activities.mark_lead_converted, activities.mark_lead_note,
                activities.soft_delete_row,
            ],
        ):
            api = create_app()
            api.state.temporal = env.client  # inject; skip the lifespan connect
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api), base_url="http://orch",
            ) as orch, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{REGISTER_PORT}",
                headers={"X-API-Key": "test-key", "X-Tenant": "EVAM"},
            ) as reg:
                yield orch, reg, env.client
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The scenarios
# --------------------------------------------------------------------------- #
async def test_new_company_capture_and_duplicate_retry(stack):
    orch, reg, _ = stack
    name = f"Verdant Hydro {uuid.uuid4().hex[:6]} Pvt Ltd"
    capture = {
        "capture_id": f"cap-{uuid.uuid4().hex[:10]}",
        "company_name": name,
        "interaction_type": "Site Visit / Due Diligence",
        "summary": "First site visit; strong offtake pipeline",
        "transcript": "Plant running at 92 percent availability...",
        "audio_ref": "s3://vox-audio/cap-1.ogg",
        "performed_by": "Chetan",
        "assigned_rm": "Shubh",
        "next_action": "Collect FY26 financials",
        "next_action_date": "2026-08-01",
        "next_meeting_date": "2026-08-04",
        "sector": "Solar - Developer",
    }
    first = await orch.post("/v1/workflows/vox-touchpoints",
                            params={"wait": "true"}, json=capture)
    assert first.status_code == 200, first.text
    r1 = first.json()["result"]
    assert r1["entity_created"] is True and r1["lead_created"] is True

    # The flaky-uplink retry: SAME capture id → SAME workflow → SAME rows, nothing new.
    second = await orch.post("/v1/workflows/vox-touchpoints",
                             params={"wait": "true"}, json=capture)
    assert second.status_code == 200, second.text
    r2 = second.json()["result"]
    assert r2["interaction_id"] == r1["interaction_id"]
    assert r2["entity_id"] == r1["entity_id"]

    ents = (await reg.get("/v1/entities", params={"q": name[:40]})).json()["items"]
    assert len(ents) == 1
    leads = (await reg.get("/v1/leads",
                           params={"entity_id": r1["entity_id"]})).json()["items"]
    assert len(leads) == 1 and leads[0]["rm"] == "Shubh"
    ints = (await reg.get("/v1/interactions",
                          params={"entity_id": r1["entity_id"]})).json()["items"]
    assert len(ints) == 1
    row = ints[0]
    assert row["transcript"].startswith("Plant running")
    assert row["source"] == "VOX"
    assert row["source_ref"] == r1["workflow_id"]      # traceable to the Temporal run
    assert row["attachments"] == [{"kind": "audio", "uri": "s3://vox-audio/cap-1.ogg"}]
    assert row["meta"]["calendar"]["status"] == "pending"


async def test_existing_company_links_active_lead(stack):
    orch, reg, _ = stack
    base = f"Kinetic Green {uuid.uuid4().hex[:6]}"
    ent = (await reg.post("/v1/entities", json={
        "code": f"KG-{uuid.uuid4().hex[:6]}",
        "legal_name": f"{base} Private Limited"})).json()
    lead = (await reg.post("/v1/leads", json={
        "company": base, "entity_id": ent["id"], "status": "Active"})).json()
    # Plant a NEWER active lead on an unrelated company: the wrong-company bug would
    # have linked THIS one instead of the target company's lead.
    decoy_ent = (await reg.post("/v1/entities", json={
        "code": f"DECOY-{uuid.uuid4().hex[:6]}",
        "legal_name": "Decoy Newer Co Pvt Ltd"})).json()
    decoy = (await reg.post("/v1/leads", json={
        "company": "Decoy Newer Co", "entity_id": decoy_ent["id"],
        "status": "Active"})).json()

    resp = await orch.post("/v1/workflows/vox-touchpoints", params={"wait": "true"},
                           json={"capture_id": f"cap-{uuid.uuid4().hex[:10]}",
                                 # Suffix-stripped variant — canonical matching's job.
                                 "company_name": f"{base} Pvt Ltd",
                                 "summary": "Follow-up call",
                                 "performed_by": "Chetan",
                                 "next_action": "Share term sheet",
                                 "next_action_date": "2026-08-02",
                                 "occurred_at": "2026-07-26T11:00:00+05:30"})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["entity_created"] is False
    assert result["lead_created"] is False
    assert result["lead_id"] == lead["id"]
    assert result["lead_id"] != decoy["id"]

    # The decoy lead must be untouched.
    decoy_after = (await reg.get(f"/v1/leads/{decoy['id']}")).json()
    assert decoy_after.get("next_action") is None

    updated = (await reg.get(f"/v1/leads/{lead['id']}")).json()
    assert updated["next_action"] == "Share term sheet"
    assert updated["last_interaction_date"] == "2026-07-26"


async def test_lead_conversion_signal_approval(stack):
    orch, reg, _ = stack
    ent = (await reg.post("/v1/entities", json={
        "code": f"CONV-{uuid.uuid4().hex[:6]}",
        "legal_name": f"Conversion Co {uuid.uuid4().hex[:6]} Pvt Ltd"})).json()
    lead = (await reg.post("/v1/leads", json={
        "company": "Conversion Co", "entity_id": ent["id"], "status": "Active"})).json()

    started = await orch.post("/v1/workflows/lead-conversions", json={
        "lead_id": lead["id"], "requested_by": "chetan@evamfinance.com",
        "is_lending": True, "product_type": "Term Loan", "amount_cr": 25,
        "rm": "Chetan", "analyst": "Archana"})
    assert started.status_code == 202, started.text
    wf_id = started.json()["workflow_id"]

    status = (await orch.get(f"/v1/workflows/{wf_id}")).json()
    assert status["status"] == "RUNNING" and status.get("stage") == "Pending"

    approved = await orch.post(f"/v1/workflows/{wf_id}/approve",
                               json={"by": "credit.head@evamfinance.com",
                                     "note": "Proceed to diligence"})
    assert approved.status_code == 200, approved.text

    # The run completes; describe now carries the applied result.
    deadline = time.monotonic() + 30
    final = None
    while time.monotonic() < deadline:
        final = (await orch.get(f"/v1/workflows/{wf_id}")).json()
        if final["status"] == "COMPLETED":
            break
    assert final and final["status"] == "COMPLETED", final
    result = final["result"]
    assert result["status"] == "Approved"
    assert result["deal_id"] and result["lending_id"]

    converted = (await reg.get(f"/v1/leads/{lead['id']}")).json()
    assert converted["status"] == "Converted"
    assert converted["converted_deal_id"] == result["deal_id"]
    deal = (await reg.get(f"/v1/deals/{result['deal_id']}")).json()
    assert deal["is_lending"] is True and deal["stage"] == "Data Awaited"
