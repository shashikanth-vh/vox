"""Which names a firm is searched and recognised by in the news.

A full-name search is precise and misses the firm's biggest story: the press writes
"Enerparc insolvency", not "Enerparc Energy insolvency", and "Kinetic Green-Saera",
not "Saera Electric Auto". A short-name search finds those and, done naively, turns
"Future Solar" into every article about the future. So a firm is watched under:

  * the names an analyst set on the company (``news_aliases``) — when there are any,
    they are the WHOLE answer, full name included or not, because a person chose them:
    a firm whose own name is a word ("Land", "Pure") is only watchable under a fuller
    name ("Land Renewables");
  * otherwise its full trading name and ONE automatic short name: the trading name without its generic words
    ("Energy", "Electric Auto", "Group"), accepted only if what is left is a single
    word of five letters or more that is not an English word, not a common Indian
    place, given name or surname, and not a business house's name. "Enerparc",
    "Saera", "Pashupati" pass; "Future", "Capital", "Atlanta", "Rajasthan", "Surya",
    "Datta" and "Adani" do not.

The English word list is SCOWL (see data/SCOWL-COPYRIGHT.txt), words of five letters
or more, so the check is a set lookup and needs no network.
"""

from __future__ import annotations

import gzip
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# Words that describe what a firm does rather than which firm it is. Removed from
# the end (or start) of a trading name to find the name the press would print.
GENERIC = frozenset("""
energy energies electric electrics electricals electrical auto autos automotive
group groups environmental enviro solar power powers india indian systems system
solutions solution technologies technology tech industries industry enterprises
enterprise ventures venture mobility logistics labs lab platform platforms engineers
engineering infra infrastructure projects project renewables renewable green global
international services service holdings holding synergy innovations innovation motors
motor batteries battery agro foods food proteins polymers polyplast plastics chemicals
metals steel textiles exports export traders trading finance financial capital
consultants consultant consulting developers development networks network digital
water waste management manufacturing products product works company co corporation
""".split())

# Not English, and still nobody's brand: states, big cities and the given names and
# words Indian firms are most often named after. A short name equal to one of these
# would match half the subcontinent's news.
_INDIA_COMMON = frozenset("""
rajasthan gujarat maharashtra karnataka kerala punjab haryana telangana andhra odisha
orissa bihar bengal assam jharkhand chhattisgarh uttarakhand himachal tamil nadu delhi
mumbai bombay chennai madras kolkata calcutta bengaluru bangalore hyderabad ahmedabad
surat jaipur lucknow kanpur nagpur indore bhopal coimbatore madurai tuticorin
thoothukudi visakha vizag visakhapatnam noida gurgaon gurugram nashik vadodara rajkot
ludhiana amritsar patna ranchi raipur guwahati mysore mysuru mangalore hubli belgaum
bharat bharath hindustan surya shakti shree shri vikas sagar ganga kaveri lakshmi laxmi
ganesh krishna shiva durga jyoti prakash kiran aditya arjun ashok vijay jaya ratna
swastik tirupati balaji siddhi vishwa vishal mahalaxmi mahalakshmi annapurna sapna
navin naveen kumar singh sharma reddy patel
""".split())

# Surnames: "Datta Power Infra" shortened to "Datta" matched Justice Datta of the
# Supreme Court in the 13 Oct live run. A surname names thousands of people.
_SURNAMES = frozenset("""
datta dutta gupta mehta jain agarwal aggarwal agrawal bansal goyal goel garg mittal
verma varma iyer iyengar nair menon pillai banerjee chatterjee mukherjee ghosh chopra
kapoor malhotra khanna arora bhatia sethi saxena srivastava tiwari mishra pandey dubey
chauhan yadav joshi kulkarni deshpande patil pawar shinde naidu chowdhury choudhary
chaudhary thakur rathore rajput sandhu sidhu grewal desai parikh gandhi trivedi bhatt
pathak shukla dixit tripathi upadhyay sinha prasad ahmed hussain siddiqui qureshi
ansari sheikh fernandes dsouza gomes murthy swamy hegde shetty kamath acharya krishnan
raman subramanian venkatesh srinivasan ganesan natarajan sundaram chandra shekhar
bhattacharya chakraborty sengupta dasgupta mazumdar majumdar banerji agnihotri
""".split())

