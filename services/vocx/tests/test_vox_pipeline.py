"""The reliability spine, exercised failure-first: timeouts, malformed model
output, resume-from-what-succeeded, the five-strikes rule, suspect segments,
and the Haiku/Sonnet routing."""

from __future__ import annotations

import json
import time

import pytest

from app.vocx.pipeline import PipelineRunner, mark_suspect_segments
from app.vocx.pipeline.runner import MAX_RETRIES
from app.vocx.pipeline.structure import MODEL_LIVE, MODEL_NOTE


# ------------------------------------------------------------------ test doubles

class FakeRegister:
    """In-memory register enforcing the SAME transition table as the real
    endpoint — so a runner that cheats the state machine fails here too."""

    TRANSITIONS = {
        "queued": {"uploading", "processing", "processing_failed"},
        "uploading": {"processing", "processing_failed"},
        "processing": {"ready", "processing_failed"},
        "processing_failed": {"processing", "failed_permanently"},
        "failed_permanently": {"processing"},
        "ready": {"processing"},
        "submitted": set(),
    }

    def __init__(self, **row):
        self.row = {"status": "queued", "retry_count": 0,
                    "recording_mode": "post_meeting",
                    "audio_ref": "vox/audio/cap-1.webm", **row}
        self.patches: list[dict] = []

    def get(self, cid):
        return dict(self.row)

    def patch(self, cid, **fields):
        self.patches.append(fields)
        status = fields.pop("status", None)
        if status and status != self.row["status"]:
            assert status in self.TRANSITIONS[self.row["status"]], (
                f"illegal move {self.row['status']} -> {status}")
            self.row["status"] = status
        if fields.pop("retry_increment", False):
            self.row["retry_count"] += 1
        if status == "ready":
            self.row["processing_error"] = None
        self.row.update(fields)
        return dict(self.row)


def good_transcribe(audio_ref):
    return {"text": "met suryodaya at whitefield, forty megawatt, twenty five crore ask",
            "segments": [
                {"text": "met suryodaya at whitefield", "no_speech_prob": 0.05},
                {"text": "forty megawatt, twenty five crore ask", "no_speech_prob": 0.1},
            ],
            "language": "en"}


def _valid_model_json():
    cell = lambda v, c="high", **kw: {"value": v, "confidence": c, **kw}  # noqa: E731
    return json.dumps({
        "detected_use_cases": ["lending"],
        "common": {
            "meeting_type": cell("in_person"), "meeting_date": cell(None, "n/a"),
            "location": cell("Whitefield", "medium"), "sector": cell("Renewables"),
            "subsector": cell(None, "n/a"), "attendees_counterparty": cell([], "n/a"),
            "key_discussion_points": cell(["40 MW discussion"]),
            "meeting_summary": cell(None, "n/a"),
            "follow_up_time": cell(None, "n/a"),
            "action_items": cell([], "n/a"), "next_steps": cell("Share DPR"),
            "follow_up_date": cell(None, "n/a"),
            "opportunity_assessment": cell("Real ask.", "n/a"),
            "opportunity_score": cell(3, "medium", user_override=False),
            "opportunity_score_override_reason": cell(None, "n/a"),
            "competitive_intelligence": cell("", "n/a"),
            "data_quality_flags": cell([], "n/a"),
        },
        "lending": {
            "requirement_nature": cell("project_finance"),
            "requirement_quantum_cr": cell(25, "medium"),
            "company_turnover_cr": cell(None, "n/a"),
            "existing_bankers": cell(None, "n/a"),
            "present_requirement": cell("25 Cr project finance"),
            "remarks": cell(None, "n/a"),
        },
        "entity_candidates": ["Suryodaya"],
    })


def good_model(model, system, user):
    return _valid_model_json()


# ------------------------------------------------------------------- happy path

def test_a_note_reaches_ready_with_a_validated_report():
    reg = FakeRegister()
    runner = PipelineRunner(reg, good_transcribe, good_model)
    row = runner.process("c1")
    assert row["status"] == "ready"
    assert row["processing_stage"] == "ready"
    assert row["structured_report"]["detected_use_cases"] == ["lending"]
    assert row["prompt_version"] == "v1" and row["registry_version"] == "v1"
    assert row["entity_candidates"] == ["Suryodaya"]
    # the null-sector-on-lending nudge arrived server-side
    flags = row["structured_report"]["common"]["data_quality_flags"]["value"]
    assert "sector not determinable" not in flags  # sector WAS set
    assert any("Turnover" in f for f in flags)     # null numeric flagged


