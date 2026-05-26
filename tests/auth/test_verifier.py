"""Tests for citation_mcp.auth.verifier.CitationMcpTokenVerifier."""

from __future__ import annotations

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


async def test_rejects_tampered_signature() -> None:
    tok = tk.issue_access_token(
        sub="u", client_id="c", scope="",
        signing_key=KEY, issuer=ISSUER, audience=AUDIENCE,
    )
    # Flip the last character of the signature segment.
    parts = tok.split(".")
    parts[-1] = parts[-1][:-1] + ("A" if parts[-1][-1] != "A" else "B")
    tampered = ".".join(parts)
    assert await _verifier().verify_token(tampered) is None
