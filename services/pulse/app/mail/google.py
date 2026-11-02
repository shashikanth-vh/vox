"""Google: the OAuth code exchange, token refresh, and the Gmail REST calls the reader
makes — read-only, with plain httpx (no Google SDK).

The browser gets the authorization code from Google's popup (GIS code client) and
posts it to PRISM; PRISM exchanges it here for a refresh token with the client id
and secret that live only on the box. Every later read mints a short-lived access
token from that refresh token. A refresh Google refuses (``invalid_grant``: the
person revoked the app, reset their password, or the account was suspended) raises
``TokenDeadError`` so the reader stops that mailbox and asks the person to reconnect.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("pulse.mail")

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GoogleError(RuntimeError):
    """Google answered with an error the reader can report (quota, outage, bad request)."""


class TokenDeadError(GoogleError):
    """The refresh token is no longer accepted: the person must reconnect."""


class NotConfiguredError(GoogleError):
    """No client id / secret on the box: the Connect button cannot work yet."""


class GoogleMail:
    def __init__(self, client_id: str, client_secret: str, *,
                 http: httpx.AsyncClient | None = None, timeout_s: float = 30.0) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._own_http = http is None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    # -- OAuth -------------------------------------------------------------------
    async def exchange_code(self, code: str, redirect_uri: str = "postmessage") -> dict[str, Any]:
        """The one-time exchange after the consent screen. ``postmessage`` is the
        redirect the GIS popup flow uses."""
        if not self.configured:
            raise NotConfiguredError("Google client id / secret are not set on this server "
                                "(PULSE_GOOGLE_CLIENT_ID / PULSE_GOOGLE_CLIENT_SECRET).")
        r = await self._http.post(TOKEN_URL, data={
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        body = _json(r)
        if r.status_code != 200 or "access_token" not in body:
            why = body.get("error_description") or body.get("error") or r.status_code
            raise GoogleError(f"Google refused the code: {why}")
        if not body.get("refresh_token"):
            raise GoogleError("Google returned no refresh token — the consent must ask for "
                              "offline access (prompt=consent, access_type=offline).")
        scope = str(body.get("scope") or "")
        if SCOPE not in scope:
            raise GoogleError("The consent did not grant Gmail read-only access.")
        return body

    async def access_token(self, refresh_token: str) -> str:
        if not self.configured:
            raise NotConfiguredError("Google client id / secret are not set on this server.")
        r = await self._http.post(TOKEN_URL, data={
            "refresh_token": refresh_token, "client_id": self.client_id,
            "client_secret": self.client_secret, "grant_type": "refresh_token"})
        body = _json(r)
        if r.status_code == 200 and body.get("access_token"):
            return str(body["access_token"])
        err = str(body.get("error") or "")
        if r.status_code in (400, 401) and err in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise TokenDeadError(body.get("error_description") or err)
        raise GoogleError(f"token refresh failed: {r.status_code} {err}")

    # -- Gmail ---------------------------------------------------------------------
    async def _get(self, access: str, path: str, **params: Any) -> dict[str, Any]:
        r = await self._http.get(f"{GMAIL}/{path}", params=params,
                                 headers={"Authorization": f"Bearer {access}"})
        if r.status_code == 401:
            raise TokenDeadError("Gmail refused the access token")
        if r.status_code == 404:
            raise GoogleError("not found")
        if r.status_code == 429 or r.status_code >= 500:
            raise GoogleError(f"Gmail {r.status_code}: slow down / outage")
        if r.status_code != 200:
            raise GoogleError(f"Gmail {r.status_code}: {r.text[:200]}")
        return _json(r)

    async def profile(self, access: str) -> dict[str, Any]:
        """{"emailAddress": ..., "historyId": ...} — the mailbox the token really opens."""
        return await self._get(access, "profile")

    async def list_threads(self, access: str, query: str, *, page_token: str | None = None,
                           max_results: int = 100) -> tuple[list[str], str | None]:
        params: dict[str, Any] = {"q": query, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        body = await self._get(access, "threads", **params)
        ids = [str(t["id"]) for t in body.get("threads") or []]
        return ids, body.get("nextPageToken")

    async def history(self, access: str, start_history_id: str, *,
                      page_token: str | None = None) -> tuple[set[str], str | None, str | None]:
        """Thread ids with messages added since ``start_history_id``, the newest history id,
        and the next page. Raises GoogleError("not found") when the id is too old (Gmail
        keeps about a week): the caller then lists by date instead."""
        params: dict[str, Any] = {"startHistoryId": start_history_id, "historyTypes": "messageAdded",
                                  "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        body = await self._get(access, "history", **params)
        threads: set[str] = set()
        for h in body.get("history") or []:
            for added in h.get("messagesAdded") or []:
                m = added.get("message") or {}
                if m.get("threadId"):
                    threads.add(str(m["threadId"]))
        return threads, body.get("historyId"), body.get("nextPageToken")

    async def thread(self, access: str, thread_id: str) -> dict[str, Any]:
        return await self._get(access, f"threads/{thread_id}", format="full")


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