def test_processing_a_ready_row_is_a_no_op():
    reg = FakeRegister()
    runner = PipelineRunner(reg, good_transcribe, good_model)
    runner.process("c1")
    n = len(reg.patches)
    runner.process("c1")
    assert len(reg.patches) == n  # idempotent: nothing re-ran


def test_live_mode_routes_to_sonnet_and_notes_to_haiku():
    seen = []

    def spy_model(model, system, user):
        seen.append(model)
        return _valid_model_json()

    PipelineRunner(FakeRegister(), good_transcribe, spy_model).process("c1")
    PipelineRunner(FakeRegister(recording_mode="live"), good_transcribe, spy_model).process("c2")
    assert seen[0] == MODEL_NOTE and seen[-1] == MODEL_LIVE


# ------------------------------------------------------------------ failure paths

def test_a_transcription_crash_is_a_state_not_a_crash():
    def broken(audio_ref):
        raise ConnectionError("uplink dropped mid-upload")

    reg = FakeRegister()
    row = PipelineRunner(reg, broken, good_model).process("c1")
    assert row["status"] == "processing_failed"
    assert "uplink dropped" in row["processing_error"]


def test_a_timeout_names_its_stage_and_the_retry_resumes():
    calls = {"n": 0}

    def slow_then_fine(audio_ref):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(1.0)
        return good_transcribe(audio_ref)

    reg = FakeRegister()
    runner = PipelineRunner(reg, slow_then_fine, good_model,
                            timeouts={"transcribe": 0.2})
    row = runner.process("c1")
    assert row["status"] == "processing_failed"
    assert "transcribe" in row["processing_error"]
    # the retry consumes one attempt and completes
    row = runner.process("c1")
    assert row["status"] == "ready" and row["retry_count"] == 1


def test_a_stored_transcript_is_never_retranscribed():
    calls = {"n": 0}

    def counting(audio_ref):
        calls["n"] += 1
        return good_transcribe(audio_ref)

    flaky = {"n": 0}

    def structure_fails_once(model, system, user):
        flaky["n"] += 1
        if flaky["n"] == 1:
            raise TimeoutError("anthropic 529")
        return _valid_model_json()

    reg = FakeRegister()
    runner = PipelineRunner(reg, counting, structure_fails_once)
    assert runner.process("c1")["status"] == "processing_failed"
    assert runner.process("c1")["status"] == "ready"
    assert calls["n"] == 1  # audio transcribed exactly once across both attempts


def test_malformed_model_output_gets_one_repair_round_then_fails_clean():
    attempts = []

    def broken_model(model, system, user):
        attempts.append(user)
        return "{ this is not json"

    reg = FakeRegister()
    row = PipelineRunner(reg, good_transcribe, broken_model).process("c1")
    assert row["status"] == "processing_failed"
    assert row.get("structured_report") is None            # never a partial write
    assert len(attempts) == 2                              # the repair round happened
    assert "violated the contract" in attempts[1]


def test_contract_violations_are_shown_back_verbatim_in_the_repair_round():
    attempts = []

    def wrong_enum_then_fine(model, system, user):
        attempts.append(user)
        if len(attempts) == 1:
            bad = json.loads(_valid_model_json())
            bad["lending"]["requirement_nature"]["value"] = "venture_debt"
            return json.dumps(bad)
        return _valid_model_json()

    row = PipelineRunner(FakeRegister(), good_transcribe, wrong_enum_then_fine).process("c1")
    assert row["status"] == "ready"
    assert "venture_debt" in attempts[1]


def test_five_strikes_parks_the_row_and_alerts_an_admin():
    alerts = []

    def always_broken(audio_ref):
        raise RuntimeError("disk on fire")

    reg = FakeRegister()
    runner = PipelineRunner(reg, always_broken, good_model, alert=alerts.append)
    for _ in range(MAX_RETRIES + 2):
        row = runner.process("c1")
    assert row["status"] == "failed_permanently"
    assert row["retry_count"] == MAX_RETRIES
    assert len(alerts) == 1 and "failed permanently" in alerts[0]
    # and it STAYS parked — no zombie retries
    assert runner.process("c1")["status"] == "failed_permanently"


