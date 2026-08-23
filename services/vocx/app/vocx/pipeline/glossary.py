"""
pipeline.glossary — the known-names context handed to the structuring model.

Field recordings arrive through STT, and STT reliably mangles exactly the words
this product cares most about: Indian company names, lender names, and finance
jargon. "SBI" comes back as "Isbaya", "Piramal" as "Pyramid", "Suryodaya" as
"Sarvodaya". The transcript is evidence and is never rewritten — but the
STRUCTURING model can be told what names exist in this world, so the structured
report uses the real spellings (at reduced confidence) instead of faithfully
propagating a mis-hearing into every field.

Two sources, merged at structuring time:
  * LENDER_GLOSSARY — the static roster of Indian banks / NBFCs / DFIs an RM
    conversation plausibly mentions. Curated, not exhaustive; additions are
    one-line edits.
  * tenant company names — pulled live from the register (entities + open
    leads) by the caller and passed in, so the glossary always reflects the
    current book without this module knowing how to fetch it.

This block travels as RUNTIME CONTEXT in the user message, beside the
transcript. The canonical prompt (prompts/v1.md) is untouched and
prompt_version stays honest.
"""

from __future__ import annotations

from collections.abc import Iterable

LENDER_GLOSSARY: tuple[str, ...] = (
    "SBI (State Bank of India)", "HDFC Bank", "ICICI Bank", "Axis Bank",
    "Kotak Mahindra Bank", "Bank of Baroda", "Punjab National Bank",
    "Canara Bank", "Union Bank of India", "Bank of India", "IndusInd Bank",
    "IDFC First Bank", "Yes Bank", "Federal Bank", "RBL Bank",
    "Piramal (Piramal Capital & Housing Finance)", "Aditya Birla Finance",
    "Tata Capital", "L&T Finance", "Bajaj Finance", "Shriram Finance",
    "Cholamandalam", "Mahindra Finance", "HDB Financial Services",
    "PFC (Power Finance Corporation)", "REC (Rural Electrification Corporation)",
    "IREDA (Indian Renewable Energy Development Agency)", "NaBFID", "SIDBI",
    "NABARD", "EXIM Bank", "HUDCO", "IIFCL", "NIIF",
)

# How many tenant names the block will carry. The register can hold thousands
# of entities; the model needs the plausible mentions, not the phone book.
MAX_COMPANY_NAMES = 400


def build_known_names_block(company_names: Iterable[str] | None = None) -> str:
    """Render the KNOWN NAMES context block for the structuring user message.

    Always returns a non-empty block: the lender roster and the correction
    rules apply even when no tenant names could be fetched.
    """
    lenders = " · ".join(LENDER_GLOSSARY)
    lines = [
        "KNOWN NAMES (runtime context — not part of the transcript):",
        f"Lenders commonly discussed: {lenders}",
    ]
    seen: set[str] = set()
    companies: list[str] = []
    for n in company_names or ():
        n = str(n or "").strip()
        key = n.lower()
        if not n or key in seen:
            continue
        seen.add(key)
        companies.append(n)
        if len(companies) >= MAX_COMPANY_NAMES:
            break
    if companies:
        lines.append("Companies already known to this firm: " + " · ".join(companies))
    lines.append(
        "Correction rules for speech-to-text errors:\n"
        "- The transcript is machine-transcribed speech. If a transcribed name is "
        "phonetically close to a KNOWN name above (e.g. 'Isbaya'~'SBI', "
        "'Pyramid'~'Piramal', 'Sarvodaya'~'Suryodaya'), use the KNOWN spelling in "
        "every structured field and set that field's confidence to at most "
        "'medium'. Quote the transcript verbatim only inside transcript-evidence "
        "fields — never 'correct' the transcript itself.\n"
        "- Apply the same reading to garbled jargon: 'coal-enders' spoken in a "
        "syndication context means 'co-lenders'.\n"
        "- A bare number attached to a lender quote in a lending discussion "
        "('SBI is at 10.75') is an interest rate in percent, NOT an amount — "
        "treat it as crore/lakh only when the speaker said crore or lakh.\n"
        "- When an asset sale mentions components in any order or run-on form "
        "('bundle, land PPA connectivity'), still extract each component "
        "individually into offer_components.\n"
        "- Resolve RELATIVE dates against the Capture timestamp: 'tomorrow', "
        "'next Monday', 'day after' become concrete ISO dates (confidence "
        "medium). A spoken time ('11am', 'around 4') fills follow_up_time as "
        "24-hour HH:MM.\n"
        "- meeting_date: a post-meeting note is normally recorded the same day "
        "as the meeting — when the transcript does not state the meeting date, "
        "use the Capture timestamp's date with confidence medium, not null."
    )
    return "\n\n".join(lines)
