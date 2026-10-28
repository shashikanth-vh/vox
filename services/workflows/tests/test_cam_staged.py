"""The staged CAM drafter: the master prompt's own workflow as bounded engine calls."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from test_cam_workbench import LENDING, _app, _call, _RegisterStub

from app import cam as cam_mod
from app.cam_pipeline import (
    DigestCache,
    SourceDoc,
    draft_staged,
    fit,
    machine_qa,
    parse_master_prompt,
    qa_markdown,
    split_pages,
    unverify_unavailable,
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
        if "Write ONLY the CAM's TITLE PAGE" in user:
            return "# Title\nEvam Finance Private Limited\n\nMacwin Solar\nDocument date: 04-Oct-2026"
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
    # 2 fact sheets + dataset + checks + 3 body + 2 final sections + the title page
    assert res.calls == 10
    sections = {re.search(r"Write ONLY this part of the CAM: (.+)", u).group(1): u
                for _s, u in fake.calls if "Write ONLY this part" in u}
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


# ------------------------------------------------- page 1, the term sheet, machine QA
# The EVAM prompt's real shape: page 1 (Proposal Summary) sits between Phase C and
# Section 1, and the Term Sheet appendix between the last section and the annexures.
MASTER_V2 = MASTER.replace(
    "Triangulate revenue across GST, bank and ITR.\n",
    "Triangulate revenue across GST, bank and ITR.\n"
    "PROPOSAL SUMMARY (Page 1)\n"
    "Part A – Client Details table.\n"
    "Evam Strategy: 4–6 bullets naming companies.\n").replace(
    "ANNEXURE I: DEBT PROFILE\n",
    "APPENDIX: TERMS AND CONDITIONS\nTerm sheet table (Head | Terms).\n"
    "ANNEXURE I: DEBT PROFILE\n")
SEED_PROMPT = (Path(__file__).resolve().parents[2]
               / "register/app/seed/templates/cam_prompt_default.docx")


def test_page_one_and_the_term_sheet_are_sections_of_their_own():
    # Page 1 used to be folded into Phase C (sent only to the dataset call) and the
    # appendix into the last section's spec: neither was ever written.
    mp = parse_master_prompt(MASTER_V2)
    assert [s.heading for s in mp.sections] == [
        "PROPOSAL SUMMARY", "SECTION 1: EXECUTIVE SUMMARY", "SECTION 2: MANAGEMENT PROFILE",
        "SECTION 3: FINANCIAL ANALYSIS", "SECTION 4: RECOMMENDATIONS",
        "APPENDIX: TERMS AND CONDITIONS", "ANNEXURE I: DEBT PROFILE"]
    page1 = mp.sections[0]
    assert page1.final and "Evam Strategy" in page1.spec
    assert "Evam Strategy" not in mp.phases["C"] and "Triangulate" in mp.phases["C"]
    assert "Term sheet" not in mp.sections[4].spec
    assert mp.sections[1].word_range == (350, 500) and page1.word_range is None


def test_the_real_evam_prompt_has_page_one_and_the_appendix():
    text, _skip = cam_mod.extract_text(cam_mod._DOCX_TYPE, SEED_PROMPT.read_bytes())
    mp = parse_master_prompt(text)
    heads = [s.heading for s in mp.sections]
    assert heads[0] == "PROPOSAL SUMMARY" and "APPENDIX: TERMS AND CONDITIONS" in heads
    assert heads.index("APPENDIX: TERMS AND CONDITIONS") == heads.index(
        "SECTION 11: RECOMMENDATIONS") + 1
    assert len(heads) == 16 and "Evam Strategy" not in mp.phases["C"]


async def test_page_one_title_and_qa_are_written_from_the_deal_on_record():
    fake = _Fake()
    res = await draft_staged(
        generate=fake, system="S", prompt=parse_master_prompt(MASTER_V2), docs=_docs(),
        analyst_notes="", cache=DigestCache(), model_id="m", progress=lambda *a: None,
        deal_context="Borrower (entity):\n  legal_name: Macwin Solar\n  cin: U40100TN2019PTC1",
        today="04-Oct-2026")
    lines = res.draft_md.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith("## "))
    # The title block sits under the document title, before page 1; its own heading dropped.
    assert lines[0] == "# Credit Appraisal Memorandum" and "# Title" not in res.draft_md
    assert "Evam Finance Private Limited  " in lines[:first]
    # One line each — the model's blank lines between title lines are dropped.
    assert lines[2:first - 1] == ["Evam Finance Private Limited  ", "Macwin Solar  ",
                                  "Document date: 04-Oct-2026"]
    assert lines[first] == "## PROPOSAL SUMMARY"
    assert res.draft_md.index("## APPENDIX: TERMS AND CONDITIONS") < res.draft_md.index(
        "## ANNEXURE I")
    # Every writer saw the deal as the Register holds it; page 1 saw the finished body.
    writes = [u for _s, u in fake.calls if "Write ONLY" in u]
    assert writes and all("THE DEAL ON THE REGISTER" in u and "U40100TN2019PTC1" in u
                          for u in writes)
    page1 = next(u for u in writes if "this part of the CAM: PROPOSAL SUMMARY" in u)
    assert "THE CAM BODY AS WRITTEN" in page1
    assert "Document date: 04-Oct-2026" in next(u for u in writes if "TITLE PAGE" in u)
    # Machine QA ran and is filed with the workpaper.
    assert res.qa["passed"] is True
    assert "## Machine QA report" in res.workpaper_md


def _qa_prompt():
    return parse_master_prompt(MASTER_V2)


def _qa_draft(fin_table: str, *, drop: str = "", twice: str = "", failed: str = "") -> str:
    parts = []
    for s in _qa_prompt().sections:
        if s.heading == drop:
            continue
        body = fin_table if "FINANCIAL" in s.heading else "Prose " * 400
        if s.heading == failed:
            body = "> This section could not be drafted (timeout)."
        parts.append(f"## {s.heading}\n\n{body}")
        if s.heading == twice:
            parts.append(f"## {s.heading}\n\nAgain.")
    return "# Credit Appraisal Memorandum\n\n" + "\n\n".join(parts)


WORKPAPER = "## Financial dataset workpaper\n| Revenue | FY25 | 1,204.50 |\n| DSCR | FY25 | 1.45 |"


def test_machine_qa_passes_a_complete_draft_whose_figures_are_locked():
    table = ("| Item | FY2025 |\n|---|---|\n| Revenue | 1204.5 |\n| DSCR | 1.45x |\n"
             "| Plants | 3 |\n| Revenue (1 dp) | 1,204.5 |\n| Revenue (rounded) | 1,205 |")
    qa = machine_qa(_qa_prompt(), _qa_draft(table), WORKPAPER)
    by = {c["check"]: c for c in qa["checks"]}
    assert qa["passed"] is True
    # 1,204.50 in the dataset and 1204.5 in the table are the same figure; the year and
    # a small count are labels, not figures to trace.
    assert by["Financial Analysis figures match the locked dataset"]["detail"] == "all 4 traced"


def test_machine_qa_names_every_failure():
    table = "| Item | FY2025 |\n|---|---|\n| Revenue | 1,250.00 |\n| DSCR | 1.45 |"
    draft = _qa_draft(table, drop="ANNEXURE I: DEBT PROFILE", twice="PROPOSAL SUMMARY",
                      failed="SECTION 2: MANAGEMENT PROFILE")
    qa = machine_qa(_qa_prompt(), draft, WORKPAPER)
    by = {c["check"]: c for c in qa["checks"]}
    assert qa["passed"] is False
    assert by["All sections and annexures present"]["detail"] == "missing: ANNEXURE I: DEBT PROFILE"
    assert by["No section duplicated"]["detail"] == "twice: PROPOSAL SUMMARY"
    assert "SECTION 2: MANAGEMENT PROFILE" in by["Every section drafted"]["detail"]
    fig = by["Financial Analysis figures match the locked dataset"]
    assert fig["status"] == "fail" and fig["detail"].startswith("1 of 2 not in the dataset (computed in the section, not "
                                                      "quoted): 1,250.00")
    assert "## Machine QA report" in qa_markdown(qa) and "FAIL" in qa_markdown(qa)


def test_machine_qa_flags_lengths_far_from_the_prompt():
    draft = _qa_draft("| Revenue | 1204.5 |").replace(
        "## SECTION 1: EXECUTIVE SUMMARY\n\n" + "Prose " * 400,
        "## SECTION 1: EXECUTIVE SUMMARY\n\nToo short.")
    by = {c["check"]: c for c in machine_qa(_qa_prompt(), draft, WORKPAPER)["checks"]}
    lengths = by["Section lengths near the prompt's ranges"]
    assert lengths["status"] == "warn" and "SECTION 1: 2 words (asked 350–500)" in lengths["detail"]


async def test_the_draft_reads_the_deal_from_the_register_as_the_caller(monkeypatch):
    eng = _Engine()
    app, stub = _job_app(monkeypatch, eng)
    stub.records = {f"/v1/lending/{LENDING}": {"id": LENDING, "entity_id": "e-1",
                                               "deal_id": "deal-9", "amount_cr": 1.0,
                                               "analyst": "Bhavana", "rm": None},
                    "/v1/entities/e-1": {"legal_name": "Macwin Solar Energy Private Limited",
                                         "cin": "U40100TN2019PTC123456", "pan": ""},
                    "/v1/deals/deal-9": 403}
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done", job
    sent = next(u for _s, u in eng.fake.calls if "Write ONLY this part" in u)
    assert "cin: U40100TN2019PTC123456" in sent and "analyst: Bhavana" in sent
    # Empty fields are left out; a record this role may not read is named, not guessed.
    assert "rm: None" not in sent and "pan:" not in sent
    assert "Deal: not readable on the Register" in sent
    assert job["qa"]["checks"] and "## Machine QA report" in job["workpaper_md"]


# ------------------------------------------------------- ratios computed by PRISM
FIG_BLOCK = ('```locked-figures\n{"unit": "Rs lakh", "periods": [{"period": "FY25", '
             '"basis": "audited", "net_sales": 1204.5, "pbdit": 180.7, "interest_total": 40, '
             '"current_assets": 600, "current_liabilities": 400}]}\n```')


class _FakeFigures(_Fake):
    """A dataset step that ends its workpaper with the locked-figures block."""

    def __init__(self, block: str = FIG_BLOCK, extra: str = "") -> None:
        super().__init__()
        self.block, self.extra = block, extra

    async def __call__(self, system: str, turns):  # noqa: ANN001
        out = await super().__call__(system, turns)
        if "locked-figures" in turns[0]["content"] and "Build the FINANCIAL DATASET" in \
                turns[0]["content"]:
            # The block restates the workpaper's own tables — PRISM traces every figure.
            return (out + "\n| Net sales | FY25 | 1,204.5 |\n| PBDIT | FY25 | 180.7 |\n"
                    "| Finance cost | FY25 | 40.0 |\n| Current assets | FY25 | 600.0 |\n"
                    "| Current liabilities | FY25 | 400.0 |" + self.extra + "\n\n" + self.block)
        return out


async def test_sections_quote_ratios_prism_computed_from_the_locked_figures():
    fake = _FakeFigures()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(),
                             model_id="m", progress=lambda *a: None)
    assert "locked-figures" not in res.workpaper_md
    assert "## Computed ratios (A3.5 — computed by PRISM" in res.workpaper_md
    assert "| PBDIT Margin | 15.0% |" in res.workpaper_md
    assert "| Interest Coverage | 4.52x |" in res.workpaper_md
    writes = [u for _s, u in fake.calls if "Write ONLY this part" in u]
    assert all("| Current Ratio | 1.50x |" in u and "never compute one yourself" in u
               for u in writes)
    assert res.figures["periods"][0]["net_sales"] == 1204.5
    by = {c["check"]: c for c in res.qa["checks"]}
    assert by["Ratios computed by PRISM (A3.5 locked formulas)"]["status"] == "pass"


async def test_without_the_figures_block_qa_says_the_ratios_are_unverified():
    fake = _Fake()                     # its dataset step returns no figures block
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(),
                             model_id="m", progress=lambda *a: None)
    by = {c["check"]: c for c in res.qa["checks"]}
    check = by["Ratios computed by PRISM (A3.5 locked formulas)"]
    assert check["status"] == "warn" and "the model's own arithmetic" in check["detail"]
    assert not any("never compute one yourself" in u for _s, u in fake.calls)


# ------------------------------------------------- public sources PRISM already holds
TRACXN = {"cin": "U40100TN2019PTC123456", "configured": True, "resolved": True,
          "fetched_at": "2026-09-30T10:00:00+00:00",
          "legal_entity": {"name": "MACWIN SOLAR ENERGY PRIVATE LIMITED",
                           "incorporated": "2019-04-02", "status": "Active"},
          "board": [{"name": "Ramesh Kumar", "designation": "Director", "since": "2019"}],
          "shareholders": [{"name": "Ramesh Kumar", "pct": 60.0}],
          "series": {"revenue": {"points": [{"fy": "FY24", "value": 61.2}], "unit": "INR Cr"}}}


async def test_public_checks_get_tracxn_and_pulse_cached_and_named(monkeypatch):
    monkeypatch.setenv("WORKFLOWS_PULSE_URL", "http://pulse:8000")
    eng = _Engine()
    app, stub = _job_app(monkeypatch, eng)
    stub.records = {
        f"/v1/lending/{LENDING}": {"id": LENDING, "entity_id": "e-1"},
        "/v1/entities/e-1": {"legal_name": "Macwin Solar Energy Private Limited"},
        "/v1/panorama/financials": TRACXN,
        "/v1/news/search": {"articles": [
            {"title": "Macwin Solar Energy wins 40 MW NTPC order", "source": "ET",
             "when": "2026-09-20", "severity": "GOOD"},
            {"title": "Solar stocks rally on budget", "source": "ET", "severity": "GOOD"}]}}
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done", job
    # Tracxn is read from the cache only — a paid fetch is always someone's decision.
    tx = [p for u, p in stub.gets if u.endswith("/v1/panorama/financials")]
    assert tx == [{"entity_id": "e-1", "cached_only": "true"}]
    checks = next(u for _s, u in eng.fake.calls if "Public information checks" in u)
    assert "PUBLIC SOURCES PRISM HOLDS" in checks and "Verified via" in checks
    assert "fetched 2026-09-30" in checks and "Ramesh Kumar (Director, since 2019)" in checks
    assert "revenue (INR Cr, as filed — not converted): FY24: 61.2" in checks
    assert "Macwin Solar Energy wins 40 MW NTPC order — ET · PULSE verdict GOOD" in checks
    # A headline that does not name the firm is not evidence about it.
    assert "Solar stocks rally" not in checks
    # The board's names are searched too, and every writer sees the same sources.
    assert any('"Ramesh Kumar"' == p.get("q") for u, p in stub.gets if "news/search" in u)
    assert all("PUBLIC SOURCES PRISM HOLDS" in u for _s, u in eng.fake.calls
               if "Write ONLY this part" in u)


async def test_a_source_not_held_is_named_not_filled(monkeypatch):
    eng = _Engine()
    app, stub = _job_app(monkeypatch, eng)
    stub.records = {f"/v1/lending/{LENDING}": {"id": LENDING, "entity_id": "e-1"},
                    "/v1/entities/e-1": {"legal_name": "Macwin Solar Energy Private Limited"},
                    "/v1/panorama/financials": {"configured": True, "resolved": False,
                                                "note": "Not fetched from Tracxn yet."}}
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    checks = next(u for _s, u in eng.fake.calls if "Public information checks" in u)
    assert job["status"] == "done"
    assert "Tracxn (company filings): not available — Not fetched from Tracxn yet." in checks
    assert "PULSE (live news search): not configured on this deployment." in checks
    # The monitor's filed findings are a Register read, named when the Register cannot
    # answer it — never left silent.
    assert "PULSE monitor findings (Register): not readable (HTTP 404)" in checks


async def test_a_figure_not_in_the_workpaper_is_dropped_and_named():
    # The block claims an inventory the workpaper never states: it is not used.
    fake = _FakeFigures(FIG_BLOCK.replace('"current_assets": 600',
                                          '"inventory": 333, "current_assets": 600'))
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    assert res.figures["periods"][0]["inventory"] is None
    assert "**Figures not in the workpaper's tables, so not used:** FY25 inventory 333" \
        in res.workpaper_md
    by = {c["check"]: c for c in res.qa["checks"]}
    assert by["Extracted figures traced to the workpaper tables"]["status"] == "warn"


async def test_a_likely_unit_slip_fails_qa_and_is_named_in_the_table():
    # FarmWAtt's real draft: an AOC in rupees read as lakh made revenue 3.23 → 24.88 →
    # 1,048.43 and "4,113% growth". PRISM computes it — and says it is implausible.
    fake = _FakeFigures(
        '```locked-figures\n{"periods": [{"period": "FY25", "basis": "audited", '
        '"net_sales": 24.88}, {"period": "FY26", "basis": "audited", '
        '"net_sales": 1048.43}]}\n```', extra="\n| Revenue | FY25 | 24.88 | FY26 | 1,048.43 |")
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(),
                             model_id="m", progress=lambda *a: None)
    assert "⚠ Implausible values" in res.workpaper_md
    assert "- Revenue growth YoY FY26 = 4113.9%" in res.workpaper_md
    by = {c["check"]: c for c in res.qa["checks"]}
    flag = by["Ratios plausible (no likely unit or period slip)"]
    # Growth alone is flagged to confirm (a first year is real) — not a failure.
    assert flag["status"] == "warn"
    assert "a first year, or a unit slip? Revenue growth YoY FY26 = 4113.9%" in flag["detail"]


async def test_days_beyond_any_working_business_fail_qa():
    # The real draft's debtor days of 1,833: an extraction problem until shown otherwise.
    fake = _FakeFigures(
        '```locked-figures\n{"periods": [{"period": "FY26", "basis": "provisional", '
        '"net_sales": 1048.43, "trade_receivables": 5264.2}]}\n```',
        extra="\n| Revenue | FY26 | 1,048.43 |\n| Trade receivables | FY26 | 5,264.2 |")
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(),
                             model_id="m", progress=lambda *a: None)
    flag = {c["check"]: c for c in res.qa["checks"]}[
        "Ratios plausible (no likely unit or period slip)"]
    assert flag["status"] == "fail" and "Debtor Days FY26 = 1,832.7" in flag["detail"]
    assert res.qa["passed"] is False


def test_a_check_cannot_be_verified_by_a_source_prism_did_not_have():
    # The real draft wrote "Verified via PRISM PUBLIC SOURCES" for a Tracxn that answered 409.
    table = ("| Check | Source / query | Status | Finding |\n|---|---|---|---|\n"
             "| Tracxn filings | Tracxn | Verified via PRISM PUBLIC SOURCES, 2026-10-04 | "
             "Not available — HTTP 409 error. |\n"
             "| News | ET | Partly verified via PULSE, 2026-10-04 | 20250421: FarmWatt wins … |")
    out = unverify_unavailable(table).splitlines()
    assert "NOT PERFORMED — the source was not available to PRISM" in out[2]
    assert "Partly verified via PULSE" in out[3]          # a real finding stands
