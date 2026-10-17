"""Evidence — what a story ASSERTS, read from its headline and snippet.

Negative news is not a finding. "SEBI probes promoter" and "SEBI bars promoter for two
years" are both UGLY to triage, and they are different things to a credit committee: the
first is an allegation that may come to nothing, the second is an order with a date on it.
Grading the two the same is how a desk ends up declining a borrower over a rumour, or
waving through one with a live ban.

So each story is read for:

  * ``nature``  — allegation (claimed, probed, accused) | action (a regulator, court or
    lender has DONE something: an order, a ban, an attachment, a petition admitted) |
    outcome (it ended: convicted, upheld, settled — or cleared, acquitted, quashed);
  * ``stage``   — pending | final | appeal | unknown;
  * ``forum``   — who: SEBI, RBI, ED, CBI, NCLT, a High Court …;
  * ``amount``  — the money named, as printed ("Rs 120 crore"), and in crore when the
    unit is clear;
  * ``favourable`` — the outcome went the firm's way (cleared, acquitted, quashed).

Rules, not a model, for the same reason triage is rules: when the desk asks why a story
was called an order, the answer is the word it matched. And it says it is a reading of
the headline (``basis``): nothing here has opened the article.
"""

from __future__ import annotations

import re
from typing import Any

# Who acted. Order matters only for display — every forum named is listed.
FORUMS: list[tuple[str, str]] = [
    ("SEBI", r"sebi|securities and exchange board"),
    ("RBI", r"rbi|reserve bank"),
    ("ED", r"\bed\b|enforcement directorate"),
    ("CBI", r"cbi|central bureau of investigation"),
    ("SFIO", r"sfio|serious fraud investigation"),
    ("NCLAT", r"nclat"),
    ("NCLT", r"nclt|national company law tribunal"),
    ("IBBI", r"ibbi|insolvency and bankruptcy board"),
    ("Supreme Court", r"supreme court|\bsc\b(?= (?:order|ruling|verdict|stays|upholds))"),
    ("High Court", r"high court|\bhc\b"),
    ("Income Tax", r"income[- ]tax|\bit department|\bit dept|\bit raid|\bit survey"),
    ("GST / DGGI", r"\bgst\b|dggi|directorate general of gst"),
    ("MCA / RoC", r"\bmca\b|ministry of corporate affairs|registrar of companies|\broc\b"),
    ("CCI", r"\bcci\b|competition commission"),
    ("Police / EOW", r"\bpolice\b|\beow\b|economic offences wing|\bfir\b"),
    ("DRT", r"\bdrt\b|debt recovery tribunal"),
    ("Exchange", r"\bnse\b|\bbse\b|stock exchange"),
    ("Lender", r"\blenders?\b|\bbank(?:s)?\b(?= (?:declares?|classif|recall|invok))"),
]
# Whole words: "arbitrator" contains "rbi", and a Supreme Court arbitration was read as
# an RBI action (cipher-test, 16 Oct).
_FORUM_RES = [(name, re.compile(rf"\b(?:{rx})\b", re.I)) for name, rx in FORUMS]

# An OUTCOME that went the firm's way. Checked before anything else: "cleared of fraud"
# is not a fraud allegation.
FAVOURABLE = [
    "acquitted", "acquittal", "cleared of", "exonerated", "quashed", "quashes",
    "clean chit", "set aside", "sets aside", "dismisses plea against", "relief to",
    "withdrawn", "withdraws case", "closure report", "discharged",
]
# An outcome against the firm — the matter is decided (subject to appeal).
ADVERSE_OUTCOME = [
    "convicted", "conviction", "sentenced", "found guilty", "upholds", "upheld",
    "liquidation order", "ordered liquidation", "ordered to be wound up", "wound up",
    "declared wilful defaulter", "declared fraud", "classified as fraud",
    "settles with sebi", "settlement order", "consent order", "pays penalty",
    "resolution plan approved",
]
# A regulator, court or lender has DONE something — an order, not a claim.
ACTION = [
    "bars", "barred", "banned", "bans", "debars", "debarred", "restrains", "restrained",
    "penalty", "penalised", "penalized", "fined", "fines", "imposes", "levies",
    "attaches", "attached", "attachment", "seizes", "seized", "freezes", "frozen",
    "arrested", "arrests", "chargesheet", "charge sheet", "chargesheeted",
    "admits insolvency", "admitted insolvency", "admits plea", "admitted to cirp",
    "insolvency proceedings", "cirp", "moratorium", "liquidator appointed",
    # Not "orders" or "declares": "bags 300 MW orders", "declares dividend".
    "order against", "ordered", "directs", "directed", "show cause",
    "show-cause", "notice", "summons", "summoned", "raid", "raids", "raided", "searches",
    "files fir", "fir registered", "fir filed", "lookout", "recovery proceedings",
    "demand notice", "suspends", "suspended", "cancels licence",
    "cancelled licence", "revokes", "revoked",
]
# Claimed, probed or reported — not (yet) established.
ALLEGATION = [
    "alleged", "allegedly", "allegation", "allegations", "accused", "accuses",
    "probe", "probes", "probing", "investigation", "investigating", "under scanner",
    "under lens", "scrutiny", "complaint", "complaints", "whistleblower", "suspected",
    "suspicion", "claims", "claimed", "reportedly", "questioned", "questions",
    "petition", "plea", "sued", "lawsuit", "files suit", "moves court", "moves nclt",
    "drags", "seeks",
]
# Where the matter stands, when the headline says.
APPEAL = ["appeal", "appeals", "appealed", "challenges", "challenged", "moves sat",
          "moves nclat", "stay on", "stays", "stayed"]
