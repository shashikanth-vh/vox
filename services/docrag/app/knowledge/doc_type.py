"""Cheap keyword-based document-type classification.

Not a model call -- this corpus's document types are distinguishable by a
handful of very characteristic terms, so a small rule table is precise and
free. Falls back to "unknown" rather than guessing.
"""

from __future__ import annotations

_KEYWORDS: dict[str, list[str]] = {
    "bank_statement": ["statement of account", "opening balance", "closing balance", "ifsc"],
    "sanction_letter": ["sanction letter", "sanctioned amount", "loan sanction",
                        "terms and conditions of sanction"],
    "id_card": ["permanent account number", "income tax department",
                "unique identification authority", "aadhaar"],
    "gst_certificate": ["goods and services tax", "gstin", "certificate of registration"],
    "financial_report": ["balance sheet", "profit and loss", "provisional", "sundry debtors"],
    "credit_report": ["cibil", "credit information report", "credit score"],
    "purchase_order": ["purchase order", "po number", "delivery schedule"],
    "term_sheet": ["term sheet", "definitive agreement", "intellectual property"],
    "company_registration": ["certificate of incorporation", "partnership deed", "firm registration"],
}


def classify_doc_type(text_sample: str) -> str:
    lowered = text_sample.lower()
    # Score by match *ratio* against each type's own keyword list, not raw
    # count -- a 1/3 hit on a short, specific list should outrank a 1/4 hit
    # on a longer, more generic one.
    scores = {
        doc_type: sum(1 for kw in keywords if kw in lowered) / len(keywords)
        for doc_type, keywords in _KEYWORDS.items()
    }
    best_type, best_score = max(scores.items(), key=lambda kv: kv[1])
    return best_type if best_score > 0 else "unknown"
