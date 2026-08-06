#!/usr/bin/env python3
"""
core.pipeline — the single entry point that ties VOX step 1 + step 2 together.

    from app.vocx.core.pipeline import process_capture
    result = process_capture(transcript, rm="Shubh", capture_ts="2026-07-21T15:00:00")

process_capture runs: extract (Haiku or offline stub) -> resolve entity ->
confidence gate -> either an approval card (needs_approval) or a write plan
(auto_write). It never performs Google/ATLAS writes itself — it returns the plan;
step 3 hands that plan to real writers. A MockWriter is used here only when
execute=True so the flow is demonstrable end to end.

CLI (typed transcript, no audio/Google needed — build-order step 1/2):
    python3 vocx_pipeline.py --rm Shubh --ts 2026-07-21T15:00 "Met the Carbon Masters team ..."
    echo "Met the Carbon Masters team ..." | python3 vocx_pipeline.py --rm Shubh
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Any

from app.vocx.core import extract as vocx_extract
from app.vocx.core import gate as vocx_gate
from app.vocx.core.atlas import AtlasStore
from app.vocx.core.resolve import EntityResolver, load_config


def process_capture(
    transcript: str,
    rm: str,
    capture_ts: str | None = None,
    transcript_ref: str | None = None,
    store: AtlasStore | None = None,
    config: dict[str, Any] | None = None,
    offline: bool = False,
    summary: str | None = None,
    execute: bool = False,
    writer: Any = None,
    meta_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a captured meeting through the full step-1+2 pipeline.

    meta_extra: capture-side facts that ride on _meta into the committed interaction's
    STRUCTURED columns (language, gps_lat/gps_lng/location, ...). Never trusted for
    routing — only recorded.

    Returns:
      {extraction, decision, approval_card|None, write_plan, writes|None}
    """
    config = config or load_config()
    store = store or AtlasStore.default()
    resolver = EntityResolver(store, config)
    capture_ts = capture_ts or _dt.datetime.now().isoformat(timespec="seconds")

    # 1) extract
    ext = vocx_extract.extract(transcript, capture_ts=capture_ts, rm=rm,
                              transcript_ref=transcript_ref, config=config, offline=offline)
    # The raw transcript + capture-side facts land in the interaction's structured
    # columns at commit; the report's transcript_english stays the polished copy.
    ext["_meta"]["transcript"] = transcript
    if meta_extra:
        ext["_meta"].update({k: v for k, v in meta_extra.items() if v not in (None, "")})
    _promote_followup(ext)   # a meeting-like next step becomes the scheduled follow-up
    # 2) resolve entity (fills entity_match)
    ext["entity_match"] = resolver.resolve(
        ext.get("company_mentioned"), ext.get("company_aliases_heard"), speaking_rm=rm)
    # a one-line summary (Haiku would author this; fall back to discussion points)
    if summary is None:
        summary = _fallback_summary(ext)
    # 3) gate
    decision = vocx_gate.gate(ext, config)
    # 4) plan + (optionally) approval card
    write_plan = vocx_gate.plan_writes(ext, store, config, summary=summary)
    approval_card = (vocx_gate.build_approval_card(ext, decision, config, summary=summary)
                     if decision["needs_approval"] else None)

    writes = None
    if execute and decision["auto_write"]:
        w = writer or vocx_gate.MockWriter()
        context = {"extraction": ext, "transcript": transcript, "summary": summary}
        writes = w.execute(write_plan, context)

    return {
        "extraction": ext,
        "decision": decision,
        "approval_card": approval_card,
        "write_plan": write_plan,
        "writes": writes,
    }


