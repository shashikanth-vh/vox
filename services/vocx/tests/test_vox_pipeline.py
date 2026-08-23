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
