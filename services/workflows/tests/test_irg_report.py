"""The IRG report (Oct 2026): a superset of the CAM — the same reading and locked dataset,
the rubric's grade computed on the fact sheets, and every pillar explained."""

from __future__ import annotations

import asyncio
import io
import zipfile

import httpx
from test_cam_staged import _Fake
from test_cam_workbench import _call
from test_risk_grade import BODY, GRADE, _AiHost, _grading_app, _Register

from app import cam as cam_mod
from app import irg


class _Engine(cam_mod.CamEngine):
    name = "fake:glm"

    def __init__(self) -> None:
        self.fake = _Fake()

    async def generate(self, http, system, turns, effort=None):  # noqa: ANN001
        return await self.fake(system, turns)


class _NoTemplate(_Register):
    async def get(self, url, **kw):  # noqa: ANN001, ANN003
        if "/v1/templates/" in str(url):
            return httpx.Response(404, json={}, request=httpx.Request("GET", str(url)))
        return await super().get(url, **kw)


async def _run(monkeypatch, ai: _AiHost, refresh: bool = False):
    eng = _Engine()
    monkeypatch.setattr(cam_mod, "build_engine", lambda settings: eng)
    app = _grading_app(monkeypatch, ai, _NoTemplate())
    start = await _call(app, "POST", "/v1/panorama/risk-grade/full",
                        json={**BODY, "refresh": refresh})
    assert start.status_code == 202, start.text
    jid = start.json()["job_id"]
    for _ in range(300):
        job = (await _call(app, "GET", f"/v1/panorama/risk-grade/full/{jid}")).json()
        if job.get("status") in ("done", "failed"):
            return app, eng, job
        await asyncio.sleep(0.01)
    raise AssertionError("IRG job did not finish")


async def test_the_irg_grades_on_fact_sheets_and_explains_every_pillar(monkeypatch):
    ai = _AiHost()
    app, eng, job = await _run(monkeypatch, ai)
    assert job["status"] == "done", job
    # The grader read each document's FACT SHEET, not clips — on a cold run beside the
    # dataset step, so before it (the second test covers the cached path).
    [(_u, kw)] = ai.grades
    docs = kw["json"]["documents"]
    assert docs and all(d["title"] == "fact sheet" for d in docs)
    # Every section was written knowing the grade, and each pillar section is there.
    asks = [u for _s, u in eng.fake.calls if "Write ONLY this part" in u]
    assert len(asks) == len(irg.SECTIONS)
    assert all("THE RISK GRADE (the rubric's result" in u for u in asks)
    md = job["report_md"]
    assert md.startswith("# Internal Risk Grade Report")
    for title, _spec, _final in irg.SECTIONS:
        assert f"## {title.split(' (')[0]}" in md
    # PRISM's own parts: the scorecard after the rating summary, appendices at the end.
    assert md.index("## RATING SUMMARY") < md.index("### Scorecard") < md.index("## SECTION 1")
    assert "| A. Financial strength | 30% | 6 | 18 |" in md
    assert md.index("## SECTION 10") < md.index("## APPENDIX A: HOW THE SCORE WAS BUILT")
    assert "## APPENDIX B: EVIDENCE READ" in md
    assert "Risk grade: AMBER – PROVISIONAL — 52/100" in md
    # The company's grade is this one, and the report downloads as a styled Word file.
    cached = (await _call(app, "GET", "/v1/panorama/risk-grade",
                          params={"company": BODY["company"], "entity_id": "e-1"})).json()
    assert cached["score"] == 52
    r = await _call(app, "GET", f"/v1/panorama/risk-grade/full/{job['job_id']}/report")
    assert r.status_code == 200
    doc = zipfile.ZipFile(io.BytesIO(r.content)).read("word/document.xml").decode()
    assert "Internal Risk Grade Report" in doc and 'w:tblW w:w="5000" w:type="pct"' in doc


async def test_a_failed_grade_fails_the_job_by_name(monkeypatch):
    ai = _AiHost(502, {"error": {"title": "Grading failed", "detail": "model down"}})
    _app, _eng, job = await _run(monkeypatch, ai)
    assert job["status"] == "failed" and "The rubric could not grade" in job["error"]


def test_grade_context_carries_the_rubric_verbatim():
    ctx = irg.grade_context(GRADE)
    assert "Rating: AMBER – PROVISIONAL · score 52/100 · confidence 64%" in ctx
    assert "A. Financial strength: 6/10 × 30% = 18 points" in ctx
    assert "NCLT: Clear" in ctx


def test_identity_numbers_never_reach_the_ai_host_in_fact_sheets():
    typed = [("KYC", "===== FACT SHEET: PAN card =====\nPAN ABAFP0945L, Aadhaar 1234 5678 9012")]
    docs = irg.documents_for_grade(typed, "## Financial dataset workpaper")
    assert "ABAFP0945L" not in docs[1]["text"] and "1234 5678 9012" not in docs[1]["text"]


async def test_a_second_irg_reuses_the_dataset_and_grades_with_it(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_CAM_DIGEST_CACHE_DIR", str(tmp_path))
    ai = _AiHost()
    _app, eng, first = await _run(monkeypatch, ai)
    assert first["status"] == "done", first
    _app2, eng2, second = await _run(monkeypatch, ai, refresh=True)
    assert second["status"] == "done", second
    # The dataset came from the cache: no dataset call, and the grader got it first.
    assert not any("Build the FINANCIAL DATASET" in u for _s, u in eng2.fake.calls)
    docs = ai.grades[-1][1]["json"]["documents"]
    assert docs[0]["name"].startswith("PRISM — locked financial dataset")


async def test_an_unchanged_file_gets_its_report_back_at_once(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_CAM_DIGEST_CACHE_DIR", str(tmp_path))
    ai = _AiHost()
    app, eng, first = await _run(monkeypatch, ai)
    calls = len(eng.fake.calls)
    again = await _call(app, "POST", "/v1/panorama/risk-grade/full", json=BODY)
    body = again.json()
    assert body["status"] == "done" and body["reused"] is True
    assert len(eng.fake.calls) == calls and len(ai.grades) == 1     # nothing recomputed
    forced = await _call(app, "POST", "/v1/panorama/risk-grade/full",
                         json={**BODY, "refresh": True})
    assert forced.json()["status"] == "running"
    for _ in range(300):
        j = (await _call(app, "GET", f"/v1/panorama/risk-grade/full/{forced.json()['job_id']}")).json()
        if j["status"] != "running":
            break
        await asyncio.sleep(0.01)
    assert len(ai.grades) == 2


async def test_without_a_cam_the_irg_builds_the_concise_dataset(monkeypatch):
    ai = _AiHost()
    _app, eng, job = await _run(monkeypatch, ai)
    asks = [u for _s, u in eng.fake.calls if "FINANCIAL DATASET WORKPAPER" in u]
    assert asks and all("CONCISE" in u and "No stress tests" in u for u in asks)
