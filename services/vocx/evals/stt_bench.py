#!/usr/bin/env python3
"""STT engine benchmark — the same saved audio, every engine, critical facts scored.

The two-test diagnostic's rule: compare on the EXACT same recording, and score
critical facts (names, amounts, negations, finance terms) — not word accuracy.
This tool runs a saved take through each engine and prints the table.

Run INSIDE the deployed vocx container (it has the STT service URL, the Sarvam
key, and the MinIO credentials):

    # by audio file (docker cp a clip in, or a local file)
    docker exec compose-vocx-1 python -m evals.stt_bench \
        --audio /tmp/test1.webm --reference evals/stt_refs/test1.json

    # by the conversation's stored audio ref (s3://... as the register row shows)
    docker exec compose-vocx-1 python -m evals.stt_bench \
        --ref "s3://vocx-captures/..." --reference evals/stt_refs/test1.json

Engines:
    service   the deployed PRISM STT service (Whisper) — exactly what production hears
    sarvam    Sarvam's Indic translate STT (saaras) — the Regional candidate
Add --engines to restrict, --prompt-off to measure WITHOUT vocabulary priming
(the ablation that shows what priming is worth), --save-dir to keep transcripts.

Scoring: a fact counts as HEARD when any of its variants appears in the
normalised transcript (lowercase, punctuation stripped, whitespace collapsed).
The table shows per-category hit rates and every miss by name — misses are the
whole point.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _normalise(text: str) -> str:
    t = (text or "").lower()
    t = t.replace("₹", " ")
    t = re.sub(r"[^\w%.'-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _score(transcript: str, refs: dict) -> dict:
    norm = " " + _normalise(transcript) + " "
    out: dict[str, dict] = {}
    for category, facts in (refs.get("facts") or {}).items():
        hits, misses = [], []
        for f in facts:
            variants = [_normalise(v) for v in (f.get("any") or [])]
            (hits if any(v and v in norm for v in variants) else misses).append(f["label"])
        out[category] = {"hit": len(hits), "total": len(hits) + len(misses),
                         "misses": misses}
    return out


def _fetch_ref(ref: str) -> tuple[bytes, str]:
    """Audio bytes for a stored reference, via the app's own stores (the vocx
    container has the MinIO credentials; nothing is exported anywhere)."""
    if ref.startswith("vox-seg:"):
        import os as _os

        from app.vocx.speech.segment_store import SegmentStore
        base = (_os.environ.get("VOCX_SEGMENTS_DIR")
                or _os.path.join("vocx_tokens", "vox_segments"))
        paths = SegmentStore(base).segment_paths(ref[len("vox-seg:"):])
        blobs = []
        for p in paths:
            with open(p, "rb") as fh:
                blobs.append(fh.read())
        return b"".join(blobs), "audio/webm"
    from app.config import get_settings
    from app.vocx.speech.audio_store import build_audio_store
    store = build_audio_store(get_settings())
    got = store.playback(ref) if store else None
    if not got:
        raise RuntimeError(f"audio ref not servable by this box's store: {ref!r}")
    kind, payload = got
    if kind == "bytes":
        return payload, "audio/webm"
    import httpx
    r = httpx.get(payload, timeout=120)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "audio/webm")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", help="local audio file")
    ap.add_argument("--ref", help="stored audio reference (s3://... or vox-seg:...)")
    ap.add_argument("--reference", required=True, help="stt_refs/*.json fact file")
    ap.add_argument("--engines", default="service,sarvam")
    ap.add_argument("--prompt-off", action="store_true",
                    help="disable vocabulary priming (ablation)")
    ap.add_argument("--save-dir", help="write each engine's transcript here")
    args = ap.parse_args()

    with open(args.reference, encoding="utf-8") as fh:
        refs = json.load(fh)

    if args.audio:
        with open(args.audio, "rb") as fh:
            blob = fh.read()
        ctype = "audio/webm"
    elif args.ref:
        blob, ctype = _fetch_ref(args.ref)
    else:
        ap.error("one of --audio / --ref is required")
        return 2
    print(f"audio: {len(blob)} bytes · reference: {refs.get('name')}")

    prompt = None
    if not args.prompt_off:
        # The SAME priming production uses — vocabulary + lender roster + Evam.
        try:
            from app.vocx.core.server import VocxApp  # noqa: F401  (import check)
            from app.vocx.core.resolve import load_config
            from app.vocx.pipeline.glossary import LENDER_GLOSSARY
            cfg = load_config()
            terms = [t for t in ((cfg.get("stt") or {}).get("vocabulary") or []) if t]
            lenders = [re.sub(r"\s*\(.*\)$", "", e).strip() for e in LENDER_GLOSSARY]
            prompt = (", ".join([*terms, "Evam Finance", *lenders]))[:1500]
        except Exception as exc:  # noqa: BLE001
            print(f"(priming unavailable: {exc})")

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    results = {}
    for name in engines:
        if name == "service":
            from app.vocx.speech.stt import APITranscriber
            eng = APITranscriber(endpoint_env="VOCX_STT_API_URL",
                                 key_env="VOCX_STT_API_KEY")
        elif name == "sarvam":
            from app.vocx.speech.sarvam_stt import SarvamTranscriber
            eng = SarvamTranscriber()
        else:
            print(f"unknown engine {name!r} — skipped")
            continue
        t0 = time.monotonic()
        try:
            got = eng.transcribe(blob, prompt=prompt, content_type=ctype)
        except Exception as exc:  # noqa: BLE001
            print(f"\n== {name}: FAILED — {exc}")
            continue
        dt = time.monotonic() - t0
        text = got.get("text") or ""
        results[name] = (_score(text, refs), dt, text)
        if args.save_dir:
            os.makedirs(args.save_dir, exist_ok=True)
            with open(os.path.join(args.save_dir, f"{name}.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(text)

    if not results:
        print("no engine produced a transcript")
        return 1
    print("\n%-10s %-10s %-10s %-10s %-12s %s" % (
        "engine", "names", "amounts", "terms", "negations", "seconds"))
    for name, (score, dt, _text) in results.items():
        row = [name]
        for cat in ("names", "amounts", "terms", "negations"):
            c = score.get(cat) or {"hit": 0, "total": 0}
            row.append(f"{c['hit']}/{c['total']}" if c["total"] else "—")
        print("%-10s %-10s %-10s %-10s %-12s %.1f" % (*row, dt))
    for name, (score, _dt, _text) in results.items():
        misses = [(cat, m) for cat in score for m in score[cat]["misses"]]
        if misses:
            print(f"\n== {name} missed:")
            for cat, m in misses:
                print(f"   [{cat}] {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
