"""CAM evaluation — a draft scored against a golden case, so "better" is a number.

WHY. Every change to the CAM pipeline used to be judged by reading one draft. The credit
team's reviews turned into checks a program can run: the figures a correct CAM must state
(a balance sheet's real net worth, the year's whole sales), the things it must never say
(a tied statement "does not balance", "liabilities only" of a form that declares assets,
a [source] tag, a rule code), and the shape of its tone (how the colours fall, how many
"not on record"s). A golden case holds those for one real file; ``score`` runs them on a
draft. Run it on cipher-test before a change ships, and on the old draft for the baseline.

Golden cases carry client facts — keep them OUTSIDE the repository, beside the client's
files. A case is JSON:

    {"name": "...",
     "facts":     [{"name": "FY26 net worth", "value": 180.99, "tolerance": 0.5,
                    "section": "FINANCIAL"}],                # section: optional heading match
     "forbidden": [{"name": "tied BS called unbalanced", "pattern": "does not balance"}],
     "required":  [{"name": "credit picture", "pattern": "Credit picture at a glance"}]}

    python -m app.cam_eval case.json draft.md [job.json]
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

# Tone and hygiene, counted on every draft whatever the case says.
_METRICS: dict[str, str] = {
    "source tags": r"\[source:",
    "'Source:' lines": r"^\s*[*_]?Sources?:",
    "rule codes and tool talk": r"\bA\d\.\d+\b|\bRule\s+\d+\b|\bPhase\s+[ABC]\b|\bPRISM\b"
                                r"|\bnot\s+configured\b|\bHonest\s+assessment\b",
    "'not on record'": r"not on record",
    "NOT PERFORMED": r"NOT PERFORMED",
    "'Mitigant: none'": r"Mitigant:\s*none",
}
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _sections(md: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for part in re.split(r"(?m)^(?=## )", md):
        if part.startswith("## "):
            head, _, body = part.partition("\n")
            out.append((head[3:].strip(), body))
    return out or [("", md)]


def _has_value(text: str, value: float, tol: float) -> bool:
    """The figure is in the text: the expected value, in lakh, crore or rupees, rounded to
    the precision the draft prints it at ("181.0" and "181" are 180.99; "181.2" is not),
    with three significant digits at least ("1.5" crore is no proof of 149.93 lakh).
    ``tol`` widens the lakh comparison for a figure legitimately rounded differently."""
    for m in _NUM.finditer(text):
        raw = m.group(0)
        try:
            x = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        if len(re.sub(r"\D", "", raw).lstrip("0")) < 3:
            continue
        places = len(raw.split(".")[1]) if "." in raw else 0
        step = Decimal(1).scaleb(-places)
        for scale in (Decimal(1), Decimal(100), Decimal("0.00001")):
            want = (Decimal(str(value)) / scale).quantize(step)
            if want == x or (scale == 1 and abs(float(x) - value) <= tol):
                return True
    return False


def score(draft_md: str, case: dict[str, Any], qa: dict[str, Any] | None = None
          ) -> dict[str, Any]:
    """{"score": passed/total, "checks": [...], "metrics": {...}} for one draft."""
    checks: list[dict[str, Any]] = []
    secs = _sections(draft_md)
    for f in case.get("facts") or []:
        where = [b for h, b in secs if re.search(f.get("section") or "", h, re.I)] or [draft_md]
        ok = any(_has_value(b, float(f["value"]), float(f.get("tolerance", 0.0))) for b in where)
        checks.append({"kind": "fact", "name": f["name"], "pass": ok,
                       "detail": f"{f['value']} {'found' if ok else 'not found'}"
                       + (f" in {f['section']}" if f.get("section") else "")})
    for f in case.get("forbidden") or []:
        hits = [m.group(0) for m in re.finditer(f["pattern"], draft_md, re.I | re.M)]
        checks.append({"kind": "forbidden", "name": f["name"], "pass": not hits,
                       "detail": f"{len(hits)} occurrence(s)" + (f": {hits[0]!r}" if hits else "")})
    for f in case.get("required") or []:
        ok = bool(re.search(f["pattern"], draft_md, re.I | re.M))
        checks.append({"kind": "required", "name": f["name"], "pass": ok,
                       "detail": "present" if ok else "missing"})
    cells = [c.strip(" *") for ln in draft_md.splitlines() if ln.lstrip().startswith("|")
             for c in ln.split("|")]
    metrics: dict[str, Any] = {k: len(re.findall(rx, draft_md, re.I | re.M))
                               for k, rx in _METRICS.items()}
    metrics.update({s: sum(1 for c in cells if re.match(rf"{s}\b", c))
                    for s in ("GREEN", "AMBER", "RED")})
    metrics["risks rated High"] = sum(1 for c in cells if re.fullmatch(r"(?i)high", c))
    metrics["words"] = len(re.findall(r"\w+", draft_md))
    if qa:
        metrics["machine QA"] = "passed" if qa.get("passed") else "failed"
        metrics["QA fails/warns"] = [c["check"] for c in qa.get("checks", [])
                                     if c.get("status") in ("fail", "warn")]
    passed = sum(1 for c in checks if c["pass"])
    return {"case": case.get("name", ""), "score": f"{passed}/{len(checks)}",
            "passed": passed, "total": len(checks), "checks": checks, "metrics": metrics}


def report(result: dict[str, Any]) -> str:
    lines = [f"# CAM eval — {result['case']}: {result['score']}", ""]
    for c in result["checks"]:
        lines.append(f"- [{'x' if c['pass'] else ' '}] {c['kind']}: {c['name']} — {c['detail']}")
    lines += ["", "Metrics:"] + [f"- {k}: {v}" for k, v in result["metrics"].items()]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    case = json.load(open(argv[1], encoding="utf-8"))
    draft = open(argv[2], encoding="utf-8").read()
    qa = None
    if len(argv) > 3:
        job = json.load(open(argv[3], encoding="utf-8"))
        qa = job.get("qa")
        draft = draft or job.get("draft_md") or ""
    result = score(draft, case, qa)
    print(report(result))
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":                      # pragma: no cover
    sys.exit(main(sys.argv))
