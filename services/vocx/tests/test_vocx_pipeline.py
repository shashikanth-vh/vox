"""The vendored VOX pipeline, wired to PRISM: offline extraction → Register-backed
resolution → gate → commit through the REAL writer against a stubbed Register API.

No Anthropic key, no Google, no STT model, no live Register — the pipeline must
degrade exactly as documented: offline stub extraction, register-only writes,
calendar ops reported as skipped.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app
from app.vocx.core.atlas import AtlasStore
from app.vocx.registry import store as register_store
from app.vocx.registry import writer as register_writer

pytestmark = pytest.mark.asyncio

ENTITY_ID = str(uuid.uuid4())
LEAD_ID = str(uuid.uuid4())


def _register_stub(state: dict):
    """A fake Register API: serves the corpus, records the writes (and their headers)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            state.setdefault("idempotency_keys", []).append(
                request.headers.get("Idempotency-Key"))
        if request.method == "GET" and path == "/v1/entities":
            return httpx.Response(200, json={"items": [{
                "id": ENTITY_ID, "code": "ECOSOCH", "legal_name": "EcoSoch Solar Private Limited",
                "display_name": "EcoSoch Solar", "sector": "Solar - Developer",
                "lens": "Mitigation", "state": "Karnataka"}], "next_cursor": None})
        if request.method == "GET" and path == "/v1/leads":
            return httpx.Response(200, json={"items": [{
                "id": LEAD_ID, "lead_no": "LD-V01", "company": "GH2 Hydrogen",
                "sector": "Green Hydrogen", "rm": "Priya", "status": "Active",
                "entity_id": None}], "next_cursor": None})
        if request.method == "GET" and path == "/v1/deals":
            return httpx.Response(200, json={"items": [{
                "id": str(uuid.uuid4()), "entity_id": ENTITY_ID, "rm": "Priya",
                "code": "DL-1"}], "next_cursor": None})
        if request.method == "GET" and path == "/v1/ref":
            return httpx.Response(200, json={"interaction_types": [
                {"value": "In-Person Meeting"}, {"value": "Phone Call"}]})
        if request.method == "GET" and "/interactions" in path:
            return httpx.Response(200, json={"items": []})
        if request.method == "POST" and path == "/v1/interactions":
            body = json.loads(request.content)
            state.setdefault("interactions", []).append(body)
            return httpx.Response(201, json={"id": str(uuid.uuid4()), **body})
        if request.method == "POST" and path == "/v1/leads":
            body = json.loads(request.content)
            state.setdefault("leads", []).append(body)
            return httpx.Response(201, json={"id": str(uuid.uuid4()), **body})
        return httpx.Response(404, json={"error": "unhandled " + path})

    return httpx.MockTransport(handler)