# ------------------------------------------------------------- suspect segments

def test_silence_hallucination_is_marked_not_extracted():
    segs = mark_suspect_segments([
        {"text": "real content about a 25 crore ask", "no_speech_prob": 0.05},
        {"text": "thank you for watching", "no_speech_prob": 0.93},
        {"text": "okay okay okay okay okay okay", "no_speech_prob": 0.2},
    ])
    assert [s["suspect"] for s in segs] == [False, True, True]
    assert "no_speech_prob" in segs[1]["suspect_reason"]
    assert "repetition" in segs[2]["suspect_reason"]


def test_looping_segments_across_the_stream_are_marked():
    segs = mark_suspect_segments([
        {"text": "subscribe to my channel", "no_speech_prob": 0.2},
        {"text": "subscribe to my channel", "no_speech_prob": 0.2},
        {"text": "subscribe to my channel", "no_speech_prob": 0.2},
    ])
    assert segs[-1]["suspect"] is True


def test_the_structuring_prompt_wraps_suspect_segments_in_markers():
    def spy_model(model, system, user):
        assert "[SUSPECT SEGMENT" in user and "thank you for watching" in user
        assert "never extract a fact solely from a suspect" in " ".join(system.split())
        return _valid_model_json()

    def transcribe_with_noise(audio_ref):
        return {"text": "real content. thank you for watching",
                "segments": [
                    {"text": "real content about the ask", "no_speech_prob": 0.05},
                    {"text": "thank you for watching", "no_speech_prob": 0.95},
                ],
                "language": "en"}

    row = PipelineRunner(FakeRegister(), transcribe_with_noise, spy_model).process("c1")
    assert row["status"] == "ready"
    marked = [s for s in row["transcript_segments"] if s.get("suspect")]
    assert len(marked) == 1


# ------------------------------------------------------------- the kick endpoint

def test_the_process_endpoint_returns_at_once_and_runs_server_side():
    import json as _json
    import time as _time

    from app.vocx.core.atlas import AtlasStore
    from app.vocx.core.server import VocxApp

    app = VocxApp(store=AtlasStore({"interaction_types": [], "interactions": [],
                                    "entities": []}))
    reg = FakeRegister()
    app._vox_runner = PipelineRunner(reg, good_transcribe, good_model)

    code, _, body = app.handle("POST", "/v1/vox/process",
                               {}, _json.dumps({"conversation_id": "c9"}).encode())
    assert code == 202 and _json.loads(body)["ok"] is True
    for _ in range(100):                       # the worker thread finishes fast
        if reg.row["status"] == "ready":
            break
        _time.sleep(0.05)
    assert reg.row["status"] == "ready"

    code, _, body = app.handle("POST", "/v1/vox/process", {}, b'{}')
    assert code == 400


def test_the_capture_door_stores_audio_creates_the_row_and_kicks_processing(tmp_path):
    import json as _json
    import time as _time

    from app.vocx.core.atlas import AtlasStore
    from app.vocx.core.server import VocxApp

    class CreatingRegister(FakeRegister):
        def create(self, **fields):
            assert fields["recording_mode"] == "post_meeting"
            assert fields["recorder_email"] == "ananda@evamfinance.com"
            self.row.update({k: v for k, v in fields.items() if v is not None})
            self.row["id"] = "c-new"
            return dict(self.row)

    app = VocxApp(store=AtlasStore({"interaction_types": [], "interactions": [],
                                    "entities": []}),
                  config={"audio": {"dir": str(tmp_path)}})
    reg = CreatingRegister(audio_ref=None)
    runner = PipelineRunner(reg, good_transcribe, good_model)
    runner.register = reg
    app._vox_runner = runner

    code, _, body = app.handle(
        "POST", "/v1/vox/capture",
        {"capture_id": ["cap-77"], "mode": ["post_meeting"], "rm": ["Ananda H"],
         "email": ["ananda@evamfinance.com"], "duration": ["141"]},
        b"RIFF....fake-audio-bytes")
    out = _json.loads(body)
    assert code == 202 and out["ok"] and out["conversation_id"] == "c-new"
    assert reg.row["audio_ref"]  # stored, and on the row before processing started
    for _ in range(100):
        if reg.row["status"] == "ready":
            break
        _time.sleep(0.05)
    assert reg.row["status"] == "ready"

    # no audio -> honest 400, nothing created
    code, _, body = app.handle("POST", "/v1/vox/capture", {"mode": ["post_meeting"]}, b"")
    assert code == 400


