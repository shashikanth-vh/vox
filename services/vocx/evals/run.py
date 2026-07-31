#!/usr/bin/env python3
"""VocX extraction eval harness — measure the pipeline, don't guess.

    cd services/vocx && python -m evals.run          # live (needs ANTHROPIC_API_KEY)
    cd services/vocx && python -m evals.run --offline # stub smoke (pipeline shape only)

Runs every case in evals/cases.json through the REAL extraction path (the same
build_system_prompt + structured-output call production uses) and scores the result
against per-case expectations. Prints a per-case PASS/FAIL table and an aggregate.

Expectation keys (all optional per case):
  company_contains            substring of extraction.company_mentioned (case-insens.)
  business_line_hint          register_signals.business_line_hint == value
  deal_temp                   report.deal_temp == value
  next_meeting_time           next_meeting.time == "HH:MM"
  next_meeting_has_date       bool — a date was / was NOT captured
  lender_updates_min          len(register_signals.lender_updates) >= n
  key_intel_min / nuances_min / commitments_min      list length >= n
  report.<field>_contains     substring of that report field (e.g. ticket_size)
  report.extra.<key>_contains substring of report.extra[key]
  summary_in_english          crude check: summary is mostly ASCII (Hinglish → English)

Exit code: 0 when every case passes, 1 otherwise — CI-friendly, but the live mode is
meant for on-demand runs (prompt changes, model changes), not every push.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.vocx.core import extract as vocx_extract  # noqa: E402
from app.vocx.core.resolve import load_config  # noqa: E402

CASES = Path(__file__).parent / "cases.json"
CAPTURE_TS = "2026-07-31T11:00:00"                  # fixed: relative dates resolve against this


def _get(d, path, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def score(ext: dict, expected: dict) -> list[tuple[str, bool, str]]:
    rep = ext.get("report") or {}
    rs = ext.get("register_signals") or {}
    nm = ext.get("next_meeting") or {}
    checks: list[tuple[str, bool, str]] = []
    for key, want in expected.items():
        if key == "company_contains":
            got = (ext.get("company_mentioned") or "").lower()
            checks.append((key, want.lower() in got, got or "∅"))
        elif key == "business_line_hint":
            got = rs.get("business_line_hint")
            checks.append((key, got == want, str(got)))
        elif key == "deal_temp":
            got = rep.get("deal_temp")
            checks.append((key, got == want, str(got)))
        elif key == "next_meeting_time":
            got = nm.get("time")
            checks.append((key, got == want, str(got)))
        elif key == "next_meeting_has_date":
            got = bool(nm.get("date"))
            checks.append((key, got == want, str(nm.get("date"))))
        elif key == "lender_updates_min":
            got = len(rs.get("lender_updates") or [])
            checks.append((key, got >= want, f"{got} updates"))
        elif key in ("key_intel_min", "nuances_min", "commitments_min"):
            field = key.rsplit("_", 1)[0]
            src = ext if field == "commitments" else rep
            got = len(src.get(field) or [])
            checks.append((key, got >= want, f"{got} items"))
        elif key == "summary_in_english":
            summary = rep.get("summary") or ""
            ascii_ratio = (sum(c.isascii() for c in summary) / len(summary)) if summary else 0
            checks.append((key, bool(summary) and ascii_ratio > 0.95, f"{ascii_ratio:.0%} ascii"))
        elif key.startswith("report.extra.") and key.endswith("_contains"):
            fld = key[len("report.extra."):-len("_contains")]
            got = str((rep.get("extra") or {}).get(fld) or "")
            checks.append((key, want.lower() in got.lower(), got or "∅"))
        elif key.startswith("report.") and key.endswith("_contains"):
            fld = key[len("report."):-len("_contains")]
            got = str(rep.get(fld) or "")
            checks.append((key, want.lower() in got.lower(), got or "∅"))
        else:
            checks.append((key, False, "unknown expectation key"))
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="run the deterministic stub (pipeline smoke, not quality)")
    ap.add_argument("--case", help="run only the named case")
    args = ap.parse_args()

    if not args.offline and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — use --offline for a stub smoke run.")
        return 1

    config = load_config()
    cases = json.loads(CASES.read_text())
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]

    total = passed = 0
    for case in cases:
        ext = vocx_extract.extract(case["transcript"], capture_ts=CAPTURE_TS, rm="Eval",
                                   config=config, offline=args.offline)
        checks = score(ext, case["expected"])
        ok = all(c[1] for c in checks)
        total += 1
        passed += ok
        print(f"\n{'PASS' if ok else 'FAIL'}  {case['name']}")
        for name, good, got in checks:
            print(f"    {'✓' if good else '✗'} {name:<28} {got}")

    print(f"\n== {passed}/{total} cases passed"
          + (" (offline stub — shape only, not quality)" if args.offline else ""))
    # Offline is a smoke of the harness itself — the stub is EXPECTED to miss quality
    # bars, so only the live run gates on the score.
    return 0 if (args.offline or passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