@pytest.fixture()
def stub_register(monkeypatch, tmp_path):
    state: dict = {}
    transport = _register_stub(state)
    real_client = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(register_store.httpx, "Client", patched)
    monkeypatch.setattr(register_writer.httpx, "Client", patched)
    monkeypatch.setenv("VOCX_TOKENS_DIR", str(tmp_path / "vox"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    yield state
    get_settings.cache_clear()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_capture_preview_resolves_against_the_register(stub_register):
    app = create_app()
    async with await _client(app) as c:
        r = await c.post("/v1/capture", json={
            "rm": "Priya",
            "transcript": "Met the EcoSoch Solar team about the 45 crore term loan. "
                          "Schedule a follow-up meeting next Monday at 3pm.",
            "offline": True})
    assert r.status_code == 200, r.text
    body = r.json()
    em = body["extraction"]["entity_match"]
    # The corpus came from the (stubbed) REGISTER, not a fixture file.
    assert em["code"] == "ECOSOCH", em
    assert em["ref_type"] == "Deal"
    ops = [o["op"] for o in body["write_plan"]]
    assert "atlas_append_interaction" in ops
    # Preview never writes.
    assert "interactions" not in stub_register


async def test_commit_writes_the_interaction_to_the_register(stub_register):
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "transcript": "Met the EcoSoch Solar team about the term loan."})
        ext = prev.json()["extraction"]
        r = await c.post("/v1/commit", json={
            "rm": "Priya", "extraction": ext, "chosen_code": "ECOSOCH",
            "summary": "Term-loan discussion"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is True, body
    written = stub_register["interactions"]
    assert len(written) == 1
    # The client CODE was translated to the Register entity row.
    assert written[0]["subject_type"] == "Entity"
    assert written[0]["subject_id"] == ENTITY_ID
    assert written[0]["interaction_type"]
    # Calendar was not connected — reported as skipped, never an error.
    cal = [x for x in body["writes"]["results"] if x["op"] == "calendar_create_event"]
    assert all(x["status"] == "skipped" for x in cal)


async def test_commit_new_lead_creates_a_register_lead(stub_register):
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "transcript": "First meeting with Windward Renewables about a 20 crore loan."})
        ext = prev.json()["extraction"]
        r = await c.post("/v1/commit", json={
            "rm": "Priya", "extraction": ext, "new_lead": True,
            "company": "Windward Renewables", "summary": "Intro meeting"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is True, body
    leads = stub_register["leads"]
    assert len(leads) == 1
    assert leads[0]["company"] == "Windward Renewables"
    assert leads[0]["lead_no"].startswith("LD-V")
    # The interaction landed on the freshly created lead (minted id translated to the row).
    inter = stub_register["interactions"][0]
    assert inter["subject_type"] == "Lead"


async def test_capabilities_reports_degraded_but_honest(stub_register):
    app = create_app()
    async with await _client(app) as c:
        r = await c.get("/v1/capabilities")
    assert r.status_code == 200
    caps = r.json()
    assert caps["extraction"] == "offline_stub"        # no ANTHROPIC_API_KEY in tests
    assert caps["google_configured"] is False          # no client secret mounted
    assert "stt" in caps


async def test_register_store_blob_shape(stub_register):
    loader = register_store.RegisterStoreLoader("http://register", "k", "EVAM", ttl_s=0)
    store = loader.store()
    assert isinstance(store, AtlasStore)
    assert "ECOSOCH" in store.clients
    assert store.clients["ECOSOCH"]["_register_entity_id"] == ENTITY_ID
    assert store.rm_for_client("ECOSOCH") == "Priya"          # via the deal
    cands = {c.name for c in store.candidates()}
    assert {"EcoSoch Solar", "GH2 Hydrogen"} <= cands
    assert store.next_vox_lead_id("LD-V", 2) == "LD-V02"      # increments past LD-V01


async def test_commit_is_idempotent_by_capture_id(stub_register):
    """The same approved capture, committed twice (client retry), must carry the SAME
    Idempotency-Key on its Register writes — the Register then replays, not duplicates."""
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "transcript": "Met the EcoSoch Solar team about the term loan."})
        ext = prev.json()["extraction"]
        cid = ext["_meta"]["capture_id"]
        assert cid, "preview must mint a capture id"
        for _ in range(2):
            r = await c.post("/v1/commit", json={
                "rm": "Priya", "extraction": ext, "chosen_code": "ECOSOCH",
                "summary": "Term-loan discussion"})
            assert r.status_code == 200 and r.json()["committed"], r.text
    keys = [k for k in stub_register["idempotency_keys"] if k]
    assert keys and all(k == f"vocx:{cid}:interaction" for k in keys), keys
    assert len(set(keys)) == 1


async def test_oversized_inputs_are_refused(stub_register):
    app = create_app()
    async with await _client(app) as c:
        r = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True, "transcript": "x" * 40_001})
        assert r.status_code == 413
        r2 = await c.post("/v1/capture_audio?rm=Priya",
                          content=b"x" * (25 * 1024 * 1024 + 1))
        assert r2.status_code == 413


