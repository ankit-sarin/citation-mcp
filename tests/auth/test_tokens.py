"""Tests for citation_mcp.auth.tokens."""

from __future__ import annotations

import base64
import hashlib
import time

import jwt
import pytest

from citation_mcp.auth import tokens

KEY = "x" * 64
ISSUER = "https://citation-mcp.example.com"
AUDIENCE = "https://citation-mcp.example.com"


def test_jwt_sign_verify_roundtrip() -> None:
    tok = tokens.issue_access_token(
        sub="user@example.com",
        client_id="client-1",
        scope="read",
        signing_key=KEY,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    claims = tokens.verify_access_token(
        tok, signing_key=KEY, issuer=ISSUER, audience=AUDIENCE
    )
    assert claims["iss"] == ISSUER
    assert claims["aud"] == AUDIENCE
    assert claims["sub"] == "user@example.com"
    assert claims["client_id"] == "client-1"
    assert claims["scope"] == "read"
    assert "jti" in claims and claims["jti"]
    assert claims["exp"] > claims["iat"]


def test_jwt_rejects_expired_token() -> None:
    tok = tokens.issue_access_token(
        sub="u", client_id="c", scope="",
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE,
        ttl_seconds=-1,  # already expired
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        tokens.verify_access_token(
            tok, signing_key=KEY, issuer=ISSUER, audience=AUDIENCE
        )


def test_jwt_rejects_wrong_issuer() -> None:
    tok = tokens.issue_access_token(
        sub="u", client_id="c", scope="",
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE,
    )
    with pytest.raises(jwt.InvalidIssuerError):
        tokens.verify_access_token(
            tok, signing_key=KEY, issuer="https://other.example", audience=AUDIENCE
        )


def test_jwt_rejects_wrong_audience() -> None:
    tok = tokens.issue_access_token(
        sub="u", client_id="c", scope="",
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE,
    )
    with pytest.raises(jwt.InvalidAudienceError):
        tokens.verify_access_token(
            tok, signing_key=KEY, issuer=ISSUER, audience="https://other.example"
        )


def test_jwt_rejects_wrong_key() -> None:
    tok = tokens.issue_access_token(
        sub="u", client_id="c", scope="",
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE,
    )
    with pytest.raises(jwt.InvalidSignatureError):
        tokens.verify_access_token(
            tok, signing_key="y" * 64, issuer=ISSUER, audience=AUDIENCE
        )


def test_jwt_rejects_missing_required_claim() -> None:
    # Hand-build a token without an `aud` claim.
    payload = {
        "iss": ISSUER,
        "sub": "u",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "jti": "x",
    }
    tok = jwt.encode(payload, KEY, algorithm="HS256")
    with pytest.raises(jwt.MissingRequiredClaimError):
        tokens.verify_access_token(
            tok, signing_key=KEY, issuer=ISSUER, audience=AUDIENCE
        )


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def test_pkce_s256_matches_known_good() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = _challenge_for(verifier)
    assert tokens.verify_pkce_s256(verifier, challenge) is True


def test_pkce_s256_rejects_tampered_verifier() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = _challenge_for(verifier)
    assert tokens.verify_pkce_s256(verifier + "x", challenge) is False


# ---------------------------------------------------------------------------
# Opaque tokens
# ---------------------------------------------------------------------------


def test_opaque_tokens_are_url_safe_and_long() -> None:
    code = tokens.new_authorization_code()
    refresh = tokens.new_refresh_token()
    assert isinstance(code, str) and len(code) >= 40
    assert isinstance(refresh, str) and len(refresh) >= 40
    # URL-safe alphabet: letters, digits, '-', '_'
    import re
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", code)
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", refresh)


def test_hash_token_is_deterministic_and_hex64() -> None:
    h1 = tokens.hash_token("hello")
    h2 = tokens.hash_token("hello")
    assert h1 == h2
    assert len(h1) == 64
    import re
    assert re.fullmatch(r"[0-9a-f]+", h1)
