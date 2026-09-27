"""The staged CAM drafter: the master prompt's own workflow as bounded engine calls."""

from __future__ import annotations

import asyncio
import re

from test_cam_workbench import LENDING, _app, _call, _RegisterStub

from app import cam as cam_mod
from app.cam_pipeline import (
    DigestCache,
    SourceDoc,
    draft_staged,
    fit,
    parse_master_prompt,
    split_pages,
)

MASTER = """EVAM FINANCE — CAM MASTER PROMPT
BLOCK A: PERSONA, ANALYTICAL RULES
A1. PERSONA
You are a senior credit analyst.
A3.5 Locked formulas: DSCR = (PAT + Dep + Interest) / (Interest + Principal).
BLOCK B: DOCUMENT STRUCTURE
PHASE A: DEEP DATA EXTRACTION
Extract every figure with its source.
PHASE B: PUBLIC INFORMATION SCRUBBING
Run at least 10 public checks: MCA, courts, news.
PHASE C: FORENSIC ANALYSIS
Triangulate revenue across GST, bank and ITR.
SECTION 1: EXECUTIVE SUMMARY (350–500 words + snapshot table)
Summarise the proposal and the verdict.
SECTION 2: MANAGEMENT PROFILE (400–700 words + tables)
Profile the promoters.
SECTION 3: FINANCIAL ANALYSIS (1,200–1,800 words + tables)
Analyse the financials using the locked dataset.
SECTION 4: RECOMMENDATIONS (300–450 words)
Recommend with conditions.
ANNEXURE I: DEBT PROFILE
Term borrowings table.
JUDGMENT QA (read-through)
Section 3: no pure number restatement.
Every figure carries a source.
BLOCK C: TECHNICAL FORMATTING SPECIFICATION
C1. Page setup A4.
"""


def test_the_master_prompt_splits_into_its_parts():
    mp = parse_master_prompt(MASTER)
    assert mp is not None
    assert [s.heading for s in mp.sections] == [
        "SECTION 1: EXECUTIVE SUMMARY", "SECTION 2: MANAGEMENT PROFILE",
        "SECTION 3: FINANCIAL ANALYSIS", "SECTION 4: RECOMMENDATIONS",
        "ANNEXURE I: DEBT PROFILE"]
    assert [s.final for s in mp.sections] == [True, False, False, True, False]
    assert "words" in mp.sections[0].title                  # the writer keeps the hint
    assert "Locked formulas" in mp.rules and "PHASE A" not in mp.rules
    assert set(mp.phases) == {"A", "B", "C"} and "10 public checks" in mp.phases["B"]
    # The QA checklist is split off (its mixed-case "Section 3:" line is not a section)
    # and Block C formatting never reaches the writers.
    assert "no pure number restatement" in mp.qa
    assert "Term borrowings table." in mp.sections[4].spec
    assert "no pure number" not in mp.sections[4].spec
    assert all("Page setup" not in s.spec for s in mp.sections)


def test_a_plain_prompt_is_not_forced_through_the_pipeline():
    assert parse_master_prompt("Write a CAM for this borrower. Be thorough.") is None


def test_large_documents_split_at_page_boundaries():
    text = "\n".join(f"[page {n}]\n" + "x" * 400 for n in range(1, 11))
    parts = split_pages(text, 1000)
    assert len(parts) >= 5 and all(len(p) <= 1000 for p in parts)
    assert all(p.startswith("[page ") for p in parts)
    assert "".join(parts).count("[page") == 10
    assert split_pages("short", 1000) == ["short"]


def test_fit_clips_the_longest_parts_first():
    parts = ["a" * 100, "b" * 10_000, "c" * 5_000]
    out = fit(parts, 9_000)
    assert out[0] == parts[0]                            # short fact sheets survive whole
    assert sum(len(p) for p in out) <= 9_000 + 200
    assert "clipped to fit" in out[1]