# Business houses: "Adani Solar" shortened to "Adani" pulled in Adani Group news. A
# group's news can matter to a group company — but that is an analyst's choice, made
# by setting the news names, not an automatic one.
_GROUP_HOUSES = frozenset("""
adani ambani birla mahindra bajaj godrej jindal hinduja essar piramal munjal kirloskar
shapoorji pallonji wadia goenka ruia singhania dalmia torrent vedanta reliance
larsen toubro murugappa hiranandani lodha oberoi raheja exide jakson chint
""".split())

# Reviewed against the register's own short names (117 of 489 firms): words that pass
# every rule above and still name someone else first — a better-known company, a
# person, an event, a deity or a Hindi word. Kept short and specific; an analyst's
# news names settle anything this list misses.
_TAKEN = frozenset("""
orano terex freyr nikol gruner indigrid ipower gastech primex astro fibro meine
praveen suresh srinivas ramesh mahesh rajesh parshuram kushmanda vidyut
""".split())

_DATA = Path(__file__).parent / "data" / "english_words.txt.gz"


@lru_cache(maxsize=1)
def _english() -> frozenset[str]:
    try:
        with gzip.open(_DATA, "rt", encoding="utf-8") as fh:
            return frozenset(line.strip() for line in fh if line.strip())
    except OSError:
        # Without the list every candidate looks distinctive; refuse them all instead,
        # so a missing file costs recall, never precision.
        return frozenset()


def _distinctive(word: str) -> bool:
    w = word.lower()
    words = _english()
    return (bool(words) and len(w) >= 5 and w.isalpha() and w not in words
            and w not in _INDIA_COMMON and w not in _SURNAMES and w not in _GROUP_HOUSES
            and w not in _TAKEN)


def too_common(name: str) -> bool:
    """A name that is one ordinary word — "Land", "Pure", "Vikas" — names everything
    and so finds nothing about the firm. Three register firms are called exactly that;
    searched, they filed 47 of the first 50 adverse stories on cipher-test (land-fraud
    cases, an IAS officer called Vikas). Such a firm is searchable only under the news
    names an analyst sets. The English list holds words of five letters or more, so a
    shorter word counts as common unless it is written as an acronym ("IPEC") or is
    plainly coined — a digit or a non-English letter ("3eco", "Savë")."""
    tokens = re.findall(r"[\w&']+", name or "")
    if len(tokens) != 1:
        return False
    raw = tokens[0]
    w = raw.lower()
    if len(w) < 5:
        coined = raw.isupper() or not (raw.isalpha() and raw.isascii())
        return len(w) < 3 or not coined
    return w in _english() or w in _INDIA_COMMON or w in _SURNAMES or w in _GROUP_HOUSES


# A GROUP is searched whole. "Pashupati Group" shortened to "Pashupati" found a temple in
# Kathmandu and an unrelated listed company (cipher-test, 16 Oct): the word in front of
# "Group" is a family, a deity or a place far more often than a brand.
_KEEP_WHOLE = frozenset({"group", "groups"})


def short_name(trading: str) -> str | None:
    """The one automatic short name for a firm, or None when there is no safe one."""
    tokens = re.findall(r"[A-Za-z0-9&']+", trading or "")
    if any(t.lower() in _KEEP_WHOLE for t in tokens):
        return None
    core = list(tokens)
    while len(core) > 1 and core[-1].lower() in GENERIC:
        core.pop()
    while len(core) > 1 and core[0].lower() in GENERIC:
        core.pop(0)
    if len(core) != 1 or core == tokens:
        return None                      # nothing generic to drop, or still several words
    return core[0] if _distinctive(core[0]) else None


def watch_names(trading: str, aliases: list[Any] | None = None) -> list[str]:
    """Every name the firm is searched and recognised by: the analyst's names when
    there are any, else the full name first and the automatic short name after it."""
    full = (trading or "").strip()
    chosen = [str(a).strip() for a in (aliases or []) if str(a or "").strip()]
    if not full and not chosen:
        return []
    names = chosen or [full, *[s for s in [short_name(full)] if s]]
    out: list[str] = []
    for n in names:
        if n.casefold() not in {o.casefold() for o in out}:
            out.append(n)
    return out
