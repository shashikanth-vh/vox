"""
google.oauth — per-user Google OAuth and refresh-token storage.

Each mobile user signs in with their OWN Google account (Drive + Calendar
scopes); writes land in the speaking RM's own Drive + Calendar. Per-user refresh
tokens are stored server-side, keyed to the ATLAS user id (the RM short name that
appears in deals.rm / leads.rm).

Flow (server-side, offline access so we get a refresh token):
  1. build_flow(config) -> google_auth_oauthlib Flow from client_secret.json
  2. authorization_url(flow, rm) -> send the RM to Google; state carries the rm
  3. exchange_code(flow, code) -> Credentials; TokenStore.save(rm, creds)
  4. TokenStore.credentials(rm) -> refreshed Credentials for the writers

Tokens are files under tokens_dir (git-ignored). google_auth libraries are
imported lazily so the token-store JSON format can be used/tested without them.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Two well-known localhost-OAuth gotchas, set before oauthlib runs:
#  * OAUTHLIB_INSECURE_TRANSPORT: oauthlib refuses a plain-http redirect by
#    default; the loopback redirect (http://localhost:8765/...) is http, so allow
#    it. (Only affects the local token exchange; production uses https.)
#  * OAUTHLIB_RELAX_TOKEN_SCOPE: Google often returns scopes in a different
#    order / adds openid, which otherwise raises "Scope has changed".
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",  # follow-up events (default)
    "openid", "https://www.googleapis.com/auth/userinfo.email",
    # add "https://www.googleapis.com/auth/drive.file" only if drive_enabled
]


class TokenStore:
    """Per-RM refresh-token storage (one JSON file per RM)."""

    def __init__(self, tokens_dir: str = "vocx_tokens"):
        self.tokens_dir = tokens_dir
        os.makedirs(self.tokens_dir, exist_ok=True)

    def _path(self, rm: str) -> str:
        safe = "".join(c for c in (rm or "") if c.isalnum() or c in ("-", "_")).lower()
        return os.path.join(self.tokens_dir, "{}.token.json".format(safe or "unknown"))

    def has(self, rm: str) -> bool:
        return os.path.exists(self._path(rm))

    def save_dict(self, rm: str, token: dict[str, Any]) -> None:
        with open(self._path(rm), "w", encoding="utf-8") as fh:
            json.dump(token, fh)

    def load_dict(self, rm: str) -> dict[str, Any] | None:
        p = self._path(rm)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def save_credentials(self, rm: str, creds: Any) -> None:
        self.save_dict(rm, _creds_to_dict(creds))

    def credentials(self, rm: str) -> Any:
        """Return refreshed google Credentials for this RM, or raise if none."""
        data = self.load_dict(rm)
        if not data:
            raise KeyError(f"No Google token stored for RM {rm!r}. Run the OAuth flow.")
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes", DEFAULT_SCOPES),
        )
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            self.save_credentials(rm, creds)     # persist rotated access token
        return creds


def _creds_to_dict(creds: Any) -> dict[str, Any]:
    return {
        "token": getattr(creds, "token", None),
        "refresh_token": getattr(creds, "refresh_token", None),
        "token_uri": getattr(creds, "token_uri", "https://oauth2.googleapis.com/token"),
        "client_id": getattr(creds, "client_id", None),
        "client_secret": getattr(creds, "client_secret", None),
        "scopes": list(getattr(creds, "scopes", []) or DEFAULT_SCOPES),
    }


# --- OAuth flow (lazy google_auth_oauthlib) ----------------------------------
def build_flow(config: dict[str, Any], redirect_uri: str | None = None) -> Any:
    from google_auth_oauthlib.flow import Flow
    g = config.get("google", {})
    scopes = g.get("scopes", DEFAULT_SCOPES)
    secret_file = g.get("client_secret_file", "client_secret.json")
    # Resolve relative to this module's folder if it isn't found in the CWD, so
    # the flow works no matter where the server is launched from.
    if not os.path.isabs(secret_file) and not os.path.exists(secret_file):
        cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), secret_file)
        if os.path.exists(cand):
            secret_file = cand
    redirect_uri = redirect_uri or g.get("redirect_uri")
    flow = Flow.from_client_secrets_file(secret_file, scopes=scopes)
    if redirect_uri:
        flow.redirect_uri = redirect_uri
    return flow


def authorization_url(flow: Any, rm: str) -> str:
    """URL to send the RM to. Offline + consent so we always get a refresh token;
    `state` carries the RM id so the callback knows whose tokens to store."""
    url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true",
        prompt="consent", state=rm)
    return url


def exchange_code(flow: Any, code: str) -> Any:
    flow.fetch_token(code=code)
    return flow.credentials