def process_audio_capture(
    audio: Any,
    rm: str,
    capture_ts: str | None = None,
    transcriber: Any = None,
    language: str | None = None,
    config: dict[str, Any] | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Step 4: transcribe an audio clip, then run the same pipeline on the text.

    `audio` is a file path or raw bytes. The result is the usual process_capture
    dict with an added `transcription` block. transcriber defaults to the backend
    named in config (stt.backend).
    """
    from app.vocx.speech import stt as vocx_stt
    config = config or load_config()
    capture_ts = capture_ts or _dt.datetime.now().isoformat(timespec="seconds")
    transcriber = transcriber or vocx_stt.build_transcriber(config)

    # Live stage reporting: the caller (the capture endpoint) records where this take
    # is in the pipeline so a UI can show progress while the request runs. Optional
    # and side-effect-only — a missing hook changes nothing.
    on_stage = kwargs.pop("on_stage", None)
    if on_stage:
        on_stage("transcribing")
    tr = transcriber.transcribe(audio, language=language,
                                prompt=kwargs.pop("stt_prompt", None))
    ref = kwargs.pop("transcript_ref", None) or vocx_stt.archive_audio(audio, capture_ts, rm, config)
    if on_stage:
        on_stage("structuring")

    # The STT-detected language rides into the interaction's `language` column unless
    # the client already asserted one.
    meta_extra = dict(kwargs.pop("meta_extra", None) or {})
    if tr.get("language"):
        meta_extra.setdefault("language", tr["language"])

    result = process_capture(tr["text"], rm=rm, capture_ts=capture_ts,
                             transcript_ref=ref, config=config,
                             meta_extra=meta_extra or None, **kwargs)
    result["transcription"] = tr
    return result


_MEETING_KW = ("follow-up meeting", "follow up meeting", "followup meeting", "follow-up call",
               "schedule a meeting", "schedule follow", "schedule the follow", "next meeting",
               "meet again", "catch up", "review meeting", "call with", "meeting with", "site visit")


def _promote_followup(ext: dict[str, Any]) -> None:
    """If no next_meeting was captured but a next_step is clearly a meeting to
    schedule (and carries a date), promote it to next_meeting so a proper calendar
    event is created — and pull a time out of the action text if one is there.
    The promoted step's date is cleared so it isn't double-scheduled."""
    import re
    nm = ext.setdefault("next_meeting", {})
    if nm.get("date"):
        return
    rep = ext.get("report") or {}
    for s in rep.get("next_steps") or []:
        act = (s.get("action") or "")
        low = act.lower()
        if s.get("date") and any(k in low for k in _MEETING_KW):
            nm["date"] = s["date"]
            m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", low)
            if m and not nm.get("time"):
                h = int(m.group(1)) % 12
                if m.group(3).startswith("p"):
                    h += 12
                nm["time"] = "{:02d}:{}".format(h, m.group(2) or "00")
            nm["confidence"] = max(float(nm.get("confidence") or 0.0), 0.8)
            s["date"] = None   # avoid a duplicate all-day reminder for the same thing
            break


def _fallback_summary(ext: dict[str, Any]) -> str:
    pts = ext.get("discussion_points") or []
    return pts[0] if pts else ""


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="VOX pipeline (typed transcript).")
    ap.add_argument("transcript", nargs="?", help="transcript text; if omitted, read stdin")
    ap.add_argument("--rm", required=True, help="speaking RM (e.g. Shubh, Chetan)")
    ap.add_argument("--ts", dest="capture_ts", default=None, help="capture ISO timestamp")
    ap.add_argument("--audio", default=None, help="path to an audio clip (transcribe via STT, step 4)")
    ap.add_argument("--offline", action="store_true", help="force offline extraction stub")
    ap.add_argument("--execute", action="store_true", help="run MockWriter on auto-write")
    args = ap.parse_args(argv)

    if args.audio:
        result = process_audio_capture(args.audio, rm=args.rm, capture_ts=args.capture_ts,
                                       offline=args.offline, execute=args.execute)
    else:
        transcript = args.transcript or sys.stdin.read()
        if not transcript.strip():
            ap.error("no transcript provided")
        result = process_capture(transcript, rm=args.rm, capture_ts=args.capture_ts,
                                 offline=args.offline, execute=args.execute)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
