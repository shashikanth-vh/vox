#!/usr/bin/env python3
"""Generate the VocX voice-pipeline Postman collection (via the NGINX edge).

    postman/PRISM_VOCX.postman_collection.json

Hand-curated requests (the all-APIs collection carries the raw adapter route; this one
teaches the flow): capabilities → capture PREVIEW → COMMIT → search, plus Google auth
and the calendar diagnostic. Works with the PRISM Full / All-APIs environments.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "postman"

H = [{"key": "X-Tenant", "value": "{{tenant}}"},
     {"key": "Authorization", "value": "Bearer {{adminToken}}"},
     {"key": "X-User-Email", "value": "{{userEmail}}"}]

TRANSCRIPT = ("Met the EcoSoch Solar team in Bengaluru about the 45 crore term loan for their "
              "150 MW pipeline. They will share audited financials by Friday. Schedule a "
              "follow-up meeting next Monday at 3pm with Ravi.")


def req(name, method, path, body=None, desc=None, tests=None):
    r = {"method": method, "header": [dict(h) for h in H],
         "url": {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"],
                 "path": [s for s in path.split("?")[0].split("/") if s]}}
    if "?" in path:
        r["url"]["query"] = [{"key": k, "value": v} for k, v in
                             (kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))]
    if body is not None:
        r["header"].append({"key": "Content-Type", "value": "application/json"})
        r["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                     "options": {"raw": {"language": "json"}}}
    if desc:
        r["description"] = desc
    it = {"name": name, "request": r}
    if tests:
        it["event"] = [{"listen": "test",
                        "script": {"type": "text/javascript", "exec": tests}}]
    return it


def main() -> None:
    col = {"info": {
        "name": "PRISM · VocX voice capture (via NGINX)",
        "description":
            "The voice pipeline end to end, through the edge (/vocx/v1/*): "
            "capabilities → capture PREVIEW (extraction + confidence gate + write plan; "
            "never writes) → COMMIT (idempotent by capture_id: Register writes as svc_vox, "
            "the RM's Google Calendar when connected) → search the interaction log. Google "
            "connect is a browser flow: open {{baseUrl}}/vocx/v1/auth/start?rm=Priya"
            "&go=1 (the gateway exempts only the CALLBACK from require_auth).",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "item": [
        req("GET capabilities — what this deployment can do", "GET",
            "/vocx/v1/capabilities",
            desc="stt: speech backend available · extraction: haiku|offline_stub (needs "
                 "ANTHROPIC_API_KEY) · google_configured: client secret mounted",
            tests=["pm.test('ok', () => pm.expect(pm.response.code).to.eql(200));",
                   "console.log(JSON.stringify(pm.response.json(), null, 2));"]),
        req("POST capture — PREVIEW a typed transcript", "POST", "/vocx/v1/capture",
            body={"rm": "Priya", "transcript": TRANSCRIPT},
            desc="extract → resolve (against the live Register) → gate. Returns the "
                 "extraction (with _meta.capture_id — the idempotency handle), the per-field "
                 "decision, an approval card when anything is low-confidence, and the write "
                 "plan. NEVER writes.",
            tests=["pm.test('preview ok', () => pm.expect(pm.response.code).to.eql(200));",
                   "const b = pm.response.json();",
                   "pm.environment.set('vocxExtraction', JSON.stringify(b.extraction));",
                   "pm.environment.set('vocxCaptureId', b.extraction._meta.capture_id);",
                   "console.log('decision:', JSON.stringify(b.decision));",
                   "console.log('entity:', JSON.stringify(b.extraction.entity_match));"]),
        req("POST commit — EXECUTE the approved capture (idempotent)", "POST",
            "/vocx/v1/commit",
            body={"rm": "Priya", "extraction": "{{vocxExtraction}}",
                  "capture_id": "{{vocxCaptureId}}",
                  "log_to": {"subject_type": "Deal", "subject_id": "{{dealId}}"},
                  "summary": "Term loan discussion — financials due Friday"},
            desc="log_to (optional) routes the interaction at an explicit subject — "
                 "Lending/Syndication/AssetMonetisation/Deal/Lead/Entity + UUID; remove it "
                 "for automatic routing. Body note: after the capture request above, vocxExtraction holds the "
                 "extraction JSON — Postman inserts it as a STRING, so replace "
                 "\"{{vocxExtraction}}\" (with quotes) by the raw object before sending, or "
                 "paste an edited extraction. chosen_code / new_lead+company override the "
                 "entity; edits patches fields. Register writes carry Idempotency-Key "
                 "vocx:<capture_id>:<op> — send twice and the Register replays, not "
                 "duplicates. Calendar is skipped unless the RM connected Google.",
            tests=["pm.test('committed', () => pm.expect(pm.response.code).to.eql(200));",
                   "console.log(JSON.stringify(pm.response.json().writes, null, 2));"]),
        req("GET interactions — search the log", "GET",
            "/vocx/v1/interactions?limit=10&sort=desc",
            tests=["pm.test('ok', () => pm.expect(pm.response.code).to.eql(200));"]),
        req("GET facets — counts by company/type/user", "GET", "/vocx/v1/facets"),
        req("GET auth status — is this RM's Google connected?", "GET",
            "/vocx/v1/auth/status?rm=Priya",
            desc="To connect: open {{baseUrl}}/vocx/v1/auth/start?rm=Priya&go=1 in a "
                 "BROWSER (Google sign-in + consent; the callback stores the refresh token "
                 "on the vocx volume). Needs the client secret mounted and the redirect URI "
                 "added in Google Cloud Console."),
        req("GET reports — the RM's server-side report list", "GET",
            "/vocx/v1/reports?rm=Priya",
            desc="Every preview auto-saves a DRAFT; save marks it ready; commit marks it "
                 "committed. The list survives device changes — no more localStorage.",
            tests=["pm.test('ok', () => pm.expect(pm.response.code).to.eql(200));",
                   "console.log(JSON.stringify(pm.response.json().reports, null, 2));"]),
        req("POST reports/save — keep the RM's edits", "POST", "/vocx/v1/reports/save",
            body={"rm": "Priya", "capture_id": "{{vocxCaptureId}}", "status": "ready",
                  "report": {"summary": "edited on the laptop"}},
            desc="409 once committed — a committed report is final."),
        req("POST reports/delete", "POST", "/vocx/v1/reports/delete",
            body={"rm": "Priya", "capture_id": "{{vocxCaptureId}}"}),
        req("GET audio — playback for an archived recording", "GET",
            "/vocx/v1/audio?ref={{vocxAudioRef}}",
            desc="MinIO refs answer {url: presigned} (signed against the PUBLIC MinIO "
                 "endpoint — set vocxAudioRef from the preview's _meta.transcript_ref); "
                 "volume refs stream audio/wav bytes. Refs outside the captures bucket/"
                 "prefix are refused."),
        req("GET calendar test — prove which calendar VocX writes to", "GET",
            "/vocx/v1/calendar/test?rm=Priya",
            desc="Creates a real test event tomorrow 16:00 on the connected account and "
                 "returns the calendar's e-mail — answers 'I can't see my follow-up' "
                 "definitively."),
    ]}
    OUT.mkdir(exist_ok=True)
    with open(OUT / "PRISM_VOCX.postman_collection.json", "w") as fh:
        json.dump(col, fh, indent=2)
    print(f"PRISM_VOCX: {len(col['item'])} requests")


if __name__ == "__main__":
    main()