def test_the_nested_subsector_details_shape_is_unwrapped_not_failed():
    """Field finding (stage, first live run): the real model keyed the canonical data
    points under the subsector's own name. Isomorphic shape -> unwrap, ready."""
    def nesting_model(model, system, user):
        obj = json.loads(_valid_model_json())
        obj["common"]["subsector"] = {"value": "Solar-Developer", "confidence": "medium"}
        obj["subsector_details"] = {"Solar-Developer": {
            "operating_uc_capacity_mw": {"value": "40 MW", "confidence": "high"}}}
        return json.dumps(obj)

    row = PipelineRunner(FakeRegister(), good_transcribe, nesting_model).process("c1")
    assert row["status"] == "ready"
    assert row["structured_report"]["subsector_details"] == {
        "operating_uc_capacity_mw": {"value": "40 MW", "confidence": "high"}}


def test_blocks_present_with_no_detected_tags_infer_the_tags():
    """Field finding two: the model filled lending AND syndication blocks but left
    detected_use_cases empty. The blocks are the declaration — infer, and ready."""
    def undeclared_model(model, system, user):
        obj = json.loads(_valid_model_json())
        obj["detected_use_cases"] = []
        obj["syndication"] = {
            "facility_nature": {"value": "term_loan_syndication", "confidence": "medium"},
            "deal_size_cr": {"value": None, "confidence": "n/a"},
            "existing_lenders": {"value": None, "confidence": "n/a"},
            "probable_lenders": {"value": "AIF appetite being explored", "confidence": "medium"},
            "remarks": {"value": None, "confidence": "n/a"},
        }
        return json.dumps(obj)

    row = PipelineRunner(FakeRegister(), good_transcribe, undeclared_model).process("c1")
    assert row["status"] == "ready"
    assert sorted(row["structured_report"]["detected_use_cases"]) == ["lending", "syndication"]


def test_object_entity_candidates_are_flattened_to_names():
    """Field finding five: candidates arrived as objects. Each carries exactly one
    unambiguous name — flatten; anything ambiguous still fails by name."""
    def objecty_model(model, system, user):
        obj = json.loads(_valid_model_json())
        obj["entity_candidates"] = [{"name": "Suryodaya EPC", "type": "company"}, "SBI"]
        return json.dumps(obj)

    row = PipelineRunner(FakeRegister(), good_transcribe, objecty_model).process("c1")
    assert row["status"] == "ready"
    assert row["structured_report"]["entity_candidates"] == ["Suryodaya EPC", "SBI"]


def test_the_forced_tool_schema_reaches_a_schema_aware_model():
    """A callable that accepts schema= gets the contract schema — the API-side wall."""
    seen = {}

    def schema_aware(model, system, user, schema=None):
        seen["schema"] = schema
        return _valid_model_json()

    row = PipelineRunner(FakeRegister(), good_transcribe, schema_aware).process("c1")
    assert row["status"] == "ready"
    sch = seen["schema"]
    assert sch["required"] == ["detected_use_cases", "common", "entity_candidates"]
    assert sch["properties"]["entity_candidates"]["items"] == {"type": "string"}
    assert "lending" in sch["properties"]["detected_use_cases"]["items"]["enum"]
    lending = sch["properties"]["lending"]
    assert lending["additionalProperties"] is False
    assert "requirement_quantum_cr" in lending["required"]


# ---------------------------------------------------------------- known names

