"""OIDC verifier — self-signed RS256 round-trip against a mock JWKS (no live IdP).

This is the unit half of the auth fix: proving the verifier accepts a well-formed
token, normalises the e-mail, and rejects wrong-audience / unknown-key tokens. The
gateway/orchestrator wiring that USES it is covered by their integration paths."""

from __future__ import annotations

import httpx
import jwt
import pytest
from evam_backend_core.oidc import OidcError, OidcVerifier, bearer_token

pytestmark = pytest.mark.asyncio


def _material():
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return key, jwk


def _idp(jwk: dict) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": "https://idp.test/jwks"})
        if req.url.path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_valid_token_and_wrong_audience():
    key, jwk = _material()
    good = jwt.encode(
        {"iss": "https://idp.test", "aud": "prism", "email": "Meera@evamfinance.com",
         "sub": "u1", "roles": ["Deal Analyst"], "exp": 9999999999},
        key, algorithm="RS256", headers={"kid": "k1"})
    async with httpx.AsyncClient(transport=_idp(jwk)) as client:
        v = OidcVerifier("https://idp.test", "prism", client)
        ident = await v.verify(good)
        assert ident.email == "meera@evamfinance.com"     # normalised lower
        assert ident.roles == ["Deal Analyst"]
        bad = jwt.encode({"iss": "https://idp.test", "aud": "other",
                          "email": "x@evamfinance.com", "exp": 9999999999},
                         key, algorithm="RS256", headers={"kid": "k1"})
        with pytest.raises(OidcError):
            await v.verify(bad)


async def test_unknown_signing_key_rejected():
    key, jwk = _material()
    other, _ = _material()
    forged = jwt.encode({"iss": "https://idp.test", "email": "x@evamfinance.com",
                         "exp": 9999999999}, other, algorithm="RS256",
                        headers={"kid": "nope"})
    async with httpx.AsyncClient(transport=_idp(jwk)) as client:
        with pytest.raises(OidcError):
            await OidcVerifier("https://idp.test", None, client).verify(forged)


def test_bearer_token_parsing():
    assert bearer_token("Bearer a.b.c") == "a.b.c"
    assert bearer_token("bearer x") == "x"
    assert bearer_token("Basic x") is None
    assert bearer_token(None) is None
