#!/usr/bin/env python3
"""Learn from the desk's approved VOCX conversations — standard library only,
so it runs on the box against the export deploy/vox-export.sh writes.

  python3 vox_learn.py scorecard vox_export.jsonl [--json]
      How the model did against what reviewers approved, field by field: how
      often each field was edited, whether the model left it empty or got it
      wrong, and how confident it was when it was wrong. The "model" report is
      rebuilt from the edit trail (the earliest edit's old value is what the
      model wrote), so this needs no model calls and spends nothing.

  python3 vox_learn.py mine vox_export.jsonl [--min-count 2] [--json]
  python3 vox_learn.py cases vox_export.jsonl lending.requirement_quantum_cr [--kind missed|invented|wrong|all]
      The notes behind one scorecard cell, with the transcript sentences that
      carry a number or the approved words — to see WHY the model missed.
      Reviewer corrections turned into proposed name fixes: pairs of
      (heard → corrected) from transcript corrections and from edits to the
      lender/attendee fields, ranked by how often they recur. The output is a
      proposal for config ``stt.canonical_aliases`` — reviewed by a person,
      never applied blind.

The export holds client conversations: it stays on the box.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable

# ----------------------------------------------------------------- loading

def load(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _absent(v: Any) -> bool:
    return v is None or v == [] or v == "" or v == "not_specified"


def _cell(x: Any) -> dict | None:
    return x if isinstance(x, dict) and "value" in x else None


# -------------------------------------------------------------- scorecard

def model_report(row: dict) -> dict:
    """The report as the MODEL wrote it: the approved report with every edited
    cell replaced by the earliest edit's old value."""
    rep = json.loads(json.dumps(row.get("structured_report") or {}))
    seen: set[str] = set()
    for e in row.get("edits") or []:
        path = e.get("field_path") or ""
        if "." not in path or path.startswith(("links.", "transcript.")) or path in seen:
            continue
        seen.add(path)
        block, key = path.split(".", 1)
        if isinstance(rep.get(block), dict):
            rep[block][key] = e.get("old_value")
    return rep


def _same(a: Any, b: Any) -> bool:
    if _absent(a) and _absent(b):
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-9
    na = re.sub(r"\s+", " ", json.dumps(a, sort_keys=True).lower()) if isinstance(a, (list, dict)) else str(a).strip().lower()
    nb = re.sub(r"\s+", " ", json.dumps(b, sort_keys=True).lower()) if isinstance(b, (list, dict)) else str(b).strip().lower()
    return na == nb