class _Fake:
    """Answers by task; records every call."""

    def __init__(self, fail_section: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_section = fail_section

    async def __call__(self, system: str, turns):  # noqa: ANN001
        user = turns[0]["content"]
        self.calls.append((system, user))
        await asyncio.sleep(0)
        if (m := re.search(r"Write ONLY this part of the CAM: (.+)", user)):
            title = m.group(1)
            if self.fail_section and self.fail_section in title:
                raise RuntimeError("provider timeout")
            return f"## {title.split(' (')[0]}\n\nText for {title.split(':')[0]}."
        if "Produce the FACT SHEET" in user:
            doc = re.search(r"===== DOCUMENT: (.+?) =====", user).group(1)
            return f"| Revenue | FY25 | 120.0 | Rs lakh | [source: {doc}, page 1] |"
        if "Public information checks" in user:
            return "## Public information checks — NOT PERFORMED\n| Check | Source | Status |"
        return "## Financial dataset workpaper\n| Revenue | FY25 | 120.0 |"


def _docs():
    return [SourceDoc("d1", "Audited FS FY25.pdf", "[page 1]\nRevenue 120.0", "financial_report", 1),
            SourceDoc("d2", "Bank statement.pdf", "[page 1]\nCredits 98.0", "bank_statement", 1)]


async def test_staged_draft_writes_every_section_from_the_locked_dataset():
    fake, seen = _Fake(), []
    mp = parse_master_prompt(MASTER)
    res = await draft_staged(generate=fake, system="SYS", prompt=mp, docs=_docs(),
                             analyst_notes="Analyst: focus on DSCR.", cache=DigestCache(),
                             model_id="m", progress=lambda *a: seen.append(a))
    heads = [ln for ln in res.draft_md.splitlines() if ln.startswith("## ")]
    assert heads == ["## SECTION 1: EXECUTIVE SUMMARY", "## SECTION 2: MANAGEMENT PROFILE",
                     "## SECTION 3: FINANCIAL ANALYSIS", "## SECTION 4: RECOMMENDATIONS",
                     "## Public information checks — NOT PERFORMED",
                     "## ANNEXURE I: DEBT PROFILE"]
    assert res.workpaper_md.startswith("## Financial dataset workpaper")
    # 2 fact sheets + dataset + checks + 3 body + 2 final sections
    assert res.calls == 9
    sections = {re.search(r"Write ONLY this part of the CAM: (.+)", u).group(1): u
                for _s, u in fake.calls if "Write ONLY" in u}
    body_ctx = sections["SECTION 3: FINANCIAL ANALYSIS (1,200–1,800 words + tables)"]
    assert "LOCKED FINANCIAL DATASET" in body_ctx and "[source: Audited FS FY25.pdf" in body_ctx
    assert "DOCUMENT MANIFEST" in body_ctx and "focus on DSCR" in body_ctx
    assert body_ctx.rstrip().endswith("Do not write any other section.")   # instructions last
    final_ctx = sections["SECTION 1: EXECUTIVE SUMMARY (350–500 words + snapshot table)"]
    assert "THE CAM BODY AS WRITTEN" in final_ctx and "Text for SECTION 3" in final_ctx
    assert all("Locked formulas" in s for s, _u in fake.calls)         # Block A everywhere
    assert {a[0] for a in seen} >= {"Reading documents", "Writing sections", "Done"}


async def test_fact_sheets_are_cached_between_generations():
    cache, mp = DigestCache(), parse_master_prompt(MASTER)
    first, second = _Fake(), _Fake()
    await draft_staged(generate=first, system="S", prompt=mp, docs=_docs(), analyst_notes="",
                       cache=cache, model_id="m", progress=lambda *a: None)
    await draft_staged(generate=second, system="S", prompt=mp, docs=_docs(), analyst_notes="",
                       cache=cache, model_id="m", progress=lambda *a: None)
    assert sum("Produce the FACT SHEET" in u for _s, u in first.calls) == 2
    assert sum("Produce the FACT SHEET" in u for _s, u in second.calls) == 0


async def test_a_failed_section_is_marked_not_fatal():
    fake = _Fake(fail_section="MANAGEMENT")
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(),
                             model_id="m", progress=lambda *a: None)
    assert "## SECTION 2: MANAGEMENT PROFILE\n\n> This section could not be drafted" in res.draft_md
    assert any(n.get("section", "").startswith("SECTION 2") for n in res.notes)
    assert "Text for SECTION 3" in res.draft_md


# ------------------------------------------------------------------- the job routes
class _Engine(cam_mod.CamEngine):
    name = "fake:glm"

    def __init__(self) -> None:
        self.fake = _Fake()

    async def generate(self, http, system, turns):  # noqa: ANN001
        return await self.fake(system, turns)


async def _poll(app, job_id: str, email: str = "bhavana@evamfinance.com") -> dict:
    for _ in range(200):
        r = await _call(app, "GET", f"/v1/cam/jobs/{job_id}")
        body = r.json()
        if body.get("status") in ("done", "failed"):
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


def _job_app(monkeypatch, engine):
    monkeypatch.setattr(cam_mod, "build_engine", lambda settings: engine)
    app = _app(monkeypatch)
    stub = _RegisterStub()
    stub.docs["master"] = ("text/markdown", MASTER)
    stub.docs["tmpl"] = ("text/markdown", "# CAM template")
    stub.docs["fin-2"] = ("text/csv", "year,ebitda\n2025,14")
    app.state.http = stub
    return app, stub


