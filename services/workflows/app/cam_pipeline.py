"""The staged CAM drafter: the credit team's master prompt run as its own workflow.

One request holding the whole prompt, every document and a ~40k-token CAM overflows the
model's context on a real deal and spreads its attention thin. The EVAM master prompt
already prescribes the fix — extract to a dataset, lock the financial table, then write
the memo — so this module runs exactly that, as bounded calls:

  1. DIGEST       each document (split at page boundaries when large) → a source-tagged
                  fact sheet. Cached per (model, rules, document), run in parallel.
  2. CONSOLIDATE  all fact sheets → the LOCKED financial dataset workpaper; in parallel,
                  the public checks the prompt requires, listed as NOT PERFORMED.
  3. BODY         every section except the summary sections, in parallel, each with the
                  rules, the fact sheets, the locked dataset and ITS specification.
  4. FINAL        the Proposal Summary (page 1), Executive Summary, Recommendations and
                  the title page, written last from the finished body so they agree with it.
  5. ASSEMBLE     the sections in the prompt's order.
  6. MACHINE QA   the prompt's Step 7 checks a program can make (sections present and
                  not duplicated, Financial Analysis figures traced to the locked
                  dataset, lengths) — reported with the draft, appended to the workpaper.

Every stage also sees the deal as the Register holds it (borrower identity, the line's
amount, product, RM, analyst), so page 1 and Annexure I are not left "not on record".

A prompt that does not have the master prompt's structure (no SECTION/ANNEXURE
specifications) is not forced through this: ``parse_master_prompt`` returns None and the
caller drafts in a single call as before.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.cam_ratios import FIGURES_TASK, compute, parse_figures, plausibility, ratios_markdown, trace
from app.cam_telemetry import stage

# ----------------------------------------------------------------------------- prompt
_BLOCK = re.compile(r"^BLOCK\s+([A-Z])\b")
_PHASE = re.compile(r"^PHASE\s+([A-Z])\b")
# Case-sensitive. The Proposal Summary (page 1) and the Term Sheet appendix are parts of
# the CAM too: without them here, page 1 was folded into Phase C's instructions and the
# appendix into Section 11's, and neither was ever written.
_SECTION = re.compile(r"^(SECTION\s+\d+\s*:|ANNEXURE\s+[IVXL]+\s*:|APPENDIX\s*:"
                      r"|PROPOSAL SUMMARY\b)")
_QA = re.compile(r"^(JUDGMENT QA|QA CHECKLIST|READ-THROUGH CHECKLIST)\b", re.I)
# Written last, from the finished body: the summaries, and page 1 (its Evam Strategy and
# proposal tables restate what the body establishes).
_FINAL = re.compile(r"EXECUTIVE SUMMARY|RECOMMENDATION|PROPOSAL SUMMARY", re.I)
# "(350–500 words + snapshot table)", "(Page 1)": guidance for the writer, not heading.
_HINT = re.compile(r"\s*\((?=[^()]*(?:\bwords?\b|\btables?\b|prose|\bpage\s*\d))[^()]*\)\s*$",
                   re.I)
_WORDS = re.compile(r"\(([\d,]+)\s*[–-]\s*([\d,]+)\s*words", re.I)


@dataclass
class SectionSpec:
    title: str          # the heading as the prompt writes it, e.g. "SECTION 5: FINANCIAL ..."
    spec: str           # the prompt's instructions for this section
    order: int
    final: bool         # written last, from the finished body

    @property
    def heading(self) -> str:
        """The heading as it appears in the CAM (writing hints removed)."""
        return _HINT.sub("", self.title).strip()

    @property
    def word_range(self) -> tuple[int, int] | None:
        """The prompt's length for this section, e.g. (350, 500), when it sets one."""
        m = _WORDS.search(self.title)
        return (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))) if m else None


