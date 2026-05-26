"""Tests for citation_mcp.auth.verifier.CitationMcpTokenVerifier."""

from __future__ import annotations

import logging

import jwt

from citation_mcp.auth import tokens as tk
from citation_mcp.auth.verifier import CitationMcpTokenVerifier

KEY = "x" * 64
ISSUER = "https://citation-mcp.example.com"
AUDIENCE = "https://citation-mcp.example.com"


def _verifier() -> CitationMcpTokenVerifier:
    return CitationMcpTokenVerifier(
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE
    )


async def test_accepts_valid_token() -> None:
    tok = tk.issue_access_token(
        sub="u@e.x",
        client_id="c1",
        scope="read write",
        signing_key=KEY,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    access = await _verifier().verify_token(tok)
    assert access is not None
    assert access.client_id == "c1"
    assert access.scopes == ["read", "write"]
    assert access.expires_at is not None


async def test_rejects_expired_token() -> None:
    tok = tk.issue_access_token(
        sub="u", client_id="c", scope="",
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE, ttl_seconds=-1,
    )
    assert await _verifier().verify_token(tok) is None


async def test_rejects_wrong_issuer() -> None:
    tok = jwt.encode(
        {
            "iss": "https://other.example",
            "sub": "u",
            "aud": AUDIENCE,
            "iat": 1,
            "exp": 9999999999,
            "client_id": "c",
            "scope": "",
        },
        KEY,
        algorithm="HS256",
    )
    assert await _verifier().verify_token(tok) is None


async def test_rejects_wrong_audience() -> None:
    tok = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "u",
            "aud": "https://other.example",
            "iat": 1,
            "exp": 9999999999,
            "client_id": "c",
            "scope": "",
        },
        KEY,
        algorithm="HS256",
    )
    assert await _verifier().verify_token(tok) is None


async def test_warning_log_on_bad_audience_does_not_leak_token(caplog) -> None:
    bad_token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "u",
            "aud": "https://wrong.example.com/",
            "iat": 1,
            "exp": 9999999999,
            "client_id": "c",
            "scope": "",
        },
        KEY,
        algorithm="HS256",
    )
    caplog.set_level(logging.WARNING, logger="citation_mcp.auth.verifier")
    result = await _verifier().verify_token(bad_token)
    assert result is None
    assert "InvalidAudienceError" in caplog.text
    assert bad_token not in caplog.text


async def test_rejects_tampered_signature() -> None:
    tok = tk.issue_access_token(
        sub="u", client_id="c", scope="",
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE,
    )
    # Replace the signature segment with one signed by a different key.
    bad_sig = jwt.encode({"x": 1}, "y" * 64, algorithm="HS256").split(".")[-1]
    parts = tok.split(".")
    parts[-1] = bad_sig
    tampered = ".".join(parts)
    assert await _verifier().verify_token(tampered) is None
