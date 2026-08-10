"""Triage — how bad is this headline, for *us*, on *this* name.

The radar's judgement, and the one part of PULSE a credit person will argue with, so it
is a set of readable rules rather than a model: whichever list a headline hits can always
be pointed at when someone asks why an alert fired.

FOUR TIERS
  UGLY   hard-adverse — fraud, enforcement, insolvency, the words that end a file
  BAD    the stress ladder and governance churn — SMA, restructuring, covenant breach,
         auditor churn — plus routine litigation
  POLICY regulatory and tariff moves that name NO company at all. A state tariff order
         or an ALMM change re-prices a whole portfolio, and a radar that only matches
         company names never sees it coming.
  GOOD   genuine wins

POLARITY IS NOT A PROPERTY OF THE HEADLINE. "Raises fresh debt" is a win for a name we
are chasing and a warning about a name we have already lent to — the borrower is levering
up somewhere else, ahead of us, and that dilutes our cover. So the same sentence classifies
differently depending on our relationship with the firm: the CONTEXT patterns below flip to
BAD (category ``context-review``) when the caller says this is a live exposure, each with
the sentence explaining why credit tenses up. Nothing is auto-filed as good on a borrower.

TWO GUARDS, because recall without precision is a radar the desk learns to ignore:
  · NEGATION — "cleared of siphoning", "acquitted", "clean chit" must not read as UGLY.
    A negator within 70 characters BEFORE the keyword suppresses it.
  · WORD SENSE — "charge" is a criminal charge, except next to "EV charging station",
    which is half this book. Same for "by default", "doing fine", "strike price".

Ported from the desk's own v19 triage (the credit brief's §2.4/§2.5), which is where the
vocabulary comes from — enforcement euphemisms and the stress ladder are what actually
appears in Indian infrastructure credit reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
# Whole words and whole phrases. Stems are spelled out rather than truncated: a substring
# match here mislabels a company for the whole desk ("fir" inside "firm").
RED = [
    "fraud", "frauds", "fraudulent", "defrauded", "default", "defaults", "defaulter",
    "wilful defaulter", "willful defaulter", "insolvency", "insolvent", "bankruptcy",
    "bankrupt", "arrest", "arrested", "raid", "raids", "raided", "scam", "npa", "npas",
    "money laundering", "cbi", "ed probe", "probe", "fir", "firs", "embezzlement",
    "embezzled", "irregularity", "irregularities", "misappropriation", "shell company",
    "shell entity", "tax evasion", "forgery", "fugitive", "fraud case", "loan fraud",
    # Enforcement euphemisms and the instruments that follow them. A recovery action is
    # reported by its instrument ("provisional attachment", "SARFAESI notice") long
    # before anyone writes the word "default".
    "siphoning", "siphoned", "diversion of funds", "round-tripping", "round tripping",
    "fake itc", "gst evasion", "dggi", "sfio", "eow", "lookout notice", "lookout circular",
    "chargesheet", "charge sheet", "absconding", "hawala", "ponzi", "forensic audit",
    "asset attachment", "provisional attachment", "sarfaesi", "nclt", "ibc", "liquidation",
    "winding up", "pmla", "benami", "circular trading", "cross-default",
    "event of default", "enforcement directorate",
]

# The stress ladder, in the order a loan actually walks down it, plus the governance churn
# that runs alongside it. Kept SEPARATE from the routine watch words because it must
# outrank a positive verb: "Promoter pledge rises to 62 per cent" contains "rises", and a
# rule that lets any cheerful word win files a distress signal as good news.
STRESS = [
    "downgrade", "downgraded", "defaulted payment",
    "sma-0", "sma-1", "sma-2", "special mention account", "restructuring", "restructured",
    "debt recast", "recast", "moratorium", "one-time settlement", "one time settlement",
    "ots", "credit watch", "rating withdrawn", "covenant breach", "dscr breach",
    "dsra breach", "promoter pledge", "share pledge", "pledged shares",
    "invocation of pledge", "discom dues", "receivables from discom", "auditor change",
    "auditor resigns", "auditor resignation", "new auditor appointed", "qualified opinion",
    "restatement", "accounting irregularity", "cfo exit", "cfo resigns", "kmp resignation",
    "gst notice", "it survey", "pcb notice", "almm delisting", "roc charge",
    "second charge", "pari passu", "inter-corporate deposit", "upstreaming",
    "related party", "related-party",
]

# Routine watch words. A clear win alongside one of these is still a win — "wins 300 MW
# order despite delay" is good news — which is exactly why the ladder above is separate.
AMBER = [
    "litigation", "lawsuit", "court", "penalty", "penalties", "fined", "fine",
    "delay", "delays", "delayed", "layoff", "layoffs",
    "strike", "dispute", "disputes", "show cause", "showcause", "investigation",
    "investigated", "shortfall", "recall", "recalled", "resignation", "resigns",
    "resigned", "warning", "summons",
]

# POLICY fires with zero company name — matched by theme, not by firm.
BLUE = [
    "tariff order", "tariff revision", "true-up", "serc", "cerc", "regulator order",
    "regulatory order", "open access", "demand charge", "fixed charge", "sub-metering",
    "cross-subsidy surcharge", "ists waiver", "connectivity regulation", "rpo",
    "renewable purchase obligation", "payment security mechanism", "late payment surcharge",
    "almm", "dcr", "domestic content requirement", "pli scheme", "pli disbursement",
    "anti-dumping duty", "safeguard duty", "net metering", "gross metering",
    "mnre notification", "mop notification", "gazette notification",
    "viability gap funding", "vgf", "must-run status", "curtailment compensation",
    "deviation settlement", "dsm regulation", "green hydrogen incentive",
]

# Genuine wins only. Funding, raises and big orders moved to CONTEXT — on a live borrower
# they are not wins.
GOOD = [
    "wins", "win", "won", "bags", "bagged", "awarded", "awards", "award",
    "ppa signed", "signs ppa", "mou", "commissions", "commissioned", "cod achieved",
    "inaugurates", "inaugurated", "inauguration", "launches", "launched", "expansion",
    "expands", "profit", "profits", "record", "milestone", "partnership", "partners",
    "tie-up", "ipo", "listing", "growth", "surges", "jumps", "rises", "upgrade",
    "upgraded", "credit upgrade", "accretive acquisition", "wins order", "wins contract",
    "bags order",
]

# name · trigger phrases · why credit tenses up when the name is already on our book
CONTEXT: list[tuple[str, list[str], str]] = [
    ("fresh_debt",
     ["raises debt", "fresh debt", "raises funding", "raised funding", "secures loan",
      "secures funding", "new loan", "bridge loan", "top-up loan", "refinance", "rollover",
      "raises capital", "funding round", "debt raise", "funding", "funded", "raises",
      "raised", "investment", "invests"],
     "Borrower levering up elsewhere dilutes our cover — could be desperation funding "
     "or ever-greening."),
    ("settlement",
     ["one-time settlement", "one time settlement", "ots", "settles with lender",
      "settlement with bank", "debt settled"],
     "An OTS means a lender took a haircut — a default event, not a win."),
    ("pledge",
     ["pledges shares", "share pledge", "promoter pledge", "pledged stake"],
     "Promoter share-pledging for cash is a classic early-distress signal."),
    ("big_order",
     ["wins huge order", "bags order worth", "wins order worth", "largest order",
      "record order"],
     "Over-trading: orders beyond funding capacity start working-capital blowups."),
    ("stake_sale",
     ["stake sale", "sells stake", "strategic investor", "promoter stake", "divests stake",
      "equity infusion"],
     "Could be growth — or a distress sale / promoter exit. Context decides."),
    ("auditor_change",
     ["auditor change", "new auditor appointed", "auditor appointed", "changes auditor",
      "auditor resigns"],
     "Auditor churn often precedes a qualified opinion or fraud discovery."),
]

# These must NOT raise an alert.
NEG = [
    "cleared of", "clears", "cleared", "absolves", "absolved", "denies", "denied", "deny",
    "refutes", "refuted", "rejects", "no default", "not defaulted", "acquitted",
    "acquittal", "exonerated", "dismisses", "dismissed", "quashes", "quashed", "clean chit",
]

# keyword -> phrases anywhere in the headline that mean it is the WRONG sense
SENSE: dict[str, list[str]] = {
    "charge": ["charging station", "ev charg", "fast charg", "battery charg",
               "free of charge", "in charge of", "took charge"],
    "charges": ["charging station", "ev charg"],
    "default": ["by default", "default setting", "default option", "default mode"],
    "fine": ["doing fine", "works fine", "fine print"],
    "strike": ["strike price", "strikes a deal"],
    "recast": ["recasts its board", "recasts board", "recasts team"],
}

# The state-scoped themes a policy sweep asks about. Published on /v1/news/config so the
# screen composes the same queries the server would.
POLICY_THEMES = [
    "tariff order", "open access charges", "net metering policy", "ALMM",
    "payment security mechanism DISCOM", "anti-dumping duty solar",
]

# How far back from a keyword a negator still applies. A headline clause is short; beyond
# roughly this much text the "cleared" belongs to a different statement.
NEGATION_WINDOW = 70


def _wre(words: list[str]) -> re.Pattern[str]:
    """Whole-word/phrase matcher, tolerant of how a phrase is punctuated: the press writes
    both "one-time settlement" and "one time settlement", and a rule that only knows one
    of them silently misses half the coverage."""
    parts = [re.escape(w).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+") for w in words]
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.I)


_RED_RE, _STRESS_RE = _wre(RED), _wre(STRESS)
_AMBER_RE, _BLUE_RE = _wre(AMBER), _wre(BLUE)
_GOOD_RE, _NEG_RE = _wre(GOOD), _wre(NEG)
_CTX_RES = [(name, _wre(phrases), why) for name, phrases, why in CONTEXT]


@dataclass(frozen=True)
class Verdict:
    """What the desk sees: the colour, why it is that colour, and — for a context flip —
    the sentence that explains the flip."""

    severity: str          # UGLY | BAD | POLICY | GOOD
    category: str          # adverse | stress | watch | policy | context-review |
                           # positive | neutral
    reason: str = ""       # only for context-review

    def as_dict(self) -> dict[str, str]:
        d = {"severity": self.severity, "category": self.category}
        if self.reason:
            d["reason"] = self.reason
        return d


def _blocked(headline: str) -> set[str]:
    """Keywords whose ordinary sense is not the one in this headline."""
    low = headline.lower()
    return {kw for kw, phrases in SENSE.items() if any(p in low for p in phrases)}


def _hit(headline: str, rx: re.Pattern[str], blocked: set[str]) -> re.Match[str] | None:
    """The first match that is neither the wrong word sense nor negated.

    Checking EVERY match matters: "EV charging firm charged with fraud" has a blocked
    "charge" and a real "fraud", and stopping at the first match would drop the alert."""
    for m in rx.finditer(headline):
        if m.group(0).lower() in blocked:
            continue
        before = headline[max(0, m.start() - NEGATION_WINDOW):m.start()]
        if _NEG_RE.search(before):
            continue
        return m
    return None


def triage(headline: str, live: bool = False) -> Verdict:
    """Judge one headline. ``live`` = we already have money out to this name.

    Precedence: hard-adverse, then the context flip (live names only, so the flag carries
    its reason), then the stress ladder, then a genuine win, then the routine watch words,
    then policy. A firm we are merely chasing reads the context phrases as the good news
    they are — but its stress signals still read as stress."""
    h = str(headline or "")
    if not h.strip():
        return Verdict("GOOD", "neutral")
    blocked = _blocked(h)

    if _hit(h, _RED_RE, blocked) is not None:
        return Verdict("UGLY", "adverse")

    ctx = next((c for c in _CTX_RES if c[1].search(h)), None)
    if ctx is not None and live:
        # Good-looking is not good when it is our borrower doing it.
        return Verdict("BAD", "context-review", ctx[2])

    if _hit(h, _STRESS_RE, blocked) is not None:
        return Verdict("BAD", "stress")

    if _GOOD_RE.search(h):
        return Verdict("GOOD", "positive")
    if _hit(h, _AMBER_RE, blocked) is not None:
        return Verdict("BAD", "watch")
    if _AMBER_RE.search(h):
        return Verdict("GOOD", "neutral")     # matched, but negated or the wrong sense
    if _BLUE_RE.search(h):
        return Verdict("POLICY", "policy")
    if ctx is not None:
        return Verdict("GOOD", "positive")    # a name we are chasing: this is a win
    return Verdict("GOOD", "neutral")


def classify(headline: str, live: bool = False) -> str:
    """Just the colour — the shape the rest of PULSE and its tests already use."""
    return triage(headline, live).severity


def context_reason(headline: str) -> str:
    """Why a context phrase would tense credit up, whatever the current verdict — so the
    screen can explain a flag without re-running the rules."""
    for _name, rx, why in _CTX_RES:
        if rx.search(str(headline or "")):
            return why
    return ""
