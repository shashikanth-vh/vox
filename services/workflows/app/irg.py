"""The IRG report — the Internal Risk Grade written as a full credit document.

A grade alone (the rubric's score and a line per pillar) read like a scorecard; the
credit team asked for the IRG to be a SUPERSET of the CAM: the same reading of every
document, the same locked financial dataset with PRISM's ratios, and on top of it the
rubric's grade — explained pillar by pillar with the evidence, figures and tables.

So the IRG report is the staged drafter (``cam_pipeline.draft_staged``) run with these
sections instead of the CAM's:

* the documents are read into the SAME fact sheets as the CAM (same rules, same
  extraction instructions, same cache: after a CAM the reading is instant);
* the financial dataset is locked and PRISM computes the ratios, as for the CAM;
* between the dataset and the writing, the rubric grades — in Chitti, on the full fact
  sheets and the locked dataset (not on clips of raw text) — and the grade becomes
  context every section reads;
* one section per rubric pillar explains its score with the evidence, then the risks,
  conditions and what would move the rating; the rating summary is written last.

PRISM's own parts are added in code, never by the model: the scorecard, the hard
triggers, the red flags PRISM read itself, and the compact evidence appendix.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.cam_pipeline import MasterPrompt, SectionSpec

IRG_TITLE = "Internal Risk Grade Report"

# Used only when the tenant has no CAM master prompt: the CAM's rules are preferred
# (the persona, the scales, Rule 9) and they make the fact sheets the CAM's own.
FALLBACK_RULES = (
    "You are a senior credit analyst at Evam Finance, an Indian climate-finance NBFC, "
    "writing for the credit committee. Every figure comes from the documents, the locked "
    "dataset or a cited public source — never invented, estimated or recalled; a missing "
    "figure reads 'not on record'. Rs. lakh, one decimal; ratios two decimals. Take a "
    "position; explain how each risk reaches cash flow.")

_PILLAR_RULE = (
    " Open with the rubric's score for this pillar exactly as THE RISK GRADE block gives it "
    "(score /10, weight, points), then explain it: what in the evidence drives it, with the "
    "figures, and what would raise or lower it. Where the evidence you read contradicts the "
    "rubric's score or its evidence line, say so plainly ('the evidence suggests …; the "
    "analyst should review the score'). Never re-score the pillar yourself.")

# (title, specification, written last) — headings carry the words cam_pipeline uses to
# give each section its own documents whole (FINANCIAL, BANK, BUREAU, DEBT PROFILE …).
SECTIONS: list[tuple[str, str, bool]] = [
    ("RATING SUMMARY (300–450 words + table)",
     "The committee's first page. Open with one line: the rating, the score out of 100, "
     "the confidence and the recommended action, exactly as THE RISK GRADE block states "
     "them. Then 3–4 paragraphs: who the borrower is and what the proposal is; the two or "
     "three facts that decide the grade (with figures); the hard triggers' position; the "
     "decisive risks and mitigants. Then a table 'Key facts' (Item | Value | Basis) of the "
     "8–12 figures a committee member would ask for first (revenue, PBDIT margin, PAT, TNW, "
     "TOL/TNW, DSCR, current ratio, banking conduct, bureau, proposed limit). Close with "
     "an italicised one-sentence verdict. Do not restate every pillar.", True),
    ("SECTION 1: BORROWER PROFILE, MANAGEMENT & PROPOSED FACILITY (400–650 words + tables)",
     "Borrower identity table (legal name, brand, constitution, incorporation/registration, "
     "registered office, PAN/GSTIN/Udyam, sector, line of activity, years in business, "
     "group). Promoters table (Name | Role | Age | Stake / capital | Bureau score | "
     "Background). What the business does in plain terms, from the company profile and "
     "the documents (products, customers, plants, certifications). The proposed facility "
     "as the term sheet words it (amount, type, rate, tenor, security, guarantees). Key-man "
     "and governance observations.", False),
    ("SECTION 2: FINANCIAL ANALYSIS — Pillar A, Financial strength (700–1,100 words + tables)",
     "Open with the locked financial table (Particulars | every period, basis labelled) from "
     "Net Sales through PAT, TNW, debt, current assets and liabilities, receivables, "
     "inventory and payables; then the PRISM computed ratios table as given. Analyse: "
     "revenue quality and trajectory; profitability and margin trend; leverage (TOL/TNW, "
     "Debt/Adj. TNW) and the capital structure; debt service (DSCR, interest cover); "
     "working-capital cycle; cash accruals; projections against history. Name every data "
     "caveat (provisional figures, consistency notes, figures not on record)."
     + _PILLAR_RULE, False),
    ("SECTION 3: CREDIT & BANKING CONDUCT — Pillar B: bank statements, GST, ITR and CIBIL "
     "bureau (600–900 words + tables)",
     "Banking: one month-wise table per account from the PRISM ledger summaries and the "
     "bank-statement analyses (Month | Credits | Debits | Closing | Lowest balance | "
     "Returns), each account's limit and utilisation, overdrawings and limit breaches with "
     "dates and amounts, cheque/ECS/NACH returns, EMI and interest debits, related-party "
     "and cash flows. Turnover triangulation: GST vs reported sales vs bank credits, with "
     "the variance. ITR consistency. Bureau: the entity's commercial report (CMR rank, "
     "facilities, DPD, overdues) and each promoter's score and remarks, in a table. Close "
     "with an italicised assessment of credit discipline." + _PILLAR_RULE, False),
    ("SECTION 4: DEBT PROFILE & SECURITY (300–500 words + tables)",
     "Existing borrowings table (Lender | Facility | Sanctioned | Outstanding | EMI / "
     "repayment | Security | Status) from the sanction letters, debt profiles and the "
     "bureau, reconciled where they differ. Total debt and its mix (secured / unsecured, "
     "bank / NBFC, term / working capital), recent loan stacking, the proposed facility "
     "against it, and the security package with its cover and charge position.", False),
    ("SECTION 5: MARKET VALIDATION — Pillar C: business model, order book, customers and "
     "lenders (400–650 words + tables)",
     "Order book table (Customer | Scope | Value | Date) and customer concentration "
     "(top-1/3/5 shares from the debtor ageing), repeat customers, debtor ageing buckets "
     "and stuck debtors; suppliers and input dependence; what other lenders' sanctions and "
     "the Register's deal lines say about the borrower's standing (approved, declined, "
     "pending)." + _PILLAR_RULE, False),
    ("SECTION 6: NEWS, LEGAL & COMPLIANCE — Pillar D, Reputation (300–500 words + tables)",
     "The legal / litigation report's findings by court and forum; statutory compliance "
     "(GST registration and filing, MSME, ITR); the news (PULSE) and the intelligence on "
     "the Register (SEBI, IBBI, NSE, adverse findings) in a table (Date | Source | Finding "
     "| Signal), each judged for whether it is about this borrower. Say plainly when a "
     "search found nothing." + _PILLAR_RULE, False),
    ("SECTION 7: DOCUMENTATION & TRANSPARENCY — Pillar E (200–350 words + table)",
     "What the file holds by kind (financials, ITRs, bank statements and analyses, bureau, "
     "KYC, constitution, sanction letters, orders, legal) and what it lacks for this "
     "proposal, as a table (Kind | On record | Gap). Inconsistencies between documents."
     + _PILLAR_RULE, False),
    ("SECTION 8: ENGAGEMENT & PROCESS HEALTH — Pillar F (150–300 words)",
     "The deal's stage on the Register and how long it has sat there, the interactions and "
     "their content, the responsiveness of the borrower, open items." + _PILLAR_RULE, False),
    ("SECTION 9: SECTOR & STRUCTURE — Pillar G, Industry (200–350 words)",
     "The borrower's sector and its risk, the structure of the proposal (ask against net "
     "worth and turnover, tenor against the cash cycle), concentration and climate "
     "relevance." + _PILLAR_RULE, False),
    ("SECTION 10: KEY RISKS, MITIGANTS, CONDITIONS & RECOMMENDATION (450–700 words + tables)",
     "A risk table (Risk | Severity | How it reaches cash flow | Mitigant), 6–10 rows, each "
     "tied to a figure. Conditions precedent and monitoring covenants, each measurable (a "
     "threshold and a trigger). Documents to obtain before sanction. What would move the "
     "rating up a band and down a band. Close with the recommendation as the rubric states "
     "it, and the analyst's reasons.", True),
]


def irg_prompt(cam_prompt: MasterPrompt | None) -> MasterPrompt:
    """The IRG's sections under the CAM's rules and extraction phases (so its fact
    sheets are the CAM's, cached), or fallback rules when there is no CAM prompt."""
    sections = [SectionSpec(title=t, spec=spec, order=n, final=final)
                for n, (t, spec, final) in enumerate(SECTIONS)]
    if cam_prompt is None:
        return MasterPrompt(rules=FALLBACK_RULES, phases={}, sections=sections, qa="")
    return MasterPrompt(rules=cam_prompt.rules, phases=dict(cam_prompt.phases),
                        sections=sections, qa="", preamble=cam_prompt.preamble)


# ------------------------------------------------------------------ the grade as context
def grade_context(g: dict[str, Any]) -> str:
    """THE RISK GRADE block every IRG section reads: the rubric's result, verbatim."""
    rows = [f"Rating: {g.get('label')} · score {g.get('score')}/100 · confidence "
            f"{g.get('confidence_pct')}% · recommended action: {g.get('action')}",
            f"Verdict: {g.get('verdict') or '—'}", "", "Pillars (score /10, weight):"]
    for p in g.get("pillars") or []:
        rows.append(f"- {p.get('key')}. {p.get('name')}: "
                    + (f"{p.get('score')}/10 × {p.get('weight')}% = {p.get('weighted')} points"
                       if p.get("available") else "no data — left out of the score")
                    + f" — evidence: {p.get('evidence') or '—'}")
    rows += ["", "Hard red triggers:"]
    rows += [f"- {t.get('trigger')}: {t.get('status')} — {t.get('evidence') or '—'}"
             for t in g.get("hard_triggers") or []]
    for key, label in (("risks", "Risks"), ("mitigants", "Mitigants"),
                       ("conditions", "Conditions"), ("data_gaps", "Data gaps")):
        if g.get(key):
            rows += ["", f"{label}:", *[f"- {x}" for x in g[key]]]
    for key, label in (("move_up", "Up a band"), ("move_down", "Down a band")):
        if g.get(key):
            rows.append(f"{label}: {g[key]}")
    return ("===== THE RISK GRADE (the rubric's result — explain it, never re-score it) "
            "=====\n" + "\n".join(rows))


# ----------------------------------------------------------- PRISM's own report parts
def _cell(v: Any) -> str:
    return str(v if v is not None else "—").replace("|", "/").replace("\n", " ").strip() or "—"


_SEV = {"red": "RED", "amber": "AMBER", "info": "INFO"}


def scorecard_md(g: dict[str, Any], flags: list[dict[str, Any]]) -> str:
    """The scorecard, the hard triggers and PRISM's own flags — in code, from the grade."""
    out = ["### Scorecard", "",
           "| Pillar | Weight | Score /10 | Points | Basis |", "| --- | --- | --- | --- | --- |"]
    for p in g.get("pillars") or []:
        avail = p.get("available")
        out.append(f"| {p.get('key')}. {_cell(p.get('name'))} | {p.get('weight')}% | "
                   f"{p.get('score') if avail else '—'} | "
                   f"{p.get('weighted') if avail else 'left out (no data)'} | "
                   f"{_cell((p.get('evidence') or '')[:220])} |")
    out += ["", f"**Total: {g.get('score')}/100 — {g.get('label')}** · bands GREEN ≥ 70 · "
            "AMBER 45–69 · RED < 45 · any hard trigger → RED · confidence "
            f"{g.get('confidence_pct')}%.", "", "### Hard red triggers", "",
            "| Trigger | Status | Evidence |", "| --- | --- | --- |"]
    status = {"Hit": "**HIT**", "Clear": "Clear", "Unknown": "Unverified"}
    out += [f"| {_cell(t.get('trigger'))} | {status.get(t.get('status'), t.get('status'))} | "
            f"{_cell(t.get('evidence'))} |" for t in g.get("hard_triggers") or []]
    if flags:
        out += ["", "### Red and amber flags PRISM read itself", "",
                "| Severity | Area | Finding | File |", "| --- | --- | --- | --- |"]
        out += [f"| {_SEV.get(str(f.get('severity')), f.get('severity'))} | "
                f"{_cell(f.get('area'))} | {_cell(f.get('title'))} — {_cell(f.get('detail'))} | "
                f"{_cell(f.get('source'))} |" for f in flags]
    return "\n".join(out)


def appendix_md(g: dict[str, Any], fates: list[dict[str, Any]], kinds: dict[str, list[str]],
                records: dict[str, int], qa_md: str, workpaper: str) -> str:
    """The appendices: how the score was built, where the rules corrected the model,
    the evidence read (compact, by kind), the machine QA and the financial workpaper."""
    out = ["## APPENDIX A: HOW THE SCORE WAS BUILT", ""]
    c = g.get("confidence_detail") or {}
    out.append(f"Confidence {g.get('confidence_pct')}% = data coverage "
               f"{c.get('data_coverage', '—')}% and agreement {c.get('agreement', '—')}% "
               "between independent readings"
               + (f" ({', '.join(str(r.get('score')) for r in g.get('runs') or [])}; each "
                  "pillar takes the median)." if g.get("runs") else "."))
    if g.get("adjustments"):
        out += ["", "**Where the rubric's rules corrected the model:**", ""]
        out += [f"- {a}" for a in g["adjustments"]]
    out += ["", "## APPENDIX B: EVIDENCE READ", ""]
    used = [f for f in fates if f.get("used")]
    out.append(f"{len(used)} of {len(fates)} file(s) read; from the Register: "
               + "; ".join(f"{n} {k}" for k, n in records.items()) + ".")
    out += ["", "| Kind | Files read |", "| --- | --- |"]
    out += [f"| {k} | {len(v)}: {_cell('; '.join(v[:8]) + (' …' if len(v) > 8 else ''))} |"
            for k, v in sorted(kinds.items())]
    named = [f for f in fates if not f.get("used") or f.get("reason")]
    if named:
        out += ["", "**Not read, and why:**", ""]
        out += [f"- {f['name']}: {f.get('reason') or 'not read'}" for f in named]
    if qa_md.strip():
        out += ["", "## APPENDIX C: MACHINE QA", "", qa_md.replace("## Machine QA report", "").strip()]
    if workpaper.strip():
        wp = re.sub(r"(?m)^## ", "### ", workpaper.strip())
        out += ["", "## APPENDIX D: FINANCIAL DATASET WORKPAPER", "", wp]
    return "\n".join(out)


def assemble(draft_md: str, g: dict[str, Any], flags: list[dict[str, Any]],
             appendix: str) -> str:
    """The draft with the scorecard after the rating summary and the appendices last;
    the title block gains the grade line (the cover shows it)."""
    lines = draft_md.splitlines()
    first = next((n for n, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
    grade_line = (f"Risk grade: {g.get('label')} — {g.get('score')}/100 · confidence "
                  f"{g.get('confidence_pct')}% · {g.get('action')}  ")
    head, body = lines[:first], "\n".join(lines[first:])
    if head and head[-1].strip() == "":
        head = head[:-1] + [grade_line, ""]
    else:
        head = head + [grade_line, ""]
    card = scorecard_md(g, flags)
    m = re.search(r"(?m)^## (?!RATING SUMMARY).+$", body)
    body = (body[:m.start()] + card + "\n\n" + body[m.start():]) if m else body + "\n\n" + card
    return "\n".join(head) + "\n" + body.rstrip() + "\n\n" + appendix + "\n"


def documents_for_grade(typed: list[tuple[str, str]], dataset: str,
                        budget: int = 300_000, per_doc: int = 40_000) -> list[dict[str, Any]]:
    """The grader's evidence: each document's FACT SHEET (dense, every figure the
    document states) instead of a clip of its raw text, and the locked dataset first."""
    from app.risk_facts import mask_ids
    from app.risk_grade import share_budget

    # Graded beside the dataset step (no dataset yet): the fact sheets alone, which
    # carry every figure the dataset would lock.
    docs = ([{"name": "PRISM — locked financial dataset and computed ratios",
              "section": "Financials", "title": "workpaper", "engines": ["prism"],
              "text": mask_ids(dataset[:60_000])}] if dataset.strip() else [])
    sheets = []
    for kind, sheet in typed:
        name = sheet.splitlines()[0].replace("===== FACT SHEET:", "").strip(" =") or "document"
        sheets.append((name, kind, sheet))
    shares = share_budget([len(s) for _n, _k, s in sheets], per_doc,
                          max(budget - sum(len(d["text"]) for d in docs), 20_000))
    for (name, kind, sheet), room in zip(sheets, shares, strict=True):
        docs.append({"name": name, "section": kind, "title": "fact sheet",
                     "engines": ["prism fact sheet"], "text": mask_ids(sheet[:room])})
    return docs[:150]


def deal_text(p: dict[str, Any]) -> str:
    """The panorama as the writers read it: identity, the lines, the stats."""
    keep = {k: p.get(k) for k in ("anchor", "stats", "lending", "deals", "syndication",
                                  "contacts", "post_disbursement") if p.get(k)}
    return json.dumps(keep, ensure_ascii=False, default=str)[:40_000]