def test_known_names_glossary_reaches_the_structuring_prompt():
    """Field finding six: 'SBI' arrived as 'Isbaya', 'Piramal' as 'Pyramid'.
    The runner's known-names provider must land in the structuring user
    message — lender roster, tenant companies, and the correction rules."""
    seen = {}

    def spy_model(model, system, user):
        seen["user"] = user
        return _valid_model_json()

    from app.vocx.pipeline.glossary import build_known_names_block
    runner = PipelineRunner(
        FakeRegister(), good_transcribe, spy_model,
        known_names=lambda: build_known_names_block(["Suryodaya EPC Pvt. Ltd."]))
    row = runner.process("c1")
    assert row["status"] == "ready"
    u = seen["user"]
    assert "KNOWN NAMES" in u
    assert "SBI (State Bank of India)" in u
    assert "Suryodaya EPC Pvt. Ltd." in u
    assert "interest rate in percent" in u
    # the transcript itself still travels untouched, after the context
    assert u.index("KNOWN NAMES") < u.index("TRANSCRIPT:")


def test_known_names_provider_failure_never_fails_the_take():
    """The glossary improves extraction; it must never gate it."""
    def boom():
        raise RuntimeError("register temporarily unreachable")

    row = PipelineRunner(FakeRegister(), good_transcribe,
                         lambda m, s, u: _valid_model_json(),
                         known_names=boom).process("c1")
    assert row["status"] == "ready"


def test_glossary_block_dedupes_and_caps_company_names():
    from app.vocx.pipeline.glossary import MAX_COMPANY_NAMES, build_known_names_block
    block = build_known_names_block(
        ["Acme", "acme", "", None, *[f"Co {i}" for i in range(MAX_COMPANY_NAMES + 50)]])
    assert block.count("Acme") == 1
    assert f"Co {MAX_COMPANY_NAMES - 2}" in block
    assert f"Co {MAX_COMPANY_NAMES + 20}" not in block


# ------------------------------------------------------------- phonetic tier

def test_phonetic_tier_surfaces_the_misheard_company_for_approval():
    """The Suryodaya field test: STT heard 'Sarvodaya'. Token-set fuzzy cannot
    see it; the phonetic tier must — as a needs-approval suggestion, never an
    auto-link and never a silent 'new company'."""
    from app.vocx.core.atlas import Candidate
    from app.vocx.core.resolve import EntityResolver

    class OneNameStore:
        def candidates(self):
            return [Candidate(ref_id="SURYODAYAEPC", name="Suryodaya EPC Pvt. Ltd.",
                              kind="client", ref_type="Entity", rm="Ananda H")]

    got = EntityResolver(OneNameStore()).resolve("Sarvodaya")
    assert got["canonical_name"] == "Suryodaya EPC Pvt. Ltd."
    assert got["match_type"] == "phonetic"
    assert got["needs_approval"] is True          # suggested, not auto-linked
    assert got["is_new_lead"] is False            # and not dropped to new-company


def test_phonetic_tier_handles_split_words_and_rejects_strangers():
    from app.vocx.core.resolve import phonetic_ratio
    assert phonetic_ratio("Chip Balapur", "Chikballapur Solar Park") >= 0.78
    assert phonetic_ratio("Meridian Textiles", "Suryodaya EPC") < 0.78
    assert phonetic_ratio("Tata Power", "Tata Steel") < 0.78


def test_structuring_prefers_the_corrected_transcript():
    """After a reviewer fixes a mis-heard name, regeneration must structure the
    CORRECTED text — and never re-transcribe or alter the verbatim original."""
    seen = {}

    def spy_model(model, system, user):
        seen["user"] = user
        return _valid_model_json()

    # resume path after /regenerate: processing, transcript stored, report cleared
    reg = FakeRegister(status="processing",
                       raw_transcript="met sarvodaya in whitefield",
                       corrected_transcript="met SURYODAYA EPC in Whitefield",
                       structured_report=None)

    def must_not_transcribe(audio_ref):
        raise AssertionError("regeneration must never re-transcribe")

    row = PipelineRunner(reg, must_not_transcribe, spy_model).process("c1")
    assert row["status"] == "ready"
    assert "SURYODAYA EPC" in seen["user"]
    assert "sarvodaya" not in seen["user"]
    assert reg.row["raw_transcript"] == "met sarvodaya in whitefield"