# --------------------------------------------------------------------------- #
# Recorded audio → MinIO/S3 (references on the interaction)
# --------------------------------------------------------------------------- #
class _FakeS3:  # noqa: N801 — mirrors boto3's CamelCase kwargs
    # boto3's S3 API uses CamelCase keyword arguments; the fake must match.
    def __init__(self, fail_puts=False):
        self.objects = {}
        self.fail_puts = fail_puts
        self.lifecycle = None

    def head_bucket(self, Bucket):  # noqa: N803
        return {}

    def get_object(self, Bucket, Key):  # noqa: N803
        import io as _io
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _io.BytesIO(self.objects[Key])}

    def create_bucket(self, Bucket):  # noqa: N803
        return {}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):  # noqa: N803
        self.lifecycle = LifecycleConfiguration

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        if self.fail_puts:
            raise RuntimeError("minio down")
        self.objects[Key] = Body


def _s3_store(tmp_path, fake):
    from app.vocx.speech.audio_store import LocalAudioStore, S3AudioStore
    store = S3AudioStore(bucket="caps", endpoint_url="http://minio:9000",
                         access_key_id="k", secret_access_key="s",
                         retention_days=30,
                         fallback=LocalAudioStore(str(tmp_path / "captures")))
    store._client = fake
    fake and store._apply_lifecycle(fake)
    return store


def test_s3_audio_store_puts_and_returns_the_reference(tmp_path):
    fake = _FakeS3()
    store = _s3_store(tmp_path, fake)
    ref = store.save(b"RIFFdata", "2026-07-30T15:02:11", "Priya")
    assert ref == "s3://caps/captures/2026/07/20260730_150211_Priya.wav"
    assert fake.objects[ref.split("caps/")[1]] == b"RIFFdata"
    # Retention became a bucket lifecycle rule, not an app cron.
    assert fake.lifecycle["Rules"][0]["Expiration"]["Days"] == 30


def test_s3_failure_degrades_to_the_volume_never_discards(tmp_path):
    store = _s3_store(tmp_path, _FakeS3(fail_puts=True))
    ref = store.save(b"RIFFdata", "2026-07-30T15:02:11", "Priya")
    assert ref and ref.startswith(str(tmp_path))          # local fallback path
    with open(ref, "rb") as fh:
        assert fh.read() == b"RIFFdata"


async def test_committed_interaction_carries_the_recording_reference(stub_register, monkeypatch):
    """capture(audio_b64, stub STT) stores the audio and the COMMIT's interaction row
    carries it as a first-class ATTACHMENT — the register row points back at what
    was said without polluting the RM's notes."""
    import base64

    from app.vocx import mount as mount_mod
    from app.vocx.speech import stt as vocx_stt
    monkeypatch.setenv("VOCX_STT_BACKEND", "stub")
    monkeypatch.setattr(vocx_stt, "build_transcriber", lambda config: vocx_stt.StubTranscriber(
        "Met the EcoSoch Solar team about the term loan."))
    get_settings.cache_clear()
    fake = _FakeS3()

    real_build = mount_mod.build_audio_store

    def patched_build(settings):
        store = real_build(settings)
        from app.vocx.speech.audio_store import S3AudioStore
        if not isinstance(store, S3AudioStore):
            store = S3AudioStore(bucket="caps", endpoint_url="http://minio:9000",
                                 access_key_id="k", secret_access_key="s",
                                 fallback=None)
        store._client = fake
        return store

    monkeypatch.setattr(mount_mod, "build_audio_store", patched_build)
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "audio_b64": base64.b64encode(b"RIFFfakeaudio").decode(),
            "capture_ts": "2026-07-30T15:02:11"})
        assert prev.status_code == 200, prev.text
        ext = prev.json()["extraction"]
        ref = ext["_meta"].get("transcript_ref")
        assert ref and ref.startswith("s3://caps/captures/"), ext["_meta"]
        assert fake.objects                                  # bytes actually landed
        r = await c.post("/v1/commit", json={
            "rm": "Priya", "extraction": ext, "chosen_code": "ECOSOCH",
            "summary": "From audio"})
    assert r.status_code == 200 and r.json()["committed"], r.text
    inter = stub_register["interactions"][-1]
    assert inter["attachments"] == [{"name": "recording.wav", "ref": ref,
                                     "content_type": "audio/wav"}]
    assert "Recording:" not in (inter.get("notes") or "")