def scorecard(rows: list[dict]) -> dict:
    per: dict[str, Counter] = defaultdict(Counter)
    conf_when_wrong: dict[str, Counter] = defaultdict(Counter)
    notes_edits: list[int] = []
    by_engine: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        approved = row.get("structured_report") or {}
        model = model_report(row)
        edits = [e for e in (row.get("edits") or []) if "." in (e.get("field_path") or "")
                 and not (e.get("field_path") or "").startswith(("links.", "transcript."))]
        notes_edits.append(len({e["field_path"] for e in edits}))
        eng = row.get("approved_engine") or row.get("engine") or "default"
        by_engine[eng]["notes"] += 1
        by_engine[eng]["edited_fields"] += len({e["field_path"] for e in edits})
        for block, cells in approved.items():
            if not isinstance(cells, dict):
                continue
            for key, cell in cells.items():
                ac, mc = _cell(cell), _cell((model.get(block) or {}).get(key)) if isinstance(model.get(block), dict) else None
                if ac is None:
                    continue
                path = f"{block}.{key}"
                av = ac.get("value")
                mv = mc.get("value") if mc else None
                c = per[path]
                c["notes"] += 1
                if not _absent(av) or not _absent(mv):
                    c["in_play"] += 1
                if _same(av, mv):
                    if not _absent(av):
                        c["right"] += 1
                    continue
                c["edited"] += 1
                if _absent(mv) and not _absent(av):
                    c["model_missed"] += 1
                elif not _absent(mv) and _absent(av):
                    c["model_invented"] += 1
                else:
                    c["model_wrong"] += 1
                conf = (mc or {}).get("confidence") or "?"
                conf_when_wrong[path][conf] += 1
    out_fields = []
    for path, c in per.items():
        in_play = c["in_play"] or 0
        if not in_play:
            continue
        out_fields.append({
            "field": path, "in_play": in_play, "edited": c["edited"],
            "edit_rate": round(c["edited"] / in_play, 3),
            "right": c["right"], "model_missed": c["model_missed"],
            "model_invented": c["model_invented"], "model_wrong": c["model_wrong"],
            "confidence_when_edited": dict(conf_when_wrong[path]),
        })
    out_fields.sort(key=lambda f: (-f["edit_rate"], -f["edited"]))
    n = len(rows)
    zero = sum(1 for x in notes_edits if x == 0)
    srt = sorted(notes_edits)
    return {
        "notes": n,
        "notes_with_no_field_edits": zero,
        "share_untouched": round(zero / n, 3) if n else None,
        "median_edited_fields_per_note": (srt[len(srt) // 2] if srt else 0),
        "by_engine": {k: {"notes": v["notes"],
                          "edited_fields_per_note": round(v["edited_fields"] / v["notes"], 2)}
                      for k, v in by_engine.items()},
        "fields": out_fields,
    }


def print_scorecard(sc: dict) -> None:
    print(f"Approved notes: {sc['notes']} · untouched by reviewers: {sc['notes_with_no_field_edits']} "
          f"({(sc['share_untouched'] or 0) * 100:.0f}%) · median edited fields per note: "
          f"{sc['median_edited_fields_per_note']}")
    for eng, v in sc["by_engine"].items():
        print(f"  {eng}: {v['notes']} notes · {v['edited_fields_per_note']} edited fields per note")
    print()
    print(f"{'field':44} {'in play':>7} {'edited':>6} {'rate':>5} {'missed':>6} {'invented':>8} {'wrong':>5}  confidence when edited")
    for f in sc["fields"]:
        conf = " ".join(f"{k}:{v}" for k, v in sorted(f["confidence_when_edited"].items(), key=lambda kv: -kv[1]))
        print(f"{f['field']:44} {f['in_play']:>7} {f['edited']:>6} {f['edit_rate']:>5.0%} {f['model_missed']:>6} "
              f"{f['model_invented']:>8} {f['model_wrong']:>5}  {conf}")
    print()
    print("missed = model left it empty, the reviewer filled it · invented = model filled it, the reviewer cleared it "
          "· wrong = both had a value and it changed")


# ------------------------------------------------------------------ mining

_NAME_FIELDS = ("lending.existing_bankers", "syndication.existing_lenders", "syndication.probable_lenders",
                "common.attendees_counterparty", "syndication.lender_updates")
_SPLIT_RE = re.compile(r"\s*(?:,|;|/|&|\band\b)\s*", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z][\w&.'-]*")
_STOP = {"the", "and", "of", "for", "to", "a", "in", "on", "at", "is", "was", "with"}
# Words a mishearing can look like but that occur in ordinary finance speech:
# an alias on these would rewrite real sentences. Flagged, never auto-adopted.
_ORDINARY = {"greenfield", "brownfield", "best", "vc", "alliance", "capital", "finance", "bank",
             "credit", "trust", "union", "federal", "first", "national", "central", "general",
             "mass", "maaz", "amar", "latina", "ammo", "a-1", "best", "cost", "light", "power",
             "energy", "solar", "wind", "green", "project", "plant", "india", "indian"}


def _names_of(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            if isinstance(x, dict):
                x = x.get("lender") or x.get("name") or ""
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    if isinstance(value, str):
        return [p.strip() for p in _SPLIT_RE.split(value) if p and p.strip()]
    return []


def _pair_names(old: list[str], new: list[str]) -> list[tuple[str, str]]:
    """Old names matched to the new name they most resemble (0.45–0.97): a
    mishearing corrected, not a name added or removed."""
    pairs: list[tuple[str, str]] = []
    used: set[str] = set()
    for o in old:
        best, score = None, 0.0
        for n in new:
            if n in used or n.lower() == o.lower():
                continue
            s = difflib.SequenceMatcher(None, o.lower(), n.lower()).ratio()
            if s > score:
                best, score = n, s
        if best and 0.45 <= score <= 0.97:
            pairs.append((o, best))
            used.add(best)
    return pairs


def _transcript_pairs(raw: str, fixed: str) -> list[tuple[str, str]]:
    """Replaced spans of up to four words between the raw and corrected transcript."""
    a, b = raw.split(), fixed.split()
    out: list[tuple[str, str]] = []
    sm = difflib.SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b], autojunk=False)
    suffix = re.compile(r"^(bank|finance|capital|financial|services?|ltd|limited)[,.;]?$", re.I)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace" or i2 - i1 > 4 or j2 - j1 > 4:
            continue
        # keep the lender word that follows ("Access Bank" → "Axis Bank"), so
        # the alias never fires on the bare English word "access"
        if i2 < len(a) and j2 < len(b) and suffix.match(a[i2]) and a[i2].lower() == b[j2].lower():
            i2 += 1
            j2 += 1
        heard = " ".join(a[i1:i2]).strip(" ,.;:")
        corr = " ".join(b[j1:j2]).strip(" ,.;:")
        if not heard or not corr or heard.lower() == corr.lower():
            continue
        if heard.lower() in _STOP or corr.lower() in _STOP:
            continue
        # a name-shaped correction: capitalised somewhere, or lender-suffixed
        if not (re.search(r"[A-Z]", heard + corr) or re.search(r"bank|finance|capital", corr, re.I)):
            continue
        out.append((heard, corr))
    return out


def mine(rows: list[dict], min_count: int = 2) -> dict:
    pairs: Counter = Counter()
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    unknown_kept: Counter = Counter()
    for row in rows:
        rid = str(row.get("id"))[:8]
        raw, fixed = row.get("raw_transcript") or "", row.get("corrected_transcript") or ""
        if raw and fixed and raw != fixed:
            for h, c in _transcript_pairs(raw, fixed):
                pairs[(h, c)] += 1
                sources[(h, c)].add(f"{rid}:transcript")
        for e in row.get("edits") or []:
            path = e.get("field_path") or ""
            if path == "transcript.corrected":
                h0, c0 = e.get("old_value") or "", e.get("new_value") or ""
                if isinstance(h0, str) and isinstance(c0, str) and h0 and c0:
                    for h, c in _transcript_pairs(h0, c0):
                        pairs[(h, c)] += 1
                        sources[(h, c)].add(f"{rid}:transcript")
            elif path in _NAME_FIELDS:
                for h, c in _pair_names(_names_of(e.get("old_value")), _names_of(e.get("new_value"))):
                    pairs[(h, c)] += 1
                    sources[(h, c)].add(f"{rid}:{path.split('.', 1)[1]}")
        # names the model wrote that nobody changed and that are not in the glossary
        rep = row.get("structured_report") or {}
        for path in _NAME_FIELDS[:3]:
            block, key = path.split(".", 1)
            cell = (rep.get(block) or {}).get(key) if isinstance(rep.get(block), dict) else None
            for n in _names_of(cell):
                unknown_kept[n] += 1
    # fold case variants of the same correction
    folded: dict[tuple[str, str], int] = {}
    srcs: dict[tuple[str, str], set[str]] = {}
    for (h, c), n in pairs.items():
        k = (h.lower(), c)
        key = next((kk for kk in folded if kk[0] == k[0] and kk[1].lower() == c.lower()), None) or (h, c)
        folded[key] = folded.get(key, 0) + n
        srcs.setdefault(key, set()).update(sources[(h, c)])
    # a pair that also occurs the other way round is a reviewer swapping two
    # real names on one note (an attribution fix), never a mishearing
    rev = {(c.lower(), h.lower()) for (h, c) in folded}
    folded = {k: n for k, n in folded.items() if (k[0].lower(), k[1].lower()) not in rev}
    ranked = sorted(folded.items(), key=lambda kv: (-kv[1], kv[0][0].lower()))
    # a one-word "heard" that is also an ordinary word would rewrite normal
    # English everywhere the alias table runs — flag it for the reviewer
    def risk(h: str) -> str | None:
        w = h.strip()
        if w.lower() in _ORDINARY:
            return "ordinary word — would rewrite normal speech"
        if len(w.split()) == 1 and w.islower():
            return "single lowercase word — check it cannot occur in normal speech"
        if len(w.split()) == 1 and len(w) <= 3:
            return "very short — may be an unrelated acronym elsewhere"
        return None
    proposals = [{"heard": h, "canonical": c, "count": n, "examples": sorted(srcs[(h, c)])[:4],
                  **({"risky": risk(h)} if risk(h) else {})}
                 for (h, c), n in ranked if n >= min_count]
    singles = [{"heard": h, "canonical": c, "count": n, "examples": sorted(srcs[(h, c)])[:2]}
               for (h, c), n in ranked if n < min_count]
    return {
        "pairs_seen": len(ranked),
        "proposals": proposals,
        "seen_once": singles[:60],
        "canonical_aliases": {p["heard"]: p["canonical"] for p in proposals},
        "lender_names_kept_by_reviewers": unknown_kept.most_common(40),
    }


def print_mine(m: dict) -> None:
    print(f"Distinct corrections seen: {m['pairs_seen']} · proposed (recurring): {len(m['proposals'])}")
    print()
    print("PROPOSED ALIASES — heard → canonical (count · where)")
    for p in m["proposals"]:
        print(f"  {p['heard']!r:36} → {p['canonical']!r:36} {p['count']:>3} · {', '.join(p['examples'])}")
    print()
    print("Seen once (review before adopting):")
    for p in m["seen_once"][:30]:
        print(f"  {p['heard']!r:36} → {p['canonical']!r:36}   1 · {', '.join(p['examples'])}")
    print()
    print("Lender names reviewers left as written (most common) — candidates for the FI master or glossary:")
    for name, n in m["lender_names_kept_by_reviewers"][:25]:
        print(f"  {name!r:40} {n:>3}")
    print()
    print("To adopt the proposals, add to config stt.canonical_aliases (services/vocx/app/vocx/config.json):")
    print(json.dumps(m["canonical_aliases"], ensure_ascii=False, indent=2))


# ------------------------------------------------------------------- cases

def cases(rows: list[dict], field: str, kind: str = "missed", limit: int = 10) -> list[dict]:
    """Show the notes behind one scorecard cell: what the model wrote, what the
    reviewer approved, and the transcript sentences that carry a number or the
    approved words — so a 'missed' can be read as 'it was spoken' or 'it was
    not'. Nothing is rewritten; this is for looking."""
    block, key = field.split(".", 1)
    out: list[dict] = []
    for row in rows:
        approved = row.get("structured_report") or {}
        model = model_report(row)
        ac = _cell((approved.get(block) or {}).get(key)) if isinstance(approved.get(block), dict) else None
        mc = _cell((model.get(block) or {}).get(key)) if isinstance(model.get(block), dict) else None
        if ac is None:
            continue
        av, mv = ac.get("value"), (mc or {}).get("value")
        if _same(av, mv):
            continue
        k = "missed" if (_absent(mv) and not _absent(av)) else "invented" if (not _absent(mv) and _absent(av)) else "wrong"
        if kind != "all" and k != kind:
            continue
        text = row.get("corrected_transcript") or row.get("raw_transcript") or ""
        sents = re.split(r"(?<=[.!?])\s+", text)
        words = [w for w in re.findall(r"[A-Za-z]{4,}", str(av))][:4]
        picked = [x for x in sents if re.search(r"\d|crore|lakh|lac\b", x, re.I)
                  or any(w.lower() in x.lower() for w in words)][:4]
        out.append({"id": str(row.get("id"))[:8], "kind": k, "model": mv,
                    "model_confidence": (mc or {}).get("confidence"), "approved": av,
                    "transcript_hints": [x.strip()[:220] for x in picked]})
        if len(out) >= limit:
            break
    return out


def print_cases(cs: list[dict], field: str) -> None:
    print(f"{field}: {len(cs)} case(s)")
    for c in cs:
        print(f"\n[{c['id']}] {c['kind']} · model: {c['model']!r} ({c['model_confidence']}) → approved: {c['approved']!r}")
        for h in c["transcript_hints"]:
            print(f"    … {h}")


# -------------------------------------------------------------------- main

def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("scorecard"); s1.add_argument("export"); s1.add_argument("--json", action="store_true")
    s2 = sub.add_parser("mine"); s2.add_argument("export"); s2.add_argument("--json", action="store_true")
    s2.add_argument("--min-count", type=int, default=2)
    s3 = sub.add_parser("cases"); s3.add_argument("export"); s3.add_argument("field")
    s3.add_argument("--kind", choices=["missed", "invented", "wrong", "all"], default="missed")
    s3.add_argument("--limit", type=int, default=10); s3.add_argument("--json", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    rows = load(args.export)
    if args.cmd == "cases":
        cs = cases(rows, args.field, args.kind, args.limit)
        if args.json:
            print(json.dumps(cs, indent=2, ensure_ascii=False))
        else:
            print_cases(cs, args.field)
        return 0
    if args.cmd == "scorecard":
        sc = scorecard(rows)
        print(json.dumps(sc, indent=2) if args.json else "", end="")
        if not args.json:
            print_scorecard(sc)
    else:
        m = mine(rows, args.min_count)
        print(json.dumps(m, indent=2, ensure_ascii=False) if args.json else "", end="")
        if not args.json:
            print_mine(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