def test_calendar_event_carries_the_promised_reminder():
    """The follow-up card says "Reminder · 1 day before" — the event body must
    actually say it to Google."""
    from app.vocx.google.workspace import CalendarWriter

    class FakeEvents:
        def insert(self, calendarId, body):
            self.body = body
            class _X:
                def execute(_s):
                    return {"id": "e1", "htmlLink": "http://cal/e1", "start": body["start"]}
            return _X()

    class FakeCal:
        def __init__(self): self._e = FakeEvents()
        def events(self): return self._e

    fake = FakeCal()
    w = CalendarWriter(fake)
    r = w.create_event("Site visit", "2026-08-28", None, "site", "Chikballapur plot",
                       reminder_minutes_before=1440)
    assert r["id"] == "e1"
    body = fake._e.body
    assert body["reminders"] == {"useDefault": False,
                                 "overrides": [{"method": "popup", "minutes": 1440}]}
    # without the arg, calendar defaults stay untouched
    w.create_event("Plain", "2026-08-28")
    assert "reminders" not in fake._e.body


def test_unspoken_meeting_date_defaults_to_the_capture_date():
    """Field finding: a note recorded now is about a meeting that just happened.
    A null meeting_date backfills from the capture timestamp at MEDIUM
    confidence — visible in the needs-strip, never invisible to date filters."""
    def dateless_model(model, system, user):
        obj = json.loads(_valid_model_json())
        obj["common"]["meeting_date"] = {"value": None, "confidence": "n/a"}
        return json.dumps(obj)

    reg = FakeRegister(created_at="2026-08-24T09:12:00+05:30")
    row = PipelineRunner(reg, good_transcribe, dateless_model).process("c1")
    assert row["status"] == "ready"
    cell = row["structured_report"]["common"]["meeting_date"]
    assert cell == {"value": "2026-08-24", "confidence": "medium"}


def test_date_rules_reach_the_prompt_context():
    from app.vocx.pipeline.glossary import build_known_names_block
    block = build_known_names_block([])
    assert "Resolve RELATIVE dates" in block
    assert "follow_up_time" in block
    assert "Capture timestamp's date" in block


def test_auth_tickets_are_single_use_and_expire(tmp_path):
    """The Connect Google tab is bearer-less: its identity is a single-use
    ticket minted by the authenticated call. Replay dies, expiry dies."""
    import time
    from app.vocx.core.server import VocxApp
    from app.vocx.core.atlas import AtlasStore

    app = VocxApp(store=AtlasStore({}), config={
        "google": {"tokens_dir": str(tmp_path)}, "thresholds": {}, "scores": {}})
    t = app._ticket_mint("ananda")
    assert app._ticket_pop(t) == "ananda"
    assert app._ticket_pop(t) is None            # single use
    t2 = app._ticket_mint("divya")
    # age it past the 300s window on disk
    import json as _j
    p = app._ticket_path()
    data = _j.load(open(p))
    data[t2]["ts"] -= 301
    _j.dump(data, open(p, "w"))
    assert app._ticket_pop(t2) is None           # expired


def test_judgement_confidence_and_bullets_normalize():
    """Field finding (stage VM): Haiku stamped 'medium' on
    competitive_intelligence through every repair round — a contract violation
    that burned all five retries. A judgement field's confidence coerces to the
    only legal value, and bulleted judgement prose joins to newline text."""
    def grading_model(model, system, user):
        obj = json.loads(_valid_model_json())
        obj["common"]["competitive_intelligence"] = {
            "value": ["Piramal quoted 11.75%", "another party in the room"],
            "confidence": "medium"}
        obj["common"]["opportunity_assessment"] = {
            "value": "Strong sponsor.", "confidence": "high"}
        return json.dumps(obj)

    row = PipelineRunner(FakeRegister(), good_transcribe, grading_model).process("c1")
    assert row["status"] == "ready"
    ci = row["structured_report"]["common"]["competitive_intelligence"]
    assert ci["confidence"] == "n/a"
    assert ci["value"] == "Piramal quoted 11.75%\nanother party in the room"
    oa = row["structured_report"]["common"]["opportunity_assessment"]
    assert oa == {"value": "Strong sponsor.", "confidence": "n/a"}