# --------------------------------------------------------------------------- #
# Server-side reports, playback, and explicit Log-To
# --------------------------------------------------------------------------- #
async def test_preview_autosaves_a_draft_and_reports_flow(stub_register, tmp_path, monkeypatch):
    """capture → a DRAFT exists server-side; save marks it ready; commit marks it
    committed (and a further save is refused); delete removes it."""
    monkeypatch.setenv("VOCX_TOKENS_DIR", str(tmp_path / "vocx"))
    get_settings.cache_clear()
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "transcript": "Met the EcoSoch Solar team about the term loan."})
        ext = prev.json()["extraction"]
        cid = ext["_meta"]["capture_id"]

        lst = (await c.get("/v1/reports", params={"rm": "Priya"})).json()
        assert [r["capture_id"] for r in lst["reports"]] == [cid]
        assert lst["reports"][0]["status"] == "draft"

        saved = await c.post("/v1/reports/save", json={
            "rm": "Priya", "capture_id": cid, "status": "ready",
            "report": {"extraction": ext, "summary": "edited by the RM"}})
        assert saved.status_code == 200 and saved.json()["report"]["status"] == "ready"

        r = await c.post("/v1/commit", json={
            "rm": "Priya", "extraction": ext, "chosen_code": "ECOSOCH",
            "capture_id": cid, "summary": "Term-loan discussion"})
        assert r.status_code == 200 and r.json()["committed"], r.text

        doc = (await c.get("/v1/reports/get", params={"rm": "Priya", "id": cid})).json()
        assert doc["report"]["status"] == "committed"
        assert doc["report"]["writes"]["ok"] is True
        refused = await c.post("/v1/reports/save", json={
            "rm": "Priya", "capture_id": cid, "report": {}})
        assert refused.status_code == 409                      # committed is final

        assert (await c.post("/v1/reports/delete", json={
            "rm": "Priya", "capture_id": cid})).status_code == 200
        assert (await c.get("/v1/reports", params={"rm": "Priya"})).json()["reports"] == []


async def test_audio_playback_presigns_own_bucket_only(stub_register, monkeypatch, tmp_path):
    from app.vocx.speech.audio_store import S3AudioStore

    class _Signer:
        def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
            return f"http://public-minio/{Params['Bucket']}/{Params['Key']}?sig=x&exp={ExpiresIn}"

    store = S3AudioStore(bucket="caps", endpoint_url="http://minio:9000",
                         access_key_id="k", secret_access_key="s", presign=True)
    monkeypatch.setattr(store, "_public_s3", lambda: _Signer())
    kind, url = store.playback("s3://caps/captures/2026/07/x.wav")
    assert kind == "url" and url.startswith("http://public-minio/caps/captures/")
    # Foreign bucket or prefix → refused, never presigned.
    assert store.playback("s3://prism-documents/secret.pdf") is None
    assert store.playback("s3://caps/reports/Priya/r.json") is None


async def test_local_playback_refuses_path_traversal(tmp_path):
    from app.vocx.speech.audio_store import LocalAudioStore
    store = LocalAudioStore(str(tmp_path / "captures"))
    ref = store.save(b"RIFFdata", "2026-07-30T15:02:11", "Priya")
    kind, data = store.playback(ref)
    assert kind == "bytes" and data == b"RIFFdata"
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    assert store.playback(str(outside)) is None


async def test_commit_log_to_targets_the_chosen_line(stub_register):
    lending_id = str(uuid.uuid4())
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "transcript": "Met the EcoSoch Solar team about the term loan."})
        ext = prev.json()["extraction"]
        bad = await c.post("/v1/commit", json={
            "rm": "Priya", "extraction": ext, "chosen_code": "ECOSOCH",
            "log_to": {"subject_type": "Bogus", "subject_id": lending_id}})
        assert bad.status_code == 400
        r = await c.post("/v1/commit", json={
            "rm": "Priya", "extraction": ext, "chosen_code": "ECOSOCH",
            "log_to": {"subject_type": "Lending", "subject_id": lending_id}})
    assert r.status_code == 200 and r.json()["committed"], r.text
    inter = stub_register["interactions"][-1]
    assert inter["subject_type"] == "Lending"
    assert inter["subject_id"] == lending_id