async def test_generate_cam_runs_as_a_staged_job(monkeypatch):
    eng = _Engine()
    app, stub = _job_app(monkeypatch, eng)
    # An earlier Ask sent fin-1: it belongs to this CAM's document set automatically.
    ask = await _call(app, "POST", f"/v1/cam/{LENDING}/refine", json={
        "instruction": "What is revenue?", "update_draft": False, "source_doc_ids": ["fin-1"]})
    assert ask.status_code == 200

    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "template_doc_id": "tmpl", "source_doc_ids": ["fin-2"],
        "doc_titles": {"fin-1": "Revenue sheet.csv", "fin-2": "EBITDA sheet.csv"},
        "instruction": "Stress DSCR at 20% lower revenue."})
    assert start.status_code == 202, start.text
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done", job
    assert job["mode"] == "staged" and job["calls"] >= 9
    assert {d["document"] for d in job["documents"]} == {"Revenue sheet.csv", "EBITDA sheet.csv"}
    assert "## SECTION 3: FINANCIAL ANALYSIS" in job["draft_md"]
    assert job["workpaper_md"].startswith("## Financial dataset workpaper")
    # The template is rendering-only — never read as a source; the analyst's words ride.
    assert all("# CAM template" not in u for _s, u in eng.fake.calls)
    assert any("Stress DSCR at 20% lower revenue" in u for _s, u in eng.fake.calls)
    assert any("What is revenue?" in u for _s, u in eng.fake.calls)
    # Filed on the CAM's transcript as the working draft.
    rid = start.json()["report_id"]
    assert stub.reports[rid]["draft_md"] == job["draft_md"]


async def test_a_plain_prompt_drafts_in_one_call(monkeypatch):
    eng = _Engine()
    app, _stub = _job_app(monkeypatch, eng)
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_text": "Write a short CAM.", "source_doc_ids": ["fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done" and job["mode"] == "single" and job["calls"] == 1


async def test_draft_needs_documents_and_jobs_are_private(monkeypatch):
    app, _stub = _job_app(monkeypatch, _Engine())
    none = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={"prompt_doc_id": "master"})
    assert none.status_code == 422 and "Tick the deal documents" in none.text
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["fin-1"]})
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://o") as c:
        other = await c.get(f"/v1/cam/jobs/{start.json()['job_id']}",
                            headers={"X-API-Key": "k", "X-User-Email": "someone@evamfinance.com"})
    assert other.status_code == 404
    await _poll(app, start.json()["job_id"])


# -------------------------------------------------------------- existing deployments
def test_an_anthropic_only_deployment_keeps_a_real_engine(monkeypatch):
    """Before GLM, deployments set only WORKFLOWS_ANTHROPIC_API_KEY. The new default
    engine (bedrock:…) must not silently turn them into the offline stub."""
    from app.config import get_settings

    monkeypatch.delenv("WORKFLOWS_CAM_ENGINE", raising=False)
    monkeypatch.setenv("WORKFLOWS_CAM_LLM_API_KEY", "")
    monkeypatch.setenv("WORKFLOWS_ANTHROPIC_API_KEY", "ak")
    monkeypatch.setenv("WORKFLOWS_CAM_ENGINE", "bedrock:zai.glm-5")
    get_settings.cache_clear()
    eng = cam_mod.build_engine(get_settings())
    assert isinstance(eng, cam_mod.AnthropicEngine)
    get_settings.cache_clear()


async def test_a_custom_prompt_still_gets_the_template_structure(monkeypatch):
    eng = _Engine()
    app, _stub = _job_app(monkeypatch, eng)
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_text": "Write a short CAM.", "template_doc_id": "tmpl",
        "source_doc_ids": ["fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    assert job["mode"] == "single"
    [(_sys, user)] = eng.fake.calls
    assert "CAM TEMPLATE (reproduce its structure" in user and "# CAM template" in user


class _VisualEngine(_Engine):
    """Reads PDFs itself (like Anthropic): content may be a block list."""
    supports_documents = True

    async def generate(self, http, system, turns):  # noqa: ANN001
        content = turns[0]["content"]
        if isinstance(content, list):
            self.fake.calls.append((system, "BLOCKS:" + content[-1]["text"]))
            return "| Signed consent | 2026 | yes | — | [source: consent scan, page 1] |"
        return await self.fake(system, turns)


async def test_scans_reach_an_engine_that_reads_pdfs_in_the_staged_path(monkeypatch):
    real_extract = cam_mod.extract_text
    monkeypatch.setattr(cam_mod, "extract_text",
                        lambda c, b: ("", cam_mod._SCANNED_PDF) if b"scanned" in b
                        else real_extract(c, b))
    eng = _VisualEngine()
    app, stub = _job_app(monkeypatch, eng)
    stub.docs["scan-1"] = ("application/pdf", "%PDF-1.7 scanned pages")
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["scan-1", "fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done", job
    fates = {d["doc_id"]: d for d in job["documents"]}
    assert fates["scan-1"]["via"] == "attached scan" and "reason" not in fates["scan-1"]
    assert any(u.startswith("BLOCKS:") and "attached scan" in u for _s, u in eng.fake.calls)