def test_tool_schema_pins_judgement_confidence():
    from app.vocx.spec import build_tool_schema
    sch = build_tool_schema()
    common = sch["properties"]["common"]["properties"]
    assert common["competitive_intelligence"]["properties"]["confidence"] == {"enum": ["n/a"]}
    assert common["meeting_summary"]["properties"]["confidence"] == {"enum": ["n/a"]}
    # ordinary fields keep the full ladder
    assert common["location"]["properties"]["confidence"] == {
        "enum": ["high", "medium", "low", "n/a"]}


def test_pipeline_workers_are_bounded(monkeypatch):
    """150 finishes at once must not mean 150 transcriptions at once: the
    semaphore holds work to VOCX_PIPELINE_WORKERS; the rest wait their turn.
    Every conversation still completes."""
    import threading
    import time as _t
    monkeypatch.setenv("VOCX_PIPELINE_WORKERS", "2")
    from app.vocx.core.atlas import AtlasStore
    from app.vocx.core.server import VocxApp

    app = VocxApp(store=AtlasStore({}), config={"thresholds": {}, "scores": {}})

    peak = {"now": 0, "max": 0, "done": 0}
    gate = threading.Lock()

    class SlowRunner:
        def process(self, cid):
            with gate:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
            _t.sleep(0.15)
            with gate:
                peak["now"] -= 1
                peak["done"] += 1

    app._vox_runner = SlowRunner()
    for i in range(6):
        code, _, _ = app._vox_process(json.dumps({"conversation_id": f"c{i}"}).encode())
        assert code == 202
    deadline = _t.time() + 5
    while peak["done"] < 6 and _t.time() < deadline:
        _t.sleep(0.05)
    assert peak["done"] == 6            # everyone finishes
    assert peak["max"] <= 2             # never more than the ceiling at once


def test_transcribe_budget_scales_with_the_take():
    """A 90-minute live take on CPU STT honestly needs more than the 3-minute
    note's allowance — the budget is duration-scaled, never below the floor."""
    seen = {}

    def slow_transcribe(audio_ref):
        return {"text": "ok", "segments": [{"text": "ok"}], "language": "en"}

    import app.vocx.pipeline.runner as runner_mod
    orig = runner_mod._run_with_timeout

    def spy(fn, stage, seconds):
        seen[stage] = seconds
        return orig(fn, stage, seconds)

    runner_mod._run_with_timeout = spy
    try:
        reg = FakeRegister(duration_seconds=5400)
        PipelineRunner(reg, slow_transcribe,
                       lambda m, s, u: _valid_model_json()).process("c1")
    finally:
        runner_mod._run_with_timeout = orig
    assert seen["transcribe"] == 5400 * 2 + 300      # scaled for the long take
    seen.clear()
    runner_mod._run_with_timeout = spy
    try:
        PipelineRunner(FakeRegister(duration_seconds=180), slow_transcribe,
                       lambda m, s, u: _valid_model_json()).process("c1")
    finally:
        runner_mod._run_with_timeout = orig
    assert seen["transcribe"] == 1800                # the floor holds for notes


# ------------------------------------------------------------ streamed capture

def test_segment_store_appends_reads_finalizes_and_caps(tmp_path):
    from app.vocx.speech.segment_store import SegmentStore, SegmentStoreError
    st = SegmentStore(str(tmp_path))
    got = st.append("cap-abc-123", 0, b"hello ", rm="Ananda H", mime="audio/webm",
                    mode="post_meeting", elapsed=5)
    assert got["bytes_total"] == 6
    st.append("cap-abc-123", 0, b"world", elapsed=9)
    st.append("cap-abc-123", 1, b"segment two", elapsed=20)   # a new browser session
    m = st.manifest("cap-abc-123")
    assert m["bytes_total"] == len(b"hello world") + len(b"segment two")
    assert m["elapsed"] == 20 and m["rm"] == "Ananda H"
    paths = st.segment_paths("cap-abc-123")
    assert [open(p, "rb").read() for p in paths] == [b"hello world", b"segment two"]
    # the caller's resume list finds it; a stranger's does not
    assert st.unfinished_for("ananda h")[0]["segments"] == 2
    assert st.unfinished_for("someone else") == []
    # finalize is idempotent and closes the door to further appends
    assert st.finalize("cap-abc-123") == paths
    assert st.finalize("cap-abc-123") == paths
    import pytest as _pt
    with _pt.raises(SegmentStoreError):
        st.append("cap-abc-123", 2, b"late")
    with _pt.raises(SegmentStoreError):
        st.append("../evil", 0, b"x")                       # id charset wall
    st.discard("cap-abc-123")
    assert st.manifest("cap-abc-123") is None