async def test_dev_ui_hidden_unless_enabled(stub_register, monkeypatch):
    # Default posture: the flag is off and the route does not exist at all.
    app = create_app()
    async with await _client(app) as c:
        r = await c.get("/v1/dev-ui")
    assert r.status_code == 404

    monkeypatch.setenv("VOCX_DEV_UI", "true")
    get_settings.cache_clear()
    app = create_app()
    async with await _client(app) as c:
        r = await c.get("/v1/dev-ui")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/html")
        assert "dev test console" in r.text
        # Dev tool, never a documented API: it must stay out of the OpenAPI schema
        # (and therefore out of every generated Postman collection).
        spec = (await c.get("/openapi.json")).json()
        assert "/v1/dev-ui" not in spec["paths"]


async def test_api_transcriber_calls_the_stt_service(monkeypatch):
    """The api backend speaks the STT service's contract: OpenAI-style multipart out,
    transcript JSON back, bearer key from env. (The service side of this contract is
    pinned in services/stt/tests.)"""
    from app.vocx.speech.stt import APITranscriber

    seen = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        seen.update(url=url, headers=headers or {}, data=data or {}, files=files or {})
        return httpx.Response(200, json={"text": "Met the EcoSoch team.", "language": "en",
                                         "duration": 3.2, "segments": []},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("VOCX_STT_API_URL", "http://stt:8000/v1/audio/transcriptions")
    monkeypatch.setenv("VOCX_STT_API_KEY", "k1")
    out = APITranscriber().transcribe(b"fake-bytes", language="en")
    assert out["text"] == "Met the EcoSoch team."
    assert out["backend"] == "api"
    assert seen["url"].endswith("/v1/audio/transcriptions")
    assert seen["headers"]["Authorization"] == "Bearer k1"
    assert seen["data"]["language"] == "en"
    # English-at-rest: any-language capture is translated by Whisper before storage.
    assert seen["data"]["task"] == "translate"
    assert seen["files"]["file"][1] == b"fake-bytes"


async def test_report_print_view(stub_register, tmp_path, monkeypatch):
    """/v1/reports/print renders the stored report as print-ready HTML (the PoC's
    'Download PDF' is the browser printing this view) and escapes content."""
    monkeypatch.setenv("VOCX_TOKENS_DIR", str(tmp_path / "vox"))
    get_settings.cache_clear()
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "transcript": "Met the EcoSoch Solar team about the <b>term loan</b>."})
        cid = prev.json()["extraction"]["_meta"]["capture_id"]
        r = await c.get(f"/v1/reports/print?rm=Priya&id={cid}")
        missing = await c.get("/v1/reports/print?rm=Priya&id=nope-123")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "EVAM FIELD INTEL" in r.text
    assert "<b>term loan</b>" not in r.text          # HTML in the transcript is escaped
    assert missing.status_code == 404


async def test_committed_interaction_lands_in_structured_columns(stub_register):
    """The VOM card's chips/fields land in the Register's STRUCTURED interaction
    columns — transcript, key_intel, next_steps, attendees, next_meeting_date, GPS,
    provenance (source/source_ref) and the CAPTURING RM as performed_by — while
    notes stays the lean human narrative."""
    app = create_app()
    async with await _client(app) as c:
        prev = await c.post("/v1/capture", json={
            "rm": "Priya", "offline": True,
            "gps_lat": "12.9716", "gps_lng": "77.5946", "location": "Bengaluru",
            "transcript": "Met the EcoSoch Solar team about the 45 crore term loan. "
                          "Schedule a follow-up meeting next Monday at 3pm."})
        ext = prev.json()["extraction"]
        cid = ext["_meta"]["capture_id"]
        r = await c.post("/v1/commit", json={
            "rm": "Priya", "extraction": ext, "chosen_code": "ECOSOCH"})
    assert r.status_code == 200 and r.json()["committed"], r.text
    inter = stub_register["interactions"][-1]
    # Provenance + actor: the capturing RM, not the service key; source is VOX.
    assert inter["performed_by"] == "Priya"
    assert inter["source"] == "VOX"
    assert inter["source_ref"] == cid
    # Capture facts in their own columns.
    assert "EcoSoch" in inter["transcript"]
    assert inter["gps_lat"] == pytest.approx(12.9716)
    assert inter["gps_lng"] == pytest.approx(77.5946)
    assert inter["location"] == "Bengaluru"
    assert inter["next_meeting_date"], inter
    # Structured intel block + meta ride as JSON, not prose.
    assert inter["key_intel"]["bullets"], inter["key_intel"]
    assert inter["meta"]["capture_id"] == cid
    # Notes is the lean narrative: no KEY INTEL / NEXT STEPS dumps.
    assert "KEY INTEL" not in inter["notes"]
    assert "captured by Priya via VocX" in inter["notes"]


