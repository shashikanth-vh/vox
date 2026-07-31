# VocX REST API — UI integration reference

Everything the PRISM/VOM front-end needs to build voice capture, the report card,
reports, playback, and Google Calendar — against the live backend. The temporary dev
console (`/vocx/v1/dev-ui`) is a working reference client for every call below.

## Base URL, auth, headers

All calls go **through the edge**:

```
https://<host>:8443/vocx/v1/...
```

| header | value | notes |
| --- | --- | --- |
| `X-Tenant` | `EVAM` | tenant selector (multi-tenant ready) |
| `Authorization` | `Bearer <token>` | required in the production posture (OIDC via the gateway); dev default posture accepts anonymous |
| `Content-Type` | `application/json` | for JSON bodies; raw audio posts its own type |

The gateway injects VocX's internal service key — the UI never handles it. The one
route exempt from auth is `GET /v1/auth/callback` (Google's redirect carries no bearer).

**Errors** are always JSON: `{"error": {"type", "title", "detail"}}` (gateway shape) or
`{"ok": false, "error": "..."}` (pipeline shape). Non-2xx ⇒ show `detail`/`error`.

## The UI flow in five calls

```
GET  capabilities                      → adapt the UI (STT? AI? Google? templates)
POST capture_audio | capture           → PREVIEW (extraction + report; never writes)
     …RM edits the report card…
POST reports/save                      → keep edits server-side (optional, repeatable)
POST commit                            → APPROVE: real writes (idempotent)
GET  reports / reports/get / print     → history, reopen, PDF
```

---

## 1 · Capabilities

`GET /v1/capabilities`

```json
{
  "ok": true,
  "stt": true, "stt_backend": "api",
  "extraction": "haiku",                  // or "offline_stub" (no ANTHROPIC_API_KEY)
  "audio_store": "s3",                    // s3 | local | off
  "google_configured": true,
  "calendar_enabled": true,
  "report_templates": [                   // drive the template-field chips from THIS
    {"id": "lending", "axis": "business_line", "label": "Lending",
     "fields": [{"key": "tenor", "label": "Tenor"},
                {"key": "sanction_stage", "label": "Sanction stage",
                 "type": "select", "options": ["", "Enquiry", "…"]}]}
  ]
}
```

Call once at app start; degrade the UI from it (no STT → open on "type instead"; no
Google → amber calendar banner).

## 2 · Capture (preview — never writes)

### Voice

`POST /v1/capture_audio?rm=<RM>&gps_lat=&gps_lng=&location=&language=&capture_id=`

Body: the **raw audio bytes** (any container the phone records — webm/opus, wav, m4a);
`Content-Type` = the blob's type. ≤ 25 MB (≈ the 90-second cap with headroom).
Query params:

| param | required | notes |
| --- | --- | --- |
| `rm` | ✓ | the capturing RM (shows as `performed_by` on the interaction) |
| `gps_lat`/`gps_lng`/`location` | – | browser geolocation; junk values are dropped, never fatal |
| `language` | – | assert the spoken language; otherwise Whisper detects it |
| `capture_id` | – | RE-USE an existing id (offline replay / re-record) — else minted |

Speech in **any language comes back as English text** (Whisper translate); the
*detected* spoken language is reported separately.

### Typed / re-analyze

`POST /v1/capture`
```json
{"rm": "Priya", "transcript": "any language…",
 "capture_id": "<existing id when RE-ANALYZING — updates the SAME draft>",
 "gps_lat": "12.9716", "gps_lng": "77.5946", "location": "Bengaluru"}
```

### Preview response (both)

```json
{
  "ok": true,
  "extraction": {
    "_meta": {"capture_id": "…", "rm": "Priya", "capture_ts": "…",
              "transcript": "raw English transcript", "transcript_ref": "s3://…wav",
              "language": "hi", "gps_lat": 12.97, "gps_lng": 77.59},
    "company_mentioned": "Adani Power",
    "entity_match": {"code": "ADANI", "canonical_name": "…", "match_score": 0.92,
                     "is_new_lead": false, "alternatives": [{"code": "…", "…": "…"}],
                     "reason": "confident_match"},
    "next_meeting": {"date": "2026-08-04", "time": "10:00", "mode": "video",
                     "confidence": 0.9},
    "register_signals": {"temp": "Warm", "business_line_hint": "syn"},
    "report": {
      "title": "…", "summary": "…", "transcript_english": "…",
      "key_intel": ["…"], "nuances": ["…"],
      "next_steps": [{"owner": "RM", "action": "…", "date": "2026-08-04"}],
      "attendees": [{"name": "…", "role": "…", "company": "…"}],
      "sector": "…", "project_type": "…", "project_size": "…", "location": "…",
      "loan_product": "…", "ticket_size": "…", "collateral": "…",
      "equity_raised": "…", "turnover": "…",
      "pipeline_stage": "Proposal", "opportunity_score": 4, "deal_temp": "Warm",
      "business_line": "syndication", "extra": {"tenor": "…"}, "_custom": []
    }
  },
  "decision": {"auto_write": false, "needs_approval": true, "…": "…"},
  "approval_card": {"…": "…"},
  "write_plan": [{"op": "atlas_append_interaction", "…": "…"}],
  "transcription": {"text": "…", "language": "hi", "duration": 40.2}
}
```

**The card binds to `extraction.report`** — every VOM field maps 1:1. Edit it in place;
whatever the UI sends back at commit is what gets written. Every preview also
**auto-saves a server-side DRAFT** under `capture_id` (a dead phone loses nothing).

## 3 · Company typeahead

`GET /v1/suggest?q=<typed>&rm=<RM>&limit=8` (min 2 chars)

```json
{"ok": true, "q": "adani",
 "matches": [{"code": "ADANI", "name": "Adani Power", "kind": "client",
              "ref_type": "Deal", "rm": "Priya", "score": 0.92}],
 "new_company": false}
```

Same scorer as capture-time resolution (incl. own-client boost via `rm`). When
`new_company` is true, offer "create *q* as a new company" → commit with
`new_lead: true` + `company`.

## 4 · Save edits (repeatable, pre-commit)

`POST /v1/reports/save`
```json
{"rm": "Priya", "capture_id": "…", "status": "ready",
 "report": {"extraction": {…the whole edited extraction…}, "summary": "…"}}
```
- lifecycle: `draft` (auto) → `ready` (saved) → `committed` (final; further saves **409**)
- 512 KB cap; last-write-wins.

## 5 · Commit (approve — the real writes)

`POST /v1/commit`
```json
{
  "rm": "Priya",
  "extraction": {…the edited extraction…},
  "capture_id": "…",                        // idempotency handle
  "summary": "…",                            // optional narrative override
  "chosen_code": "ADANI",                    // picked an ATLAS entity, or:
  "new_lead": true, "company": "NewCo",      // create as a new company
  "edits": {"date": "2026-08-04", "time": "10:00", "mode": "video", "temp": "Warm"},
  "log_to": {"subject_type": "Lending",      // optional explicit routing:
             "subject_id": "<uuid>"}         // Lead|Deal|Entity|Lending|Syndication|AssetMonetisation
}
```

Response:
```json
{"ok": true, "committed": true,
 "writes": {"ok": true, "results": [
   {"op": "atlas_create_lead", "status": "ok", "lead_no": "LD-V01"},
   {"op": "atlas_append_interaction", "status": "ok"},
   {"op": "calendar_create_event", "status": "skipped", "reason": "google_not_connected"}]}}
```

- **Idempotent**: Register writes carry `Idempotency-Key: vocx:<capture_id>:<op>` —
  a retried commit replays, never duplicates. Safe to retry on network failure.
- What lands on the Register interaction (structured columns): English `transcript`,
  `language`, GPS, `attendees`, `key_intel` (bullets/facts/nuances/template_fields),
  `next_steps`, `next_action(_date)`, `next_meeting_date`, `performed_by` = the RM,
  `source: "VOX"`, `source_ref` = capture_id, the recording as an `attachments` entry,
  `meta` (score/stage/temp/business line). `notes` = the lean narrative.
- Calendar: with the RM's Google connected and a `next_meeting.date`, a real event is
  created; otherwise the op reports `skipped` (the follow-up is still in the Register).

## 6 · Reports (server-side, per RM)

| call | notes |
| --- | --- |
| `GET /v1/reports?rm=` | list: `{capture_id, status, company, summary, updated_at}` |
| `GET /v1/reports/get?rm=&id=` | full doc — `report.extraction` reloads the card |
| `GET /v1/reports/print?rm=&id=` | **print-ready HTML** — open in a tab; browser print = the PDF |
| `POST /v1/reports/save` | see §4 |
| `POST /v1/reports/delete` | body `{rm, capture_id}` |

## 7 · Audio playback

`GET /v1/audio?ref=<extraction._meta.transcript_ref>`

Default: **streams the audio bytes** through VocX (HTTPS, auth'd — feed to
`<audio src>` via a blob URL). If the deployment opts into presigning
(`VOCX_AUDIO_PRESIGN=true`) the response is `{"ok": true, "url": "<presigned>"}` —
handle both:

```js
const r = await fetch(url);
if ((r.headers.get('content-type') || '').startsWith('audio/'))
  audio.src = URL.createObjectURL(await r.blob());
else audio.src = (await r.json()).url;
```

Refs outside the captures bucket/prefix are refused.

## 8 · Template auto-fill

`POST /v1/template_fill`
```json
{"transcript": "…", "fields": [{"key": "tenor", "label": "Tenor"}, …]}
→ {"ok": true, "values": {"tenor": "5 years", "pricing": null}}
```
Merge `values` into `report.extra`. Answers `{"ok": false, "error": "no_api_key"}`
when Claude isn't configured — disable the ✨ button then.

## 9 · Google Calendar (per-RM, one-time)

| call | notes |
| --- | --- |
| `GET /v1/auth/status?rm=` | `{connected: bool}` |
| `GET /v1/auth/start?rm=&go=1` | **open in a browser/webview** — Google sign-in + consent; the callback stores the refresh token server-side |
| `GET /v1/auth/callback` | Google's redirect target (gateway-exempt; not called by the UI) |
| `GET /v1/calendar/test?rm=` | creates a probe event; returns which calendar |

## 10 · Interaction log (read side)

| call | notes |
| --- | --- |
| `GET /v1/interactions?company=&user=&type=&q=&from=&to=&limit=&offset=&sort=` | search |
| `GET /v1/facets` (same filters) | counts by company/type/user |
| `GET /v1/entity?code=` | one entity + its interactions |
| `GET /v1/interaction_types` | vocabulary for dropdowns |

---

## Endpoint index

```
GET  /vocx/v1/capabilities        POST /vocx/v1/capture            POST /vocx/v1/capture_audio
POST /vocx/v1/commit              GET  /vocx/v1/suggest            POST /vocx/v1/template_fill
GET  /vocx/v1/reports             GET  /vocx/v1/reports/get        GET  /vocx/v1/reports/print
POST /vocx/v1/reports/save        POST /vocx/v1/reports/delete     GET  /vocx/v1/audio
GET  /vocx/v1/interactions        GET  /vocx/v1/facets             GET  /vocx/v1/entity
GET  /vocx/v1/interaction_types   GET  /vocx/v1/auth/status        GET  /vocx/v1/auth/start
GET  /vocx/v1/auth/callback       GET  /vocx/v1/calendar/test
```

Postman: `postman/PRISM_VOCX.postman_collection.json` (this flow, runnable in BOTH
postures — request 00 signs in via Dex when the environment sets `dexUrl` and skips
itself in the dev posture, exactly like the E2E journey) and the
all-APIs collection (every operation from OpenAPI). Reference client: the dev console
at `/vocx/v1/dev-ui` (`VOCX_DEV_UI=true`) implements every call in this document.
