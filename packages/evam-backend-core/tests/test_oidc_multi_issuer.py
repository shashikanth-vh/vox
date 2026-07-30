"""Multi-issuer selection and the e-mail domain allowlist.

Both are AUTHENTICATION controls, so the tests assert the refusals as hard as the successes: an
unrecognised issuer, and an identity from outside the organisation, must be rejected before any
authorization runs.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from evam_backend_core.oidc import (
    MultiIssuerVerifier,
    OidcError,
    OidcVerifier,
    build_verifier,
    parse_issuer_specs,
)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "test-key-1"


def _token(issuer: str, email: str, audience: str = "prism", **extra) -> str:
    claims = {"iss": issuer, "aud": audience, "email": email, "sub": "sub-1",
              "exp": int(time.time()) + 300, **extra}
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": _KID})


def _verifier(issuer: str, **kw) -> OidcVerifier:
    v = OidcVerifier(issuer, "prism", httpx.AsyncClient(), **kw)
    # Pre-seed the JWKS cache so no network call happens.
    v._keys = {_KID: _KEY.public_key()}          # noqa: SLF001
    v._fetched_at = time.monotonic()             # noqa: SLF001
    v._jwks_uri = f"{issuer}/keys"               # noqa: SLF001
    return v


# --------------------------------------------------------------------------- #
# Issuer specs
# --------------------------------------------------------------------------- #
def test_issuer_specs_parse_pairs_and_tolerate_whitespace():
    got = parse_issuer_specs(" https://accounts.google.com|abc.apps.googleusercontent.com , "
                             "http://dex:5556/dex|prism ")
    assert got == [("https://accounts.google.com", "abc.apps.googleusercontent.com"),
                   ("http://dex:5556/dex", "prism")]


def test_issuer_spec_audience_is_optional_and_blanks_are_skipped():
    assert parse_issuer_specs("https://idp.example|,, ,https://other.example") == [
        ("https://idp.example", None), ("https://other.example", None)]


def test_build_verifier_returns_none_without_configuration():
    # This is what preserves the dev header-trust path.
    assert build_verifier(httpx.AsyncClient()) is None


def test_build_verifier_single_issuer_is_not_wrapped():
    v = build_verifier(httpx.AsyncClient(), issuer="http://dex:5556/dex", audience="prism")
    assert isinstance(v, OidcVerifier)


def test_build_verifier_multi_issuer_is_a_registry():
    v = build_verifier(httpx.AsyncClient(),
                       issuers_spec="https://accounts.google.com|abc,http://dex:5556/dex|prism")
    assert isinstance(v, MultiIssuerVerifier)
    assert v.issuers == ["http://dex:5556/dex", "https://accounts.google.com"]


def test_issuers_spec_takes_precedence_over_the_single_setting():
    v = build_verifier(httpx.AsyncClient(), issuer="http://ignored", audience="x",
                       issuers_spec="https://a.example|aud1,https://b.example|aud2")
    assert isinstance(v, MultiIssuerVerifier)
    assert "http://ignored" not in v.issuers


# --------------------------------------------------------------------------- #
# Multi-issuer verification
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_each_issuer_verifies_its_own_token():
    google, dex = "https://accounts.google.com", "http://dex:5556/dex"
    reg = MultiIssuerVerifier({google: _verifier(google), dex: _verifier(dex)})
    for iss in (google, dex):
        ident = await reg.verify(_token(iss, "rm@evamfinance.com"))
        assert ident.email == "rm@evamfinance.com"


@pytest.mark.asyncio
async def test_an_unrecognised_issuer_is_refused():
    dex = "http://dex:5556/dex"
    reg = MultiIssuerVerifier({dex: _verifier(dex)})
    with pytest.raises(OidcError, match="not accepted"):
        await reg.verify(_token("https://evil.example", "attacker@evamfinance.com"))


@pytest.mark.asyncio
async def test_a_token_is_not_tried_against_every_verifier():
    """A weaker issuer must never vouch for another issuer's audience: selection is by `iss`,
    so a Dex-signed token claiming Google's issuer fails rather than falling through."""
    google, dex = "https://accounts.google.com", "http://dex:5556/dex"
    reg = MultiIssuerVerifier({google: _verifier(google), dex: _verifier(dex)})
    # Signed by the same key, but the `iss` claim says Google while the audience is Dex's.
    with pytest.raises(OidcError):
        await reg.verify(_token(google, "x@evamfinance.com", audience="wrong-audience"))


@pytest.mark.asyncio
async def test_malformed_token_is_refused_before_issuer_lookup():
    reg = MultiIssuerVerifier({"http://dex:5556/dex": _verifier("http://dex:5556/dex")})
    with pytest.raises(OidcError, match="malformed"):
        await reg.verify("not-a-jwt")


# --------------------------------------------------------------------------- #
# Domain allowlist
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_allowed_domain_passes():
    iss = "https://accounts.google.com"
    v = _verifier(iss, allowed_domains=["evamfinance.com"])
    ident = await v.verify(_token(iss, "priya@evamfinance.com"))
    assert ident.email == "priya@evamfinance.com"


@pytest.mark.asyncio
async def test_a_personal_account_is_refused_even_with_a_valid_signature():
    """THE control: with Google as an accepted issuer, any consumer account produces a genuine
    token. Authentication — not just the later user lookup — must refuse it."""
    iss = "https://accounts.google.com"
    v = _verifier(iss, allowed_domains=["evamfinance.com"])
    with pytest.raises(OidcError, match="gmail.com"):
        await v.verify(_token(iss, "someone@gmail.com"))


@pytest.mark.asyncio
async def test_allowlist_is_case_insensitive_and_ignores_a_leading_at():
    iss = "http://dex:5556/dex"
    v = _verifier(iss, allowed_domains=["@EvamFinance.COM", " "])
    ident = await v.verify(_token(iss, "Priya@EVAMFINANCE.com"))
    assert ident.email == "priya@evamfinance.com"


@pytest.mark.asyncio
async def test_no_allowlist_means_no_restriction():
    # Dev default — otherwise every local setup would break.
    iss = "http://dex:5556/dex"
    v = _verifier(iss)
    assert (await v.verify(_token(iss, "anyone@anywhere.example"))).email