async def test_suggest_typeahead_matches_and_new_company(stub_register):
    """/v1/suggest ranks the live corpus for a partial name; an unknown name answers
    new_company=true so the UI can offer 'create as new company'."""
    app = create_app()
    async with await _client(app) as c:
        hit = await c.get("/v1/suggest?q=EcoSoch&rm=Priya")
        miss = await c.get("/v1/suggest?q=Totally Unknown Ventures")
        short = await c.get("/v1/suggest?q=E")
    assert hit.status_code == 200, hit.text
    body = hit.json()
    assert body["matches"] and body["matches"][0]["code"] == "ECOSOCH"
    assert body["new_company"] is False
    assert miss.json()["new_company"] is True
    assert short.status_code == 400


async def test_s3_playback_streams_bytes_by_default(tmp_path):
    """Playback default = bytes THROUGH VocX (https/auth safe everywhere); presign is
    opt-in and still refuses foreign refs."""
    from app.vocx.speech.audio_store import S3AudioStore
    fake = _FakeS3()
    store = S3AudioStore(bucket="caps", endpoint_url="http://minio:9000",
                         access_key_id="k", secret_access_key="s", fallback=None)
    store._client = fake
    ref = store.save(b"RIFFdata", "2026-07-31T10:00:00", "Priya")
    got = store.playback(ref)
    assert got is not None and got[0] == "bytes" and got[1] == b"RIFFdata"
    # Foreign refs stay refused in streaming mode too.
    assert store.playback("s3://other-bucket/captures/x.wav") is None


def test_audio_name_defaults_to_now_when_no_timestamp():
    """No capture_ts must NOT collapse to a shared 'capture_<rm>' key — that would make
    each new capture silently overwrite the RM's previous recording."""
    import time

    from app.vocx.speech.audio_store import _safe_name
    yyyy, mm, name = _safe_name("", "Priya")
    assert yyyy == time.strftime("%Y", time.gmtime())
    assert mm == time.strftime("%m", time.gmtime())
    assert name != "capture_Priya.wav" and name.endswith("_Priya.wav")


def test_system_prompt_carries_glossary_and_examples():
    """Batch-1 intelligence: the extraction prompt embeds the Evam glossary and the
    few-shot worked examples from config.json (editable without code changes)."""
    from app.vocx.core.extract import EXTRACTION_SCHEMA, build_system_prompt
    from app.vocx.core.resolve import load_config
    sp = build_system_prompt(load_config())
    assert "EVAM DOMAIN GLOSSARY" in sp
    assert "Information Memorandum" in sp          # jargon expanded
    assert "WORKED EXAMPLES" in sp and "GreenVolt" in sp
    assert build_system_prompt({}) != sp           # config-driven, not hardcoded
    assert EXTRACTION_SCHEMA["required"] == ["company_mentioned", "report"]


