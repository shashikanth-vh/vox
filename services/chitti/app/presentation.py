"""Business-facing chat responses: no execution traces or raw database tables."""

from __future__ import annotations

import re
from dataclasses import replace

_UNSAFE = re.compile(
    r"\b[a-zA-Z][a-zA-Z0-9]*_[a-zA-Z0-9_]+\b"
    r"|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
    r'|```|(?m:^\s*\|)|(?m:^\s*\{\s*")|\bSELECT\s+.+\s+FROM\b'
    r'|\b(?:caller-visible|aggregate scalar|result row count|evidence obligations?'
    r'|unavailable obligations?|obligation to confirm|execution plan|retrieval trace)\b',
    re.IGNORECASE,
)
_RETRY = "I couldn't prepare a clear answer to that question. Please try rephrasing it."


def business_text(text: str) -> str:
    # Fail closed on schema-style names, internal IDs, code, and tables. This is a
    # presentation check in addition to the answer prompt, not an authorization rule.
    return _RETRY if _UNSAFE.search(text) else text


def public_result(result):
    metadata = result.metadata
    outcome = metadata.get("outcome")
    if outcome in {"ANSWERED", "PARTIAL_RESULT"}:
        content = result.public_content or _RETRY
    elif outcome == "ACCESS_DENIED":
        content = "You don't have access to the information needed to answer this question."
    elif outcome in {"CLARIFICATION_REQUIRED", "OUT_OF_SCOPE"}:
        content = result.content
    else:
        content = "I couldn't complete this answer. Please try again shortly."
    evidence = [{"reference": item["reference"], "label": business_text(item["label"])}
                for item in metadata.get("public_evidence", [])
                if isinstance(item, dict) and re.fullmatch(r"E[0-9]+", str(item.get("reference", "")))
                and isinstance(item.get("label"), str)]
    content = business_text(content)
    if content == _RETRY:
        outcome = "FAILED"
        evidence = []
    return replace(result, content=content, public_content=content, metadata={
        "request_id": metadata.get("request_id"),
        "outcome": outcome,
        "completeness": "FAILED" if content == _RETRY else metadata.get("completeness"),
        "evidence": evidence,
        **({"tables": result.public_tables} if result.public_tables
           and outcome in {"ANSWERED", "PARTIAL_RESULT"} else {}),
    })


def public_status(event: dict) -> dict:
    return {"type": "status", "data": {"description": event.get("description", "")}}