FINAL = ["final order", "convicted", "sentenced", "upholds", "upheld", "wound up",
         "liquidation order", "settlement order", "consent order", "acquitted",
         "quashed", "dismissed"]


def _wre(words: list[str]) -> re.Pattern[str]:
    parts = [re.escape(w).replace(r"\ ", r"[\s-]+") for w in words]
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.I)


_FAV, _ADV_OUT, _ACTION = _wre(FAVOURABLE), _wre(ADVERSE_OUTCOME), _wre(ACTION)
# What a NAMED forum does when it acts. Alone these words are ambiguous ("bags orders",
# "appoints CFO"); with a court, regulator or agency in the headline they are an act —
# "NCLT Ahmedabad … Orders Vehicle Return", "CBI registers case against founders".
_FORUM_ACT = _wre(["order", "orders", "directs", "declines", "refuses", "rejects",
                   "appoints", "admits", "registers case", "registered a case",
                   "case registered", "books", "booked", "issues notice", "imposes",
                   "freezes", "attaches", "summons"])
_ALLEG, _APPEAL, _FINAL = _wre(ALLEGATION), _wre(APPEAL), _wre(FINAL)

# "Rs 1,200 crore", "₹45 cr", "INR 3.5 lakh crore", "Rs. 80 lakh", "$12 million"
_AMOUNT = re.compile(
    r"(?:(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(lakh\s+crore|lakh|lac|crore|cr|"
    r"million|mn|billion|bn)?\b)"
    r"|(?:\$\s*([\d,]+(?:\.\d+)?)\s*(million|mn|billion|bn)\b)"
    r"|(?:\b([\d,]+(?:\.\d+)?)\s*(lakh\s+crore|crore|cr)\b)",
    re.I)
# To crore. Dollars are left in the printed form: the rate on the story's day is not ours
# to guess.
_TO_CRORE = {"lakh crore": 100000.0, "crore": 1.0, "cr": 1.0, "lakh": 0.01, "lac": 0.01,
             "million": 0.1, "mn": 0.1, "billion": 100.0, "bn": 100.0}


def _amount(text: str) -> tuple[str, float | None]:
    """The largest sum the text names, as printed, and in crore when the unit is a
    rupee unit. Largest because a headline with two figures ("Rs 40 crore of Rs 900
    crore loan") is about the bigger exposure more often than not."""
    best: tuple[str, float | None] = ("", None)
    best_cr = -1.0
    for m in _AMOUNT.finditer(text or ""):
        num_s = m.group(1) or m.group(3) or m.group(5) or ""
        unit = (m.group(2) or m.group(4) or m.group(6) or "").lower()
        unit = re.sub(r"\s+", " ", unit)
        try:
            num = float(num_s.replace(",", ""))
        except ValueError:
            continue
        if m.group(3):                      # dollars: printed, not converted
            cr = None
        elif unit:
            cr = num * _TO_CRORE.get(unit, 0.0) or None
        else:
            cr = None                       # "Rs 500" with no unit is not an exposure
            if num < 1000:
                continue
        printed = re.sub(r"\s+", " ", m.group(0)).strip()
        rank = cr if cr is not None else 0.0
        if rank > best_cr or not best[0]:
            best, best_cr = (printed, round(cr, 2) if cr is not None else None), rank
    return best


def read_evidence(title: str, snippet: str = "") -> dict[str, Any]:
    """What the story asserts. Every field is present; ``unknown`` / empty when the
    text does not say — never a guess dressed as a reading."""
    text = f"{title or ''}. {snippet or ''}"
    forums = [name for name, rx in _FORUM_RES if rx.search(text)]
    printed, crore = _amount(text)
    favourable = bool(_FAV.search(text))
    if favourable or _ADV_OUT.search(text):
        nature = "outcome"
    elif _ACTION.search(text) and (forums or not _ALLEG.search(title or "")):
        # An action word with nobody named to have taken it ("faces orders") is still
        # more likely a report of a claim — unless the headline has no claim words.
        nature = "action"
    elif forums and _FORUM_ACT.search(text):
        nature = "action"
    elif _ALLEG.search(text):
        nature = "allegation"
    elif _ACTION.search(text) or _APPEAL.search(text):
        # An appeal is against something already ordered.
        nature = "action"
    else:
        nature = "report"
    if _APPEAL.search(text):
        stage = "appeal"
    elif _FINAL.search(text) or nature == "outcome":
        stage = "final"
    elif nature in ("allegation", "action"):
        stage = "pending"
    else:
        stage = "unknown"
    return {"nature": nature, "stage": stage, "forums": forums,
            "amount": printed, "amount_cr": crore, "favourable": favourable,
            "basis": "headline"}