def test_stream_endpoints_append_list_audio_discard(tmp_path, monkeypatch):
    from app.vocx.core.atlas import AtlasStore
    from app.vocx.core.server import VocxApp
    monkeypatch.setenv("VOCX_SEGMENTS_DIR", str(tmp_path))
    app = VocxApp(store=AtlasStore({}), config={"thresholds": {}, "scores": {}})

    def post(path, q, body=b""):
        return app.handle("POST", path, {k: [v] for k, v in q.items()}, body)
    def get(path, q):
        return app.handle("GET", path, {k: [v] for k, v in q.items()}, b"")

    code, _, out = post("/v1/vox/stream", {"capture_id": "cap-xyz-1", "seg": "0",
                                           "rm": "Divya", "elapsed": "12"}, b"audio-bytes")
    assert code == 200 and json.loads(out)["bytes_total"] == 11
    code, _, out = get("/v1/vox/stream/unfinished", {"rm": "Divya"})
    takes = json.loads(out)["takes"]
    assert code == 200 and takes[0]["capture_id"] == "cap-xyz-1"
    code, ctype, data = get("/v1/vox/stream/audio", {"capture_id": "cap-xyz-1", "seg": "0"})
    assert code == 200 and data == b"audio-bytes"
    code, _, out = post("/v1/vox/stream/discard", {"capture_id": "cap-xyz-1"})
    assert code == 200
    code, _, out = get("/v1/vox/stream/unfinished", {"rm": "Divya"})
    assert json.loads(out)["takes"] == []
    # oversized batch refused with a name, storage untouched
    code, _, out = post("/v1/vox/stream", {"capture_id": "cap-xyz-2", "seg": "0"},
                        b"x" * (4 * 1024 * 1024 + 1))
    assert code == 413


def test_multi_segment_transcripts_merge_in_order(tmp_path):
    from app.vocx.speech.segment_store import SegmentStore, transcribe_segments
    st = SegmentStore(str(tmp_path))
    st.append("cap-mrg-1", 0, b"a")
    st.append("cap-mrg-1", 1, b"b")

    class EchoTranscriber:
        def transcribe(self, path, prompt=None, **kw):
            name = path.rsplit("/", 1)[-1]
            return {"text": f"text-of-{name}", "segments": [{"text": name}], "language": "en"}

    got = transcribe_segments(st.segment_paths("cap-mrg-1"), EchoTranscriber())
    assert got["text"] == "text-of-seg000.webm\ntext-of-seg001.webm"
    assert [s["text"] for s in got["segments"]] == ["seg000.webm", "seg001.webm"]
    assert got["language"] == "en"


def test_streamed_takes_belong_to_their_recorder(tmp_path, monkeypatch):
    """The capture id alone is not a key: another signed-in user presenting a
    stolen id is refused on append, playback, finish and discard alike."""
    from app.vocx.core.atlas import AtlasStore
    from app.vocx.core.server import VocxApp
    monkeypatch.setenv("VOCX_SEGMENTS_DIR", str(tmp_path))
    app = VocxApp(store=AtlasStore({}), config={"thresholds": {}, "scores": {}})

    def call(method, path, q, body=b""):
        return app.handle(method, path, {k: [v] for k, v in q.items()}, body)

    assert call("POST", "/v1/vox/stream",
                {"capture_id": "cap-own-1", "seg": "0", "rm": "Divya"}, b"abc")[0] == 200
    for method, path in [("POST", "/v1/vox/stream"),
                         ("GET", "/v1/vox/stream/audio"),
                         ("POST", "/v1/vox/stream/finish"),
                         ("POST", "/v1/vox/stream/discard")]:
        code, _, out = call(method, path,
                            {"capture_id": "cap-own-1", "seg": "0", "rm": "Chetan"}, b"x")
        assert code == 403, (path, out)
        assert b"another user" in out
    # the owner still passes everywhere
    assert call("GET", "/v1/vox/stream/audio",
                {"capture_id": "cap-own-1", "seg": "0", "rm": "Divya"})[0] == 200
