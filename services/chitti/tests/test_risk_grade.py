"""Company 360 risk grade on the AI host: the rubric is the desk's prompt file, the
colour follows the rubric's own arithmetic, evidence comes from the local DocRAG,
and no model means no grade."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app import risk_grade as rg
from app.config import get_settings

KEY = {"X-API-Key": "gateway-service-key", "X-Tenant": "EVAM"}


def _pillars(**scores):
    return [{"key": k, "score": scores.get(k, 5), "available": True, "evidence": "ev"}
            for k in "ABCDEFG"]


# --------------------------------------------------------------------------- prompt
def test_the_system_prompt_is_part_b_of_the_desk_file_plus_the_json_contract():
    system = rg.load_prompt()
    assert system.startswith("You are a senior credit risk analyst at EVAM")
    assert "=== STEP 1: HARD RED TRIGGERS" in system
    assert "GREEN ≥ 70 | AMBER 45–69 | RED < 45" in system
    # Part A (the desk's how-to) and Part C (the worked example) stay out.
    assert "Where the data lives" not in system and "DESCO" not in system
    assert '"pillars"' in system and "ONE JSON object" in system


# --------------------------------------------------------------------------- rubric
def test_score_and_band_are_recomputed_from_the_weighted_pillars():
    g = rg.enforce({"rating": "GREEN", "score": 90, "pillars": _pillars(
        A=6, B=6, C=6, D=6, E=6, F=6, G=6)})
    assert g["score"] == 60 and g["rating"] == "AMBER" and not g["provisional"]
    assert [p["weight"] for p in g["pillars"]] == [30, 20, 15, 10, 10, 10, 5]
    assert any("recomputed" in a for a in g["adjustments"])
    assert any("the model said GREEN" in a for a in g["adjustments"])


def test_any_hard_trigger_hit_forces_red_whatever_the_score():
    g = rg.enforce({"pillars": _pillars(A=9, B=9, C=9, D=9, E=9, F=9, G=9),
                    "hard_triggers": [{"trigger": "NCLT", "status": "hit", "evidence": "x"}]})
    assert g["score"] == 90 and g["rating"] == "RED" and g["label"] == "RED"


def test_missing_financials_caps_green_at_amber_provisional():
    p = _pillars(A=9, B=9, C=9, D=9, E=9, F=9, G=9)
    p[0]["available"] = False
    g = rg.enforce({"rating": "GREEN", "pillars": p})
    assert g["rating"] == "AMBER" and g["provisional"]
    assert g["label"] == "AMBER – PROVISIONAL"
    assert any("Capped at AMBER: Financials" in a for a in g["adjustments"])


def test_the_rubrics_worked_example_desco_comes_out_amber_provisional():
    """Part C of the prompt file: no financials, no banking, clean news → AMBER –
    PROVISIONAL. Missing pillars are left out of the score, not counted as 0."""
    p = _pillars(A=0, B=0, C=5, D=8, E=2, F=4, G=6)
    p[0]["available"] = p[1]["available"] = False
    g = rg.enforce({"rating": "AMBER", "score": 38, "pillars": p})
    assert g["score"] == 49 and g["label"] == "AMBER – PROVISIONAL"
    assert any("50% of the weight" in a and "A, B" in a for a in g["adjustments"])


def _reading(hit=False, **scores):
    return {"pillars": _pillars(**scores), "confidence": "Medium",
            "hard_triggers": [{"trigger": "NCLT", "status": "Hit" if hit else "Clear"}]}


def test_each_pillar_takes_the_median_of_the_readings():
    # One reading's outlier (C=10) no longer drags the score: C's median is 5.
    g = rg.aggregate([_reading(A=6, B=6, C=5, D=6, E=6, F=6, G=6),
                      _reading(A=6, B=6, C=10, D=6, E=6, F=6, G=6),
                      _reading(A=7, B=5, C=5, D=6, E=6, F=6, G=6)])
    assert [p["score"] for p in g["pillars"]][:3] == [6, 6, 5]
    assert [r["score"] for r in g["runs"]] == [58, 66, 60]
    assert g["score"] == 58                  # A 6, B 6, C 5 … over the fixed weights


def test_confidence_is_a_percentage_of_coverage_and_agreement():
    same = rg.aggregate([_reading(A=6, B=6, C=6, D=6, E=6, F=6, G=6)] * 3)
    assert same["confidence_pct"] == 100 and same["confidence"] == "High"
    assert same["confidence_detail"]["spread"] == 0
    thin = [_reading(A=0, B=0, C=5, D=8, E=2, F=4, G=6) for _ in range(3)]
    for r in thin:
        r["pillars"][0]["available"] = r["pillars"][1]["available"] = False
    g = rg.aggregate(thin)
    # 50% of the weight had data, readings agree: 0.6 × 50 + 0.4 × 100 = 70.
    assert g["confidence_pct"] == 70 and g["confidence_detail"]["data_coverage"] == 50


def test_a_hard_trigger_needs_most_readings():
    one = rg.aggregate([_reading(hit=True, A=8, B=8, C=8, D=8, E=8, F=8, G=8),
                        _reading(A=8, B=8, C=8, D=8, E=8, F=8, G=8),
                        _reading(A=8, B=8, C=8, D=8, E=8, F=8, G=8)])
    assert one["rating"] == "GREEN" and one["hard_triggers"][0]["status"] != "Hit"
    two = rg.aggregate([_reading(hit=True, A=8, B=8, C=8, D=8, E=8, F=8, G=8)] * 2
                       + [_reading(A=8, B=8, C=8, D=8, E=8, F=8, G=8)])
    assert two["rating"] == "RED" and two["borderline"] is False


def test_disagreement_on_the_band_is_named():
    g = rg.aggregate([_reading(A=5, B=5, C=5, D=5, E=5, F=5, G=5),
                      _reading(A=4, B=4, C=4, D=4, E=4, F=4, G=4),
                      _reading(A=5, B=4, C=5, D=4, E=5, F=5, G=4)])
    assert g["borderline"] is True
    assert any("disagreed" in a for a in g["adjustments"])


def test_the_data_gap_rule_caps_but_never_lifts_a_red():
    p = _pillars(A=2, B=2, C=2, D=2, E=2, F=2, G=2)
    p[1]["available"] = False
    g = rg.enforce({"rating": "RED", "pillars": p})
    assert g["rating"] == "RED" and g["label"] == "RED – PROVISIONAL"


def test_out_of_range_and_missing_pillars_are_named_not_hidden():
    g = rg.enforce({"pillars": [{"key": "A", "score": 14, "available": True}]})
    assert g["pillars"][0]["score"] == 10
    assert any("clamped" in a for a in g["adjustments"])
    assert any("Pillar G" in a for a in g["adjustments"])


def test_the_reply_json_survives_fences_and_prose():
    assert rg.parse_json_object('Here:\n```json\n{"rating": "RED"}\n```') == {"rating": "RED"}
    with pytest.raises(ValueError):
        rg.parse_json_object("no json at all")


# --------------------------------------------------------------------------- route
PANORAMA = {
    "anchor": {"entity_id": "e-1", "matched_by": "entity", "name": "Aapaavani Environmental",
               "cin": None, "sector": "Water Treatment", "state": "KA", "about": "STP EPC"},
    "restricted": [],
    "stats": {"open_leads": 1, "live_deals": 1, "deals_in_flight": 1, "deals_done": 0,
              "exposure_ask_cr": 5, "last_touch": "2026-09-01", "documents": 2},
    "leads": [], "deals": [], "syndication": [], "asset_monetisation": [],
    "lending": [{"tracker_no": "L078", "stage": "Diligence", "amount_cr": 5,
                 "stage_updated_at": "2026-09-01", "ladder": []}],
    "interactions": [], "documents": [{"title": "GST", "section": "KYC"}],
    "prospect": None, "contacts": [], "brief": "", "brief_full": "",
}


class _Completions:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[dict] = []

    async def create(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        text = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200))


def _grading_app(monkeypatch, replies, *, docrag=None):
    monkeypatch.setenv("CHITTI_LLM_BASE_URL", "https://bedrock.example/openai/v1")
    monkeypatch.setenv("CHITTI_LLM_API_KEY", "bk")
    monkeypatch.setenv("CHITTI_RISK_GRADE_MODEL", "zai.glm-5")
    if docrag is not None:
        monkeypatch.setenv("CHITTI_DOCRAG_URL", "http://docrag:8000")
        monkeypatch.setenv("CHITTI_DOCRAG_API_KEY", "front")
        real = httpx.AsyncClient
        monkeypatch.setattr(rg.httpx, "AsyncClient",
                            lambda **kw: real(transport=httpx.MockTransport(docrag), **kw))
    get_settings.cache_clear()
    completions = _Completions(replies)

    class _Client:
        def __init__(self, **kw):  # noqa: ANN003
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(rg, "AsyncOpenAI", _Client)
    from app.main import create_app
    return create_app(), completions


async def _post(app, body, headers=KEY):  # noqa: ANN001
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://chitti") as c:
        return await c.post("/v1/risk-grade", json=body, headers=headers)


def _answer():
    p = _pillars(A=5, B=5, C=6, D=7, E=3, F=4, G=6)
    p[0]["available"] = False
    return json.dumps({
        "rating": "AMBER", "provisional": True, "score": 50, "confidence": "Low",
        "verdict": "Engaged but unproven.", "pillars": p,
        "hard_triggers": [{"trigger": "NCLT", "status": "Clear", "evidence": "none"}],
        "risks": ["no financials"], "mitigants": ["live line"],
        "data_gaps": ["Audited financials (3 FY)"], "move_up": "file financials",
        "move_down": "a lender decline", "action": "Hold", "conditions": []})


async def test_the_route_grades_with_the_rubric_and_the_companys_data(monkeypatch):
    app, completions = _grading_app(monkeypatch, ["```json\n" + _answer() + "\n```"])
    r = await _post(app, {"company": "Aapaavani Environmental", "panorama": PANORAMA,
                          "news": [{"headline": "Wins STP order", "source": "ET",
                                    "when": "2026-09-20", "severity": "GREEN"}]})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["label"] == "AMBER – PROVISIONAL" and g["engine"] == "chitti:zai.glm-5"
    assert g["prompt"].startswith("ATLAS_Client") and len(g["prompt_version"]) == 12
    assert g["inputs"]["news_items"] == 1 and g["inputs"]["lending_lines"] == 1
    assert g["inputs"]["document_note"] == "document AI not configured"
    assert g["usage"]["calls"] == 3 and g["usage"]["input_tokens"] == 3000   # 3 runs
    assert [r["score"] for r in g["runs"]] == [g["score"]] * 3
    assert g["confidence_pct"] == round(0.6 * 70 + 0.4 * 100)     # A had no data
    call = completions.calls[0]
    assert call["model"] == "zai.glm-5"
    system, user = call["messages"][0]["content"], call["messages"][1]["content"]
    assert system.startswith("You are a senior credit risk analyst")
    assert "L078" in user and '"days_in_stage"' in user and "Wins STP order" in user
    assert "GOOD" in user


async def test_evidence_comes_from_the_companys_indexed_files_only(monkeypatch):
    seen: list[dict] = []

    def docrag(req: httpx.Request) -> httpx.Response:
        assert req.headers["X-API-Key"] == "front"
        if req.method == "GET":
            return httpx.Response(200, json={"items": [
                {"id": "d1", "name": "Aapaavani Environmental — Audited FY25.pdf"},
                {"id": "d2", "name": "Someone Else — FY25.pdf"}]})
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={"results": [{"chunk": {
            "chunk_id": "c1", "doc": "Audited FY25.pdf", "pages": [4],
            "text": "Revenue FY25 ₹42 Cr; EBITDA ₹6.1 Cr; net worth ₹18 Cr."}}]})

    app, completions = _grading_app(monkeypatch, [_answer()], docrag=docrag)
    r = await _post(app, {"company": "Aapaavani Environmental", "panorama": PANORAMA})
    assert r.status_code == 200, r.text
    assert {tuple(b["doc_ids"]) for b in seen} == {("d1",)}      # scoped to this company
    assert len(seen) == len(rg.DOC_QUESTIONS) and seen[0]["mode"] == "extractive"
    inp = r.json()["inputs"]
    assert inp["document_passages"] == 1 and inp["documents_cited"] == ["Audited FY25.pdf"]
    assert "Revenue FY25 ₹42 Cr" in completions.calls[0]["messages"][1]["content"]


async def test_data_register_files_are_the_evidence_when_sent(monkeypatch):
    def docrag(req: httpx.Request) -> httpx.Response:
        raise AssertionError("the index is not searched when the files are sent")

    app, completions = _grading_app(monkeypatch, [_answer()], docrag=docrag)
    r = await _post(app, {"company": "Macwin Solar", "panorama": PANORAMA,
                          "news": [{"headline": "Promoter named in GST probe", "severity": "RED",
                                    "about": "R. Kumar"}],
                          "documents": [{"name": "Macwin Audited Fins FY 2024-25 signed.pdf",
                                         "section": "Financials", "engines": ["opendataloader"],
                                         "text": "Revenue FY25 ₹61.2 Cr; PAT ₹3.4 Cr"}]})
    assert r.status_code == 200, r.text
    user = completions.calls[0]["messages"][1]["content"]
    assert "FILE: Macwin Audited Fins FY 2024-25 signed.pdf" in user
    assert "Revenue FY25 ₹61.2 Cr" in user and "[about R. Kumar]" in user
    inp = r.json()["inputs"]
    assert inp["documents_read"] == 1 and inp["document_passages"] == 0
    assert inp["documents_cited"] == ["Macwin Audited Fins FY 2024-25 signed.pdf"]


async def test_a_reply_without_json_is_asked_once_more_then_is_a_502(monkeypatch):
    app, completions = _grading_app(monkeypatch, ["I think it is amber."])
    r = await _post(app, {"company": "X", "panorama": PANORAMA})
    assert r.status_code == 502 and len(completions.calls) == 6       # 3 runs × (ask + nudge)
    assert r.json()["error"]["type"] == "grade_failed"


async def test_the_route_needs_the_service_key(monkeypatch):
    app, _ = _grading_app(monkeypatch, [_answer()])
    r = await _post(app, {"company": "X", "panorama": PANORAMA}, headers={})
    assert r.status_code == 401


async def test_without_a_model_there_is_no_grade(monkeypatch):
    monkeypatch.setenv("CHITTI_LLM_BASE_URL", "")
    monkeypatch.setenv("CHITTI_LLM_API_KEY", "")
    get_settings.cache_clear()
    from app.main import create_app
    r = await _post(create_app(), {"company": "X", "panorama": PANORAMA})
    assert r.status_code == 409
    assert "CHITTI_LLM_API_KEY" in r.json()["error"]["detail"]


def test_the_kind_of_file_decides_whether_financials_and_banking_have_data():
    audited = {"name": "Audited Fins FY25.pdf", "title": "Audited financials — last 3 FYs"}
    sanction = {"name": "canara sanction letter.pdf", "title": "Loan outstanding / SOA"}
    cibil = {"name": "Sukruthi cibil report.pdf", "title": "CIBIL"}
    # Banking has a file, but a sanction letter is not conduct: B has no data, and the
    # readings that scored B 0 "for lack of data" do not drag the score (Macwin, 28 Sep).
    raws = [_reading(A=6, B=0, C=6, D=6, E=6, F=6, G=6) for _ in range(3)]
    for r in raws:
        r["pillars"][1]["available"] = False
    raws[0]["pillars"][1].update(available=True, score=5)
    g = rg.aggregate(raws, [audited, sanction])
    assert not g["pillars"][1]["available"] and g["provisional"] and g["score"] == 60
    assert any("no banking evidence was read" in a for a in g["adjustments"])
    # A bureau report is conduct evidence: B has data, scored from the readings with data.
    raws = [_reading(A=6, B=5, C=6, D=6, E=6, F=6, G=6) for _ in range(3)]
    raws[2]["pillars"][1].update(available=False, score=0)
    g = rg.aggregate(raws, [audited, cibil])
    assert g["pillars"][1]["available"] and g["pillars"][1]["score"] == 5
    assert not g["provisional"]


def test_a_no_data_zero_never_drags_a_pillar():
    raws = [_reading(A=6, B=6, C=7, D=6, E=6, F=6, G=6),
            _reading(A=6, B=6, C=7, D=6, E=6, F=6, G=6),
            _reading(A=6, B=6, C=0, D=6, E=6, F=6, G=6)]
    raws[2]["pillars"][2]["available"] = False
    assert rg.aggregate(raws)["pillars"][2]["score"] == 7