async def test_stt_priming_prompt_carries_vocabulary_and_corpus_names(stub_register, monkeypatch):
    """capture_audio primes the transcriber with finance vocabulary + the LIVE corpus
    names (names last — Whisper reads only the prompt's tail)."""
    from app.vocx.speech import stt as vocx_stt
    seen = {}

    class _Rec(vocx_stt.StubTranscriber):
        def transcribe(self, audio, language=None, prompt=None):
            seen["prompt"] = prompt
            return super().transcribe(audio, language)

    monkeypatch.setenv("VOCX_STT_BACKEND", "stub")
    monkeypatch.setattr(vocx_stt, "build_transcriber",
                        lambda config: _Rec("Met the EcoSoch Solar team."))
    get_settings.cache_clear()
    app = create_app()
    async with await _client(app) as c:
        r = await c.post("/v1/capture_audio?rm=Priya", content=b"RIFFfake",
                         headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    prompt = seen["prompt"] or ""
    assert "crore" in prompt                        # config vocabulary
    assert "EcoSoch" in prompt                      # live Register corpus name
    assert prompt.index("crore") < prompt.index("EcoSoch")   # names LAST


async def test_intelligence_features_are_independently_switchable(stub_register, monkeypatch):
    """Each Batch-1 feature has its own env flag; disabling one reverts exactly that
    feature to pre-Batch-1 behaviour."""
    from app.vocx.core.extract import build_system_prompt
    from app.vocx.loader import build_vox_config
    from app.vocx.speech import stt as vocx_stt

    # Flags OFF → glossary/examples leave the prompt; priming prompt becomes None.
    monkeypatch.setenv("VOCX_EXTRACT_GLOSSARY", "false")
    monkeypatch.setenv("VOCX_EXTRACT_FEW_SHOT", "false")
    monkeypatch.setenv("VOCX_EXTRACT_STRUCTURED", "false")
    monkeypatch.setenv("VOCX_STT_PRIMING", "false")
    monkeypatch.setenv("VOCX_STT_BACKEND", "stub")
    get_settings.cache_clear()
    cfg = build_vox_config(get_settings())
    assert cfg["intelligence"] == {"stt_priming": False, "glossary": False,
                                   "few_shot": False, "structured_output": False}
    sp = build_system_prompt(cfg)
    assert "EVAM DOMAIN GLOSSARY" not in sp and "WORKED EXAMPLES" not in sp

    seen = {}

    class _Rec(vocx_stt.StubTranscriber):
        def transcribe(self, audio, language=None, prompt=None):
            seen["prompt"] = prompt
            return super().transcribe(audio, language)

    monkeypatch.setattr(vocx_stt, "build_transcriber",
                        lambda config: _Rec("Met the EcoSoch Solar team."))
    app = create_app()
    async with await _client(app) as c:
        r = await c.post("/v1/capture_audio?rm=Priya", content=b"RIFFfake",
                         headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    assert seen["prompt"] is None                    # priming disabled

    # Flags back ON (defaults) → everything returns.
    for var in ("VOCX_EXTRACT_GLOSSARY", "VOCX_EXTRACT_FEW_SHOT",
                "VOCX_EXTRACT_STRUCTURED", "VOCX_STT_PRIMING"):
        monkeypatch.delenv(var)
    get_settings.cache_clear()
    cfg = build_vox_config(get_settings())
    sp = build_system_prompt(cfg)
    assert "EVAM DOMAIN GLOSSARY" in sp and "WORKED EXAMPLES" in sp


# ---------------------------------------------------------------------------
# Follow-up meeting capture: "schedule followup meeting next monday 11am"
# ---------------------------------------------------------------------------

def _null_meeting_client(captured: dict):
    """A fake Anthropic client whose extraction leaves next_meeting empty — the
    exact failure the deterministic backfill exists to catch."""
    from types import SimpleNamespace

    class _Msgs:
        def create(self, **kw):
            captured.update(kw)
            block = SimpleNamespace(type="tool_use", input={
                "company_mentioned": "Biomas Energy Systems",
                "report": {"title": "Biomas Energy Systems"},
                "next_meeting": {"date": None, "time": None, "mode": None,
                                 "confidence": 0.0},
            })
            return SimpleNamespace(content=[block])

    return SimpleNamespace(messages=_Msgs())


async def test_followup_backfill_resolves_next_monday_when_model_returns_null():
    """A plainly stated follow-up must land in next_meeting even when the model
    fails to resolve the relative date — resolved deterministically against
    capture_ts, at a confidence that clears the gate (no approval block)."""
    from app.vocx.core import extract as vocx_extract

    captured: dict = {}
    ext = vocx_extract.extract(
        "Met the Biomas Energy Systems team about the machinery loan. "
        "Schedule followup meeting next monday 11am.",
        capture_ts="2026-08-01T10:00:00", rm="Priya",
        client=_null_meeting_client(captured), config={},
    )
    nm = ext["next_meeting"]
    assert nm["date"] == "2026-08-03"          # 2026-08-01 is a Saturday
    assert nm["time"] == "11:00"
    assert nm["confidence"] >= 0.70            # >= VOX_DATE_CONF_MIN
    # And the model is TOLD the weekday, so it can resolve relative dates itself.
    assert "(a Saturday)" in captured["messages"][0]["content"]


async def test_followup_backfill_never_invents_a_meeting_from_a_deadline():
    """'they will share financials on Friday' is a commitment, not a meeting —
    without a scheduling phrase the backfill must leave next_meeting empty."""
    from app.vocx.core import extract as vocx_extract

    ext = vocx_extract.extract(
        "They will share the audited financials on friday.",
        capture_ts="2026-08-01T10:00:00", rm="Priya",
        client=_null_meeting_client({}), config={},
    )
    assert ext["next_meeting"]["date"] is None


async def test_followup_backfill_fills_missing_time_and_mode_next_to_a_model_date():
    """When the model resolved the date but dropped the time/mode, the backfill
    completes them from the transcript instead of leaving the card half-empty."""
    from types import SimpleNamespace

    from app.vocx.core import extract as vocx_extract

    class _Msgs:
        def create(self, **kw):
            block = SimpleNamespace(type="tool_use", input={
                "company_mentioned": "Biomas Energy Systems",
                "report": {"title": "Biomas Energy Systems"},
                "next_meeting": {"date": "2026-08-03", "time": None, "mode": None,
                                 "confidence": 0.9},
            })
            return SimpleNamespace(content=[block])

    ext = vocx_extract.extract(
        "Schedule a follow-up video call next monday 11am.",
        capture_ts="2026-08-01T10:00:00", rm="Priya",
        client=SimpleNamespace(messages=_Msgs()), config={},
    )
    nm = ext["next_meeting"]
    assert nm["date"] == "2026-08-03" and nm["time"] == "11:00"
    assert nm["mode"] == "video"


async def test_stub_relative_date_and_time_resolvers():
    from app.vocx.core.extract import _stub_next_date, _stub_time

    ts = "2026-08-01T10:00:00"                       # a Saturday
    assert _stub_next_date("followup meeting next monday 11am", ts)[0] == "2026-08-03"
    assert _stub_next_date("let us meet this monday", ts)[0] == "2026-08-03"
    assert _stub_next_date("meet the day after tomorrow", ts)[0] == "2026-08-03"
    assert _stub_next_date("review on the 29th", ts)[0] == "2026-08-29"
    assert _stub_next_date("review on the 1st", ts)[0] == "2026-09-01"   # past -> next month
    assert _stub_next_date("no date here at all", ts) == (None, 0.0)
    assert _stub_time("meet at 11am") == "11:00"
    assert _stub_time("around 3:30 p.m.") == "15:30"
    assert _stub_time("meet at 4 o'clock") == "16:00"
    assert _stub_time("meet at 14:30") == "14:30"
    assert _stub_time("no time stated") is None


async def test_offline_capture_populates_next_meeting_end_to_end(stub_register):
    """The user's exact phrase, through /v1/capture: the review payload carries the
    resolved follow-up date + time so the VOM card's NEXT MEETING is filled."""
    app = create_app()
    async with await _client(app) as c:
        r = await c.post("/v1/capture", json={
            "rm": "Priya",
            "transcript": "Met the EcoSoch Solar team about the machinery loan. "
                          "Schedule followup meeting next monday 11am.",
            "capture_ts": "2026-08-01T10:00:00",
            "offline": True})
    assert r.status_code == 200, r.text
    nm = r.json()["extraction"]["next_meeting"]
    assert nm["date"] == "2026-08-03"
    assert nm["time"] == "11:00"
