"""The model call: one conversation in, one structured note out.

Engines, chosen by ``PULSE_MAIL_ENGINE``:
  anthropic:<model>   Claude via the Messages API (ANTHROPIC key)
  sarvam:<model>      Sarvam's OpenAI-compatible chat endpoint (SARVAM key)
  stub                no model: the subject and the latest message become the note
  auto                anthropic if its key is set, else sarvam if its key is set, else stub

The note's shape is fixed (``NOTE_KEYS``) so the filing step and the UI never
depend on which engine wrote it. A reply that is not JSON, or JSON of the wrong
shape, raises ``ExtractError``; the reader keeps the conversation for the next run.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.mail.threads import Thread

log = logging.getLogger("pulse.mail")

NOTE_KEYS = ("summary", "discussed", "decisions", "asks", "amounts", "next_step",
             "next_step_date", "signal", "signal_reason", "counterparty_name", "evidence_quote")
SIGNALS = ("RED", "AMBER", "GREEN", "NONE")

PROMPT = """You read one e-mail conversation between a climate-finance advisory desk (Evam Finance) \
and an outside party, and write the desk's record of it. Answer with ONE JSON object and nothing \
else, with exactly these keys:
- "summary": one or two sentences (max 280 characters) a colleague reads to know what this \
conversation is about and where it stands.
- "discussed": list of short phrases, the topics actually discussed.
- "decisions": list of short phrases, decisions or agreements reached (empty if none).
- "asks": list of short phrases, what each side asked the other for.
- "amounts": list of short phrases quoting any amounts, rates, tenors or terms with their context.
- "next_step": the next concrete action and who owns it, or null.
- "next_step_date": that action's date as YYYY-MM-DD if the mail names or implies one, else null.
- "signal": "RED" if the conversation carries a credit or reputational risk (default, dispute, \
regulator, fraud, delay in payments, withdrawal), "AMBER" if a concern worth watching, "GREEN" if \
clearly positive news for the relationship (mandate won, sanction received, funds raised), else "NONE".
- "signal_reason": one sentence on why, or null.
- "counterparty_name": the outside person's name as written, or null.
- "evidence_quote": one short verbatim quote (max 200 characters) from the mail that best \
supports the summary.
Quote facts from the mail; do not invent amounts or dates. Do not describe the e-mail \
("the sender writes"); state what was discussed."""


class ExtractError(RuntimeError):
    pass


def resolve_engine(engine: str, anthropic_key: str, sarvam_key: str) -> str:
    e = (engine or "auto").strip()
    if e != "auto":
        return e
    if anthropic_key.strip():
        return "anthropic:claude-sonnet-5"
    if sarvam_key.strip():
        return "sarvam:sarvam-105b-conversations"
    return "stub"


def _conversation(t: Thread, company: str | None) -> str:
    head = f"Subject: {t.subject}\n"
    if company:
        head += f"The desk believes this conversation concerns: {company}\n"
    return head + "\n" + t.text()


def _parse(raw: str) -> dict[str, Any]:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ExtractError("the model did not answer with JSON")
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise ExtractError(f"the model's JSON did not parse: {exc}") from exc
    if not isinstance(obj, dict):
        raise ExtractError("the model's answer is not an object")
    return normalise_note(obj)


def normalise_note(obj: dict[str, Any]) -> dict[str, Any]:
    note: dict[str, Any] = {}
    note["summary"] = str(obj.get("summary") or "").strip()[:280]
    for k in ("discussed", "decisions", "asks", "amounts"):
        v = obj.get(k) or []
        if isinstance(v, str):
            v = [v]
        note[k] = [str(x).strip() for x in v if str(x).strip()][:12]
    ns = obj.get("next_step")
    note["next_step"] = str(ns).strip()[:300] if ns else None
    d = obj.get("next_step_date")
    note["next_step_date"] = d if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None
    sig = str(obj.get("signal") or "NONE").strip().upper()
    note["signal"] = sig if sig in SIGNALS else "NONE"
    sr = obj.get("signal_reason")
    note["signal_reason"] = str(sr).strip()[:300] if sr else None
    cn = obj.get("counterparty_name")
    note["counterparty_name"] = str(cn).strip()[:200] if cn else None
    q = obj.get("evidence_quote")
    note["evidence_quote"] = str(q).strip()[:200] if q else None
    if not note["summary"]:
        raise ExtractError("the model's note has no summary")
    return note


def stub_note(t: Thread) -> dict[str, Any]:
    """No model: an honest, plain note so the pipeline works offline (tests, a box
    without keys). Marked as such in the summary."""
    last = t.last
    excerpt = re.sub(r"\s+", " ", last.text)[:180]
    return normalise_note({
        "summary": f"{t.subject}: {excerpt}" if excerpt else t.subject,
        "discussed": [t.subject], "decisions": [], "asks": [], "amounts": [],
        "next_step": None, "next_step_date": None, "signal": "NONE", "signal_reason": None,
        "counterparty_name": None,   # the reader names the outside party itself
        "evidence_quote": excerpt[:200] or None,
    })


class Extractor:
    def __init__(self, engine: str, *, anthropic_key: str = "", sarvam_key: str = "",
                 sarvam_base_url: str = "https://api.sarvam.ai/v1",
                 http: httpx.AsyncClient | None = None, timeout_s: float = 90.0) -> None:
        self.engine = resolve_engine(engine, anthropic_key, sarvam_key)
        self.anthropic_key = anthropic_key.strip()
        self.sarvam_key = sarvam_key.strip()
        self.sarvam_base_url = sarvam_base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._own_http = http is None
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    @property
    def provider(self) -> str:
        return self.engine.split(":", 1)[0]

    @property
    def model(self) -> str:
        return self.engine.split(":", 1)[1] if ":" in self.engine else ""

    async def note(self, t: Thread, company: str | None = None) -> dict[str, Any]:
        if self.provider == "stub":
            return stub_note(t)
        convo = _conversation(t, company)
        self.calls += 1
        if self.provider == "anthropic":
            return await self._anthropic(convo)
        if self.provider == "sarvam":
            return await self._sarvam(convo)
        raise ExtractError(f"unknown engine '{self.engine}'")

    async def _anthropic(self, convo: str) -> dict[str, Any]:
        if not self.anthropic_key:
            raise ExtractError("ANTHROPIC key is not set for the mail engine")
        r = await self._http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.anthropic_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            # No temperature: the current Claude models refuse the parameter outright
            # ("`temperature` is deprecated for this model", HTTP 400, staging 3 Nov).
            json={"model": self.model, "max_tokens": 1200,
                  "system": PROMPT, "messages": [{"role": "user", "content": convo}]})
        body = _json(r)
        if r.status_code != 200:
            err = (body.get("error") or {}).get("message") or r.text[:200]
            raise ExtractError(f"Claude refused the request (HTTP {r.status_code}): {err}")
        usage = body.get("usage") or {}
        self.tokens_in += int(usage.get("input_tokens") or 0)
        self.tokens_out += int(usage.get("output_tokens") or 0)
        text = "".join(str(c.get("text") or "") for c in body.get("content") or []
                       if c.get("type") == "text")
        return _parse(text)

    async def _sarvam(self, convo: str) -> dict[str, Any]:
        if not self.sarvam_key:
            raise ExtractError("SARVAM key is not set for the mail engine")
        r = await self._http.post(
            f"{self.sarvam_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.sarvam_key}",
                     "api-subscription-key": self.sarvam_key, "content-type": "application/json"},
            json={"model": self.model, "temperature": 0, "max_tokens": 1200,
                  "messages": [{"role": "system", "content": PROMPT},
                               {"role": "user", "content": convo}]})
        body = _json(r)
        if r.status_code != 200:
            err = (body.get("error") or {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ExtractError(f"Sarvam refused the request (HTTP {r.status_code}): {msg or r.text[:200]}")
        usage = body.get("usage") or {}
        self.tokens_in += int(usage.get("prompt_tokens") or 0)
        self.tokens_out += int(usage.get("completion_tokens") or 0)
        choices = body.get("choices") or []
        text = str((choices[0].get("message") or {}).get("content") or "") if choices else ""
        return _parse(text)


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