@dataclass
class MasterPrompt:
    rules: str                          # Block A: persona, analytical rules, scales, tone
    phases: dict[str, str]              # "A" extraction, "B" public checks, "C" forensic
    sections: list[SectionSpec]
    qa: str                             # the read-through checklist (quality bar)
    preamble: str = ""


def parse_master_prompt(text: str) -> MasterPrompt | None:
    """Split a master prompt into its parts, or None when it has no section specs."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    blocks = {m.group(1): i for i, ln in enumerate(lines) if (m := _BLOCK.match(ln.strip()))}
    heads = [i for i, ln in enumerate(lines) if _SECTION.match(ln.strip())]
    if len(heads) < 3:
        return None
    end_b = blocks.get("C", len(lines))
    qa_at = next((i for i in range(heads[-1] + 1, end_b) if _QA.match(lines[i].strip())), end_b)

    def span(a: int, b: int) -> str:
        return "\n".join(ln for ln in lines[a:b] if ln.strip()).strip()

    start_a = blocks.get("A", 0)
    rules = span(start_a, blocks.get("B", heads[0]))
    phase_at = [(m.group(1), i) for i, ln in enumerate(lines[:heads[0]])
                if (m := _PHASE.match(ln.strip()))]
    phases = {}
    for n, (name, i) in enumerate(phase_at):
        nxt = phase_at[n + 1][1] if n + 1 < len(phase_at) else heads[0]
        phases[name] = span(i, nxt)
    sections = []
    for n, i in enumerate(heads):
        nxt = heads[n + 1] if n + 1 < len(heads) else qa_at
        title = lines[i].strip()
        sections.append(SectionSpec(title=title, spec=span(i + 1, nxt), order=n,
                                    final=bool(_FINAL.search(title))))
    return MasterPrompt(rules=rules, phases=phases, sections=sections,
                        qa=span(qa_at, end_b) if qa_at < end_b else "",
                        preamble=span(0, start_a))


# ------------------------------------------------------------------------- documents
@dataclass
class SourceDoc:
    doc_id: str
    name: str
    text: str
    doc_type: str = ""
    pages: int | None = None
    # Content blocks for an engine that reads files itself (a scan with no text layer,
    # handed to Anthropic as the PDF); the fact sheet is then read from the attachment.
    blocks: list[dict[str, Any]] | None = None


def split_pages(text: str, max_chars: int) -> list[str]:
    """Split at ``[page N]`` markers (DocRAG) or paragraph breaks, never mid-page when a
    page fits; each part at most max_chars."""
    if len(text) <= max_chars:
        return [text]
    units = re.split(r"(?=^\[page \d+\]$)", text, flags=re.M)
    if len(units) <= 1:
        units = text.split("\n\n")
    parts: list[str] = []
    cur = ""
    for unit in units:
        while len(unit) > max_chars:                    # one oversized page: hard cut
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(unit[:max_chars])
            unit = unit[max_chars:]
        if len(cur) + len(unit) > max_chars and cur:
            parts.append(cur)
            cur = ""
        cur += unit if not cur else ("\n\n" + unit if not unit.startswith("[page") else unit)
    if cur.strip():
        parts.append(cur)
    return parts


def manifest(docs: list[SourceDoc]) -> str:
    rows = ["| # | Document | Type | Pages |", "| --- | --- | --- | --- |"]
    for n, d in enumerate(docs, 1):
        rows.append(f"| {n} | {d.name} | {d.doc_type or '—'} | {d.pages or '—'} |")
    return "DOCUMENT MANIFEST (what the analyst supplied)\n" + "\n".join(rows)


def fit(parts: list[str], budget: int) -> list[str]:
    """Shrink the LONGEST parts first until the total fits the budget — short fact
    sheets survive whole; a clipped part says so."""
    total = sum(len(p) for p in parts)
    if total <= budget:
        return parts
    out = list(parts)
    cap = max(len(p) for p in out)
    while sum(len(p) for p in out) > budget and cap > 2000:
        cap = int(cap * 0.85)
        out = [p if len(p) <= cap else p[:cap] + "\n[… clipped to fit the context budget]"
               for p in parts]
    return out


# ------------------------------------------------------------------------- prompts
DIGEST_TASK = (
    "Produce the FACT SHEET for the document above — extraction only, no credit opinion. "
    "Use these headings, omitting any with nothing to report: Document (type, entity, "
    "period covered, date); Key figures (a Markdown table: Item | Period | Value | Unit | "
    "Source); Borrowings & debt; Debtors, creditors & order book; Banking, GST & ITR; "
    "Management, ownership & group; Legal, compliance & security; Qualitative "
    "observations; Gaps & red flags (what a CAM would need that this document lacks, "
    "internal inconsistencies). Copy every figure exactly as stated, with its unit and "
    "period, and tag it [source: <document name>, page N]. Never compute, estimate or "
    "infer a figure that is not written in the document.")

DATASET_TASK = (
    "Build the FINANCIAL DATASET WORKPAPER from the fact sheets above, as the prompt's "
    "Steps 1 and 4 require. It is LOCKED once written: every CAM section will quote it. "
    "Include: (1) the multi-year P&L and balance sheet table in the prompt's currency "
    "convention, one column per period, each figure source-tagged; (2) the inputs each "
    "A3.5 ratio needs, source-tagged (the ratios themselves are computed by PRISM from "
    "your figures block — do not compute them); (3) the debt structure map and debtor "
    "book map; (4) triangulation across "
    "documents against the prompt's variance thresholds, naming every conflict; (5) the "
    "stress tests the prompt specifies, with post-stress DSCR; (6) the Analyst Base Case "
    "if the prompt's trigger applies. Where documents disagree, show both values and the "
    "one you adopt with the reason. Mark any figure you could not source 'not on record'. "
    "Output Markdown, headed '## Financial dataset workpaper'.")

CHECKS_TASK = (
    "From the public-information phase above, list every public check the prompt requires "
    "for this borrower (at least the minimum it sets), as one Markdown table: Check | "
    "Source / query | Status | Finding. Where the PUBLIC SOURCES PRISM HOLDS block "
    "answers a check, Status is 'Verified via <source>, <date>' (or 'Partly verified via "
    "…') and Finding states ONLY what that source shows, cited. Every other row: Status "
    "'NOT PERFORMED — to be completed by the analyst' (this environment has no web or "
    "registry access), Finding blank. Never state a public fact that is not in that "
    "block. Head it '## Public information checks'. Output only that section.")


TITLE_TASK = (
    "Write ONLY the CAM's TITLE PAGE block, as plain lines (no heading, no table, no "
    "blank lines), in this order: 'Evam Finance Private Limited'; the borrower's legal "
    "name; its constitution and location; 'Proposed Facility: <facility type> — Rs. "
    "<amount> lakh' (write 'facility type not on record' when the type is unknown); the "
    "key terms on one line if on record (rate | tenor | availability); 'Document date: "
    "{today}'; 'Financial data as of <the latest period in the locked dataset, and "
    "whether audited or provisional>'; the dates the banking, debtor and debt data run "
    "to, where on record; 'Prepared by: <the analyst on the Register, else Credit Risk "
    "Group>'. Take each fact from the deal on the Register, the locked dataset or the "
    "CAM body; write 'not on record' for any that is missing — never invent one.")


RATIOS_RULE = (
    " Ratios, margins, growth rates, day counts and coverage figures: quote ONLY the "
    "'Computed ratios' table in the locked dataset — never compute one yourself; if a "
    "ratio you need is 'not computable' or absent, say so and name the missing input.")


def section_task(spec: SectionSpec, computed: bool = False) -> str:
    return (f"Write ONLY this part of the CAM: {spec.title}\n\nIts specification:\n{spec.spec}"
            f"\n\nStart with the heading '## {spec.heading}', use '###' for sub-sections and "
            "Markdown tables with a header row. Quote figures from the LOCKED financial "
            "dataset exactly — never recompute or alter them."
            + (RATIOS_RULE if computed else "")
            + " Where the documents are silent, say 'not on record'. Do not write any "
            "other section.")


# ----------------------------------------------------------------------- machine QA
# A figure, also as a multiple ("2.5x") or a percentage ("12.5%").
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?=x\b|[^\w]|$)")
_FAILED = "This section could not be drafted"


def _canon(value: str) -> str | None:
    try:
        f = abs(float(value.replace(",", "")))
    except ValueError:
        return None
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _figures(text: str) -> set[str]:
    return {c for m in _NUM.finditer(text) if (c := _canon(m.group(0)))}


def _traced(raw: str, locked: list[float]) -> bool:
    """A table figure is the dataset's when some dataset value, rounded to the
    figure's own precision, equals it — "1,048.4" in Section 5 is "1,048.43" in the
    dataset shown to one decimal, not a new number."""
    from decimal import ROUND_HALF_UP, Decimal

    places = len(raw.split(".")[1]) if "." in raw else 0
    step = Decimal(1).scaleb(-places)
    shown = Decimal(raw.replace(",", ""))
    # Half-up, as figures are rounded on paper: Python's round() takes 1204.5 to 1204.
    return any(Decimal(str(x)).quantize(step, ROUND_HALF_UP) == shown for x in locked)


def _blocks(draft_md: str) -> list[tuple[str, str]]:
    """(heading, body) for every '## ' part of the draft, in order."""
    out: list[tuple[str, str]] = []
    for part in re.split(r"(?m)^(?=## )", draft_md):
        if part.startswith("## "):
            head, _, body = part.partition("\n")
            out.append((head[3:].strip(), body))
    return out


def _same(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.casefold().replace("&", " and ")).strip()  # noqa: E731
    return norm(a) == norm(b)


def machine_qa(prompt: MasterPrompt, draft_md: str, workpaper_md: str, *,
               ratios_from_code: bool | None = None, ratios_note: str | None = None,
               implausible: list[str] | None = None, untraced: list[str] | None = None,
               ) -> dict[str, Any]:
    """The master prompt's Step 7, the part a program can check — before an analyst
    reads the draft. Every check is named with what it found; a failure is reported,
    never silently fixed, so the analyst knows exactly what to look at.

    - every section and annexure of the prompt is present, and none twice;
    - no section was left as a "could not be drafted" placeholder;
    - every figure in the Financial Analysis tables is in the locked dataset;
    - each section's length is near the prompt's word range."""
    checks: list[dict[str, Any]] = []
    blocks = _blocks(draft_md)

    missing = [s.heading for s in prompt.sections
               if not any(_same(h, s.heading) for h, _b in blocks)]
    checks.append({"check": "All sections and annexures present",
                   "status": "fail" if missing else "pass",
                   "detail": ("missing: " + "; ".join(missing)) if missing
                   else f"{len(prompt.sections)} of {len(prompt.sections)}"})

    dupes = sorted({s.heading for s in prompt.sections
                    if sum(_same(h, s.heading) for h, _b in blocks) > 1})
    checks.append({"check": "No section duplicated", "status": "fail" if dupes else "pass",
                   "detail": ("twice: " + "; ".join(dupes)) if dupes else "none"})

    failed = [h for h, b in blocks if _FAILED in b]
    checks.append({"check": "Every section drafted", "status": "fail" if failed else "pass",
                   "detail": ("not drafted: " + "; ".join(failed)) if failed else "all"})

    fin = next((b for h, b in blocks if "FINANCIAL ANALYSIS" in h.upper()), None)
    if fin is None:
        checks.append({"check": "Financial Analysis figures match the locked dataset",
                       "status": "warn", "detail": "no Financial Analysis section"})
    else:
        locked = [float(v) for v in _figures(workpaper_md)]
        cells = [c for ln in fin.splitlines() if ln.lstrip().startswith("|")
                 and not re.fullmatch(r"[\s|:\-]+", ln) for c in ln.split("|")]
        figs = []
        for c in cells:
            for m in _NUM.finditer(c):
                raw = m.group(0)
                v = _canon(raw)
                # Years and small counts are labels, not figures.
                if v is None or re.fullmatch(r"(19|20)\d\d", raw) or ("." not in raw
                                                                       and float(v) < 100):
                    continue
                figs.append((raw, v))
        off = sorted({raw for raw, _v in figs if not _traced(raw, locked)})
        checks.append({
            "check": "Financial Analysis figures match the locked dataset",
            "status": "fail" if off else ("pass" if figs else "warn"),
            "detail": (f"{len(off)} of {len(figs)} not in the dataset (computed in the "
                       "section, not quoted): " + ", ".join(off[:12])
                       + (" …" if len(off) > 12 else "")) if off
            else (f"all {len(figs)} traced" if figs else "no table figures found")})

    lengths = []
    for s in prompt.sections:
        rng = s.word_range
        body = next((b for h, b in blocks if _same(h, s.heading)), None)
        if not rng or body is None or _FAILED in body:
            continue
        words = len(re.findall(r"\w+", "\n".join(ln for ln in body.splitlines()
                                                 if not ln.lstrip().startswith("|"))))
        if words < rng[0] * 0.7 or words > rng[1] * 1.3:
            lengths.append(f"{s.heading.split(':')[0]}: {words} words "
                           f"(asked {rng[0]}–{rng[1]})")
    checks.append({"check": "Section lengths near the prompt's ranges",
                   "status": "warn" if lengths else "pass",
                   "detail": "; ".join(lengths) if lengths else "within range"})

    if ratios_from_code is not None:
        checks.append({"check": "Ratios computed by PRISM (A3.5 locked formulas)",
                       "status": "pass" if ratios_from_code else "warn",
                       "detail": "from the locked figures" if ratios_from_code else
                       f"{ratios_note or 'no figures block'} — ratios in this draft are "
                       "the model's own arithmetic; verify them"})
    if ratios_from_code:
        flags = implausible or []
        # Growth can be real (a first full year, a start-up) as easily as a unit slip:
        # it is flagged for a look. Days, leverage, coverage and liquidity beyond any
        # working business are an extraction problem until shown otherwise: they fail.
        growth = [f for f in flags if f.startswith(("Revenue growth", "Revenue CAGR"))]
        bad = [f for f in flags if f not in growth]
        checks.append({"check": "Ratios plausible (no likely unit or period slip)",
                       "status": "fail" if bad else ("warn" if growth else "pass"),
                       "detail": "; ".join(filter(None, [
                           ("check the inputs' unit and period: " + "; ".join(bad[:8])
                            + (" …" if len(bad) > 8 else "")) if bad else "",
                           ("growth to confirm — a first year, or a unit slip? "
                            + "; ".join(growth)) if growth else ""])) or "all in range"})
        checks.append({"check": "Extracted figures traced to the workpaper tables",
                       "status": "warn" if untraced else "pass",
                       "detail": ("dropped, not in the tables: " + "; ".join((untraced or [])[:8]))
                       if untraced else "all traced"})

    gaps = len(re.findall(r"not on record", draft_md, re.I))
    checks.append({"check": "Facts marked 'not on record'", "status": "info",
                   "detail": f"{gaps} place(s) — the documents were silent there"})
    return {"passed": not any(c["status"] == "fail" for c in checks), "checks": checks}


_NOT_HELD = re.compile(r"not available|not configured|unreachable|not checked|none found"
                       r"|HTTP [45]\d\d|no answer", re.I)


def unverify_unavailable(table_md: str) -> str:
    """A check cannot be "verified" by a source PRISM did not have: a row marked
    Verified / Partly verified whose finding says the source was not available is
    put back to NOT PERFORMED — enforced here, not left to the model."""
    out = []
    for ln in table_md.splitlines():
        cells = ln.split("|")
        if len(cells) >= 6 and re.search(r"\bverified\b", cells[3], re.I) \
                and _NOT_HELD.search(cells[4]):
            cells[3] = " NOT PERFORMED — the source was not available to PRISM "
            ln = "|".join(cells)
        out.append(ln)
    return "\n".join(out)


def qa_markdown(qa: dict[str, Any]) -> str:
    rows = ["## Machine QA report", "",
            f"**Result:** {'passed' if qa.get('passed') else 'FAILED — see below'}", "",
            "| Check | Status | Detail |", "| --- | --- | --- |"]
    rows += [f"| {c['check']} | {c['status'].upper()} | {c['detail']} |"
             for c in qa.get("checks", [])]
    return "\n".join(rows)


# -------------------------------------------------------------------------- engine
Engine = Callable[[str, list[dict[str, Any]]], Awaitable[str]]
Progress = Callable[[str, int, int], None]


@dataclass
class DraftResult:
    draft_md: str
    workpaper_md: str
    notes: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    qa: dict[str, Any] = field(default_factory=dict)
    figures: dict[str, Any] = field(default_factory=dict)   # what the ratios came from


class DigestCache:
    """Fact sheets are deterministic in (model, rules, document text): reuse them."""

    def __init__(self, size: int = 512) -> None:
        self._d: OrderedDict[str, str] = OrderedDict()
        self.size = size

    @staticmethod
    def key(*parts: str) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(p.encode("utf-8", "ignore") + b"\x00")
        return h.hexdigest()

    def get(self, key: str) -> str | None:
        if key in self._d:
            self._d.move_to_end(key)
        return self._d.get(key)

    def put(self, key: str, value: str) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.size:
            self._d.popitem(last=False)


async def draft_staged(*, generate: Engine, system: str, prompt: MasterPrompt,
                       docs: list[SourceDoc], analyst_notes: str, cache: DigestCache,
                       model_id: str, progress: Progress, concurrency: int = 4,
                       call_budget: int = 360_000, digest_part_chars: int = 90_000,
                       deal_context: str = "", public_context: str = "",
                       today: str = "") -> DraftResult:
    gate = asyncio.Semaphore(max(1, concurrency))
    result = DraftResult(draft_md="", workpaper_md="")
    base_system = f"{system}\n\n===== THE CREDIT TEAM'S RULES (Block A) =====\n{prompt.rules}"

    async def call(user: Any, label: str, *, sys: str = base_system) -> str:
        async with gate:
            result.calls += 1
            with stage(label):
                return await generate(sys, [{"role": "user", "content": user}])

    async def call_retry(user: Any, label: str) -> str:
        try:
            return await call(user, label)
        except RuntimeError:
            return await call(user, label)              # one retry: transient provider errors

    # 1. DIGEST -------------------------------------------------------------------------
    jobs = [(d, i, part) for d in docs
            for i, part in enumerate(split_pages(d.text, digest_part_chars))]
    done = 0
    progress("Reading documents", 0, len(jobs))
    phase_a = prompt.phases.get("A", "")

    async def digest(doc: SourceDoc, idx: int, part: str) -> tuple[SourceDoc, str, str]:
        """(doc, fact sheet or "", error or "")."""
        nonlocal done
        attached = repr(doc.blocks)[:4096] if doc.blocks else ""
        key = cache.key(model_id, prompt.rules, phase_a, DIGEST_TASK, part,
                        hashlib.sha256(attached.encode()).hexdigest() if attached else "")
        sheet, err = cache.get(key) or "", ""
        if not sheet:
            label = doc.name + (f" (part {idx + 1})" if idx else "")
            ask = (f"===== DOCUMENT: {label} =====\n"
                   + (part or "(the document is the attached scan)") + "\n\n"
                   f"===== EXTRACTION INSTRUCTIONS (Phase A) =====\n{phase_a}\n\n{DIGEST_TASK}")
            content: Any = [*doc.blocks, {"type": "text", "text": ask}] if doc.blocks else ask
            try:
                sheet = await call_retry(content, "digest")
                cache.put(key, sheet)
            except RuntimeError as exc:
                sheet, err = "", str(exc)
        done += 1
        progress("Reading documents", done, len(jobs))
        return doc, sheet, err

    sheets: dict[str, list[str]] = {d.doc_id: [] for d in docs}
    for doc, sheet, err in await asyncio.gather(*(digest(d, i, p) for d, i, p in jobs)):
        if sheet:
            sheets[doc.doc_id].append(sheet)
        else:
            result.notes.append({"doc_id": doc.doc_id, "document": doc.name,
                                 "reason": f"fact sheet failed: {err}"})
    fact_sheets = [f"===== FACT SHEET: {d.name} =====\n" + "\n\n".join(sheets[d.doc_id])
                   for d in docs if sheets.get(d.doc_id)]
    head = manifest(docs)
    notes_block = (f"===== ANALYST'S CONVERSATION & INSTRUCTIONS (the documents and the "
                   f"dataset take precedence on facts) =====\n{analyst_notes}"
                   if analyst_notes.strip() else "")
    # The borrower and the line as the Register holds them: identity (CIN, PAN, GSTIN,
    # constitution), the amount, product, RM and analyst. Page 1, the title page and
    # Annexure I were "not on record" without it.
    deal_block = (f"===== THE DEAL ON THE REGISTER (facts of record) =====\n{deal_context}"
                  if deal_context.strip() else "")
    # What PRISM itself holds from public sources — Tracxn's filings (cached, never a
    # paid fetch from here) and PULSE's news — so checks can be "verified via", cited.
    public_block = (f"===== PUBLIC SOURCES PRISM HOLDS (cite as given; documents take "
                    f"precedence on the borrower's own figures) =====\n{public_context}"
                    if public_context.strip() else "")
    notes_block = "\n\n".join(b for b in (deal_block, public_block, notes_block) if b)

    # 2. CONSOLIDATE + public checks ------------------------------------------------------
    progress("Locking the financial dataset", 0, 1)
    ctx_budget = max(call_budget - len(base_system) - 20_000, 40_000)
    dataset_ctx = "\n\n".join([head, *fit(fact_sheets, ctx_budget), notes_block])
    phase_c = prompt.phases.get("C", "")
    dataset_task = call_retry(f"{dataset_ctx}\n\n===== FORENSIC ANALYSIS (Phase C) =====\n"
                              f"{phase_c}\n\n{DATASET_TASK}\n\n{FIGURES_TASK}", "dataset")
    checks_task = call_retry(f"{head}\n\n{public_block}\n\n===== PUBLIC INFORMATION "
                             f"(Phase B) =====\n{prompt.phases.get('B', '')}\n\n{CHECKS_TASK}",
                             "public_checks")
    dataset, checks = await asyncio.gather(dataset_task, checks_task, return_exceptions=True)
    if isinstance(dataset, BaseException):
        raise RuntimeError(f"The financial dataset could not be built: {dataset}")
    # The ratios are PRISM's: computed from the figures the dataset step locked, and
    # added to the dataset every section quotes.
    figures, dataset, why_not = parse_figures(dataset)
    implausible: list[str] = []
    untraced: list[str] = []
    if figures is not None:
        untraced = trace(figures, dataset)
        implausible = plausibility(figures, compute(figures))
        dataset = f"{dataset.rstrip()}\n\n{ratios_markdown(figures)}\n"
        if untraced:
            dataset += ("\n**Figures not in the workpaper's tables, so not used:** "
                        + "; ".join(untraced) + "\n")
        result.figures = {"subordinated": figures.subordinated,
                          "periods": [{"period": p.label, "basis": p.basis, **p.v}
                                      for p in figures.periods]}
    computed = figures is not None       # if not, Machine QA says why (why_not)
    result.workpaper_md = dataset
    progress("Locking the financial dataset", 1, 1)

    # 3. BODY sections ------------------------------------------------------------------
    locked = f"===== LOCKED FINANCIAL DATASET (quote exactly) =====\n{dataset}"
    qa = f"===== QUALITY BAR (read-through checklist) =====\n{prompt.qa}" if prompt.qa else ""
    body_specs = [s for s in prompt.sections if not s.final]
    final_specs = [s for s in prompt.sections if s.final]
    written: dict[int, str] = {}
    total = len(prompt.sections)
    progress("Writing sections", 0, total)

    def section_ctx(extra: list[str], spare: int) -> str:
        return "\n\n".join([head, *fit(fact_sheets, spare), *extra])

    async def write(spec: SectionSpec, context: str) -> None:
        try:
            text = await call_retry(f"{context}\n\n{section_task(spec, computed)}",
                                    "final_section" if spec.final else "section")
        except RuntimeError as exc:
            text = (f"## {spec.heading}\n\n> This section could not be drafted ({exc}). "
                    "Regenerate, or write it by hand.")
            result.notes.append({"section": spec.title, "reason": str(exc)})
        if not text.lstrip().startswith("#"):
            text = f"## {spec.heading}\n\n{text}"
        written[spec.order] = text.strip()
        progress("Writing sections", len(written), total)

    fixed = [locked, notes_block, qa]
    spare = max(ctx_budget - sum(len(x) for x in fixed) - len(head), 20_000)
    await asyncio.gather(*(write(s, section_ctx(fixed, spare)) for s in body_specs))

    # 4. FINAL sections, from the finished body ------------------------------------------
    body = "\n\n".join(written[s.order] for s in body_specs)
    body_block = f"===== THE CAM BODY AS WRITTEN =====\n{body}"
    final_ctx = "\n\n".join([head, locked, *fit([body_block], max(ctx_budget - len(locked), 20_000)),
                             notes_block, qa])

    async def title_page() -> str:
        try:
            text = await call_retry(f"{final_ctx}\n\n"
                                    f"{TITLE_TASK.format(today=today or _today())}", "title")
        except RuntimeError as exc:
            result.notes.append({"section": "TITLE PAGE", "reason": str(exc)})
            return ""
        # Plain lines under the document title; a heading the model added is dropped.
        return "\n".join(ln for ln in text.strip().splitlines()
                         if not ln.lstrip().startswith("#")).strip()

    finals = await asyncio.gather(title_page(), *(write(s, final_ctx) for s in final_specs))
    title = str(finals[0] or "")

    # 5. ASSEMBLE -----------------------------------------------------------------------
    parts = [written[s.order] for s in prompt.sections]
    if isinstance(checks, str) and checks.strip():
        checks = unverify_unavailable(checks)
        first_annex = next((n for n, s in enumerate(prompt.sections)
                            if s.title.startswith("ANNEXURE")), len(parts))
        parts.insert(first_annex, checks.strip())
    # Two trailing spaces: a Markdown line break, so the title block stays one line each.
    title_md = "  \n".join(ln.strip() for ln in title.splitlines() if ln.strip())
    result.draft_md = ("# Credit Appraisal Memorandum\n\n" + (f"{title_md}\n\n" if title_md else "")
                       + "\n\n".join(parts) + "\n")

    # 6. MACHINE QA (the prompt's Step 7) -------------------------------------------------
    result.qa = machine_qa(prompt, result.draft_md, dataset, ratios_from_code=computed,
                           ratios_note=why_not, implausible=implausible, untraced=untraced)
    result.workpaper_md = f"{dataset.rstrip()}\n\n{qa_markdown(result.qa)}\n"
    progress("Done", total, total)
    return result


def _today() -> str:
    from datetime import date
    return date.today().strftime("%d-%b-%Y")
