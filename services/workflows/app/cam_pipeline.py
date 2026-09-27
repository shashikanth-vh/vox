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
  4. FINAL        Executive Summary and Recommendations, written last from the finished
                  body so they agree with it.
  5. ASSEMBLE     the sections in the prompt's order.

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

from app.cam_telemetry import stage

# ----------------------------------------------------------------------------- prompt
_BLOCK = re.compile(r"^BLOCK\s+([A-Z])\b")
_PHASE = re.compile(r"^PHASE\s+([A-Z])\b")
_SECTION = re.compile(r"^(SECTION\s+\d+|ANNEXURE\s+[IVXL]+)\s*:")      # case-sensitive
_QA = re.compile(r"^(JUDGMENT QA|QA CHECKLIST|READ-THROUGH CHECKLIST)\b", re.I)
_FINAL = re.compile(r"EXECUTIVE SUMMARY|RECOMMENDATION", re.I)
# "(350–500 words + snapshot table)" is guidance for the writer, not part of the heading.
_HINT = re.compile(r"\s*\((?=[^()]*(?:\bwords?\b|\btables?\b|prose))[^()]*\)\s*$", re.I)


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
    "convention, one column per period, each figure source-tagged; (2) every ratio the "
    "prompt defines, using ONLY its locked formula definitions, with inputs and formula "
    "shown; (3) the debt structure map and debtor book map; (4) triangulation across "
    "documents against the prompt's variance thresholds, naming every conflict; (5) the "
    "stress tests the prompt specifies, with post-stress DSCR; (6) the Analyst Base Case "
    "if the prompt's trigger applies. Where documents disagree, show both values and the "
    "one you adopt with the reason. Mark any figure you could not source 'not on record'. "
    "Output Markdown, headed '## Financial dataset workpaper'.")

CHECKS_TASK = (
    "From the public-information phase above, list every public check the prompt requires "
    "for this borrower (at least the minimum it sets), as one Markdown table: Check | "
    "Source / query to run | Status. Status is 'NOT PERFORMED — to be completed by the "
    "analyst' for every row: this environment has no web or registry access. Head it "
    "'## Public information checks — NOT PERFORMED'. Output only that section.")


def section_task(spec: SectionSpec) -> str:
    return (f"Write ONLY this part of the CAM: {spec.title}\n\nIts specification:\n{spec.spec}"
            f"\n\nStart with the heading '## {spec.heading}', use '###' for sub-sections and "
            "Markdown tables with a header row. Quote figures from the LOCKED financial "
            "dataset exactly — never recompute or alter them. Where the documents are "
            "silent, say 'not on record'. Do not write any other section.")


# -------------------------------------------------------------------------- engine
Engine = Callable[[str, list[dict[str, Any]]], Awaitable[str]]
Progress = Callable[[str, int, int], None]


@dataclass
class DraftResult:
    draft_md: str
    workpaper_md: str
    notes: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0


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
                       ) -> DraftResult:
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

    # 2. CONSOLIDATE + public checks ------------------------------------------------------
    progress("Locking the financial dataset", 0, 1)
    ctx_budget = max(call_budget - len(base_system) - 20_000, 40_000)
    dataset_ctx = "\n\n".join([head, *fit(fact_sheets, ctx_budget), notes_block])
    phase_c = prompt.phases.get("C", "")
    dataset_task = call_retry(f"{dataset_ctx}\n\n===== FORENSIC ANALYSIS (Phase C) =====\n"
                              f"{phase_c}\n\n{DATASET_TASK}", "dataset")
    checks_task = call_retry(f"{head}\n\n===== PUBLIC INFORMATION (Phase B) =====\n"
                             f"{prompt.phases.get('B', '')}\n\n{CHECKS_TASK}", "public_checks")
    dataset, checks = await asyncio.gather(dataset_task, checks_task, return_exceptions=True)
    if isinstance(dataset, BaseException):
        raise RuntimeError(f"The financial dataset could not be built: {dataset}")
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
            text = await call_retry(f"{context}\n\n{section_task(spec)}",
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
    await asyncio.gather(*(write(s, final_ctx) for s in final_specs))

    # 5. ASSEMBLE -----------------------------------------------------------------------
    parts = [written[s.order] for s in prompt.sections]
    if isinstance(checks, str) and checks.strip():
        first_annex = next((n for n, s in enumerate(prompt.sections)
                            if s.title.startswith("ANNEXURE")), len(parts))
        parts.insert(first_annex, checks.strip())
    result.draft_md = "# Credit Appraisal Memorandum\n\n" + "\n\n".join(parts) + "\n"
    progress("Done", total, total)
    return result
