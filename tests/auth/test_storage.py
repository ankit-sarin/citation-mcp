"""Tests for citation_mcp.auth.storage."""

from __future__ import annotations

import pytest

from citation_mcp.auth.models import (
    REFRESH_REUSE_DETECTED,
    AuthCodeRecord,
    RefreshReuseDetected,
    RefreshTokenRecord,
)
from citation_mcp.auth.storage import OAuthStorage, new_chain_id


@pytest.fixture
async def storage() -> OAuthStorage:
    s = OAuthStorage(":memory:")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Clients (DCR)
# ---------------------------------------------------------------------------


async def test_register_and_get_client_roundtrip(storage: OAuthStorage) -> None:
    metadata = {
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "client_name": "Test",
        "token_endpoint_auth_method": "none",
    }
    client_id, registered = await storage.register_client(metadata)
    assert client_id and len(client_id) >= 16
    assert registered["client_id"] == client_id
    assert "client_id_issued_at" in registered
    assert registered["redirect_uris"] == metadata["redirect_uris"]

    fetched = await storage.get_client(client_id)
    assert fetched is not None
    assert fetched["client_id"] == client_id
    assert fetched["redirect_uris"] == metadata["redirect_uris"]


async def test_get_unknown_client_returns_none(storage: OAuthStorage) -> None:
    assert await storage.get_client("does-not-exist") is None


# ---------------------------------------------------------------------------
# Authorization codes
# ---------------------------------------------------------------------------


async def test_store_then_consume_authorization_code(storage: OAuthStorage) -> None:
    await storage.store_authorization_code(
        "the-code",
        client_id="c1",
        user_email="alice@example.com",
        redirect_uri="https://claude.ai/cb",
        code_challenge="abc",
        scope="read",
        resource="https://citation-mcp.example.com",
        ttl_seconds=600,
    )
    rec = await storage.consume_authorization_code("the-code")
    assert isinstance(rec, AuthCodeRecord)
    assert rec.client_id == "c1"
    assert rec.user_email == "alice@example.com"
    assert rec.redirect_uri == "https://claude.ai/cb"
    assert rec.code_challenge == "abc"
    assert rec.scope == "read"
    assert rec.resource == "https://citation-mcp.example.com"
    assert rec.consumed_at is not None


async def test_second_consume_returns_none(storage: OAuthStorage) -> None:
    await storage.store_authorization_code(
        "code-2",
        client_id="c1",
        user_email="a@b.c",
        redirect_uri="https://x/cb",
        code_challenge="ch",
        scope=None,
        resource=None,
        ttl_seconds=600,
    )
    first = await storage.consume_authorization_code("code-2")
    second = await storage.consume_authorization_code("code-2")
    assert first is not None
    assert second is None


async def test_unknown_code_consume_returns_none(storage: OAuthStorage) -> None:
    assert await storage.consume_authorization_code("never-existed") is None


async def test_expired_code_returns_none(
    storage: OAuthStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Store with very short TTL, then jump time forward.
    import citation_mcp.auth.storage as st

    base = 1_000_000
    monkeypatch.setattr(st, "_now", lambda: base)
    await storage.store_authorization_code(
        "expired-code",
        client_id="c1",
        user_email="a@b.c",
        redirect_uri="https://x/cb",
        code_challenge="ch",
        scope=None,
        resource=None,
        ttl_seconds=10,
    )
    monkeypatch.setattr(st, "_now", lambda: base + 11)
    assert await storage.consume_authorization_code("expired-code") is None


# ---------------------------------------------------------------------------
# Refresh tokens (chain rotation)
# ---------------------------------------------------------------------------


async def test_refresh_rotation_happy_path(storage: OAuthStorage) -> None:
    chain = new_chain_id()
    await storage.store_refresh_token(
        "t1",
        client_id="c1",
        user_email="a@b.c",
        chain_id=chain,
        parent_hash=None,
        scope=None,
        ttl_seconds=3600,
    )
    new_rec = await storage.rotate_refresh_token("t1", "t2", ttl_seconds=3600)
    assert isinstance(new_rec, RefreshTokenRecord)
    assert new_rec.chain_id == chain
    assert new_rec.parent_hash is not None  # parent = hash(t1)

    # The new token resolves; the old is marked rotated.
    old = await storage.get_refresh_token("t1")
    assert old is not None and old.rotated_at is not None
    fresh = await storage.get_refresh_token("t2")
    assert fresh is not None and fresh.rotated_at is None and fresh.revoked_at is None


async def test_refresh_reuse_revokes_chain(storage: OAuthStorage) -> None:
    chain = new_chain_id()
    await storage.store_refresh_token(
        "t1",
        client_id="c1",
        user_email="a@b.c",
        chain_id=chain,
        parent_hash=None,
        scope=None,
        ttl_seconds=3600,
    )
    await storage.rotate_refresh_token("t1", "t2", ttl_seconds=3600)
    # Reusing t1 — already rotated — must revoke entire chain.
    second = await storage.rotate_refresh_token("t1", "t3", ttl_seconds=3600)
    assert second is REFRESH_REUSE_DETECTED or isinstance(second, RefreshReuseDetected)
    t1 = await storage.get_refresh_token("t1")
    t2 = await storage.get_refresh_token("t2")
    assert t1.revoked_at is not None
    assert t2.revoked_at is not None
    # t3 was never inserted because rotation failed.
    assert await storage.get_refresh_token("t3") is None


async def test_chain_revocation_cascades_three_deep(storage: OAuthStorage) -> None:
    chain = new_chain_id()
    await storage.store_refresh_token(
        "t1",
        client_id="c1",
        user_email="a@b.c",
        chain_id=chain,
        parent_hash=None,
        scope=None,
        ttl_seconds=3600,
    )
    r2 = await storage.rotate_refresh_token("t1", "t2", ttl_seconds=3600)
    r3 = await storage.rotate_refresh_token("t2", "t3", ttl_seconds=3600)
    assert isinstance(r2, RefreshTokenRecord) and isinstance(r3, RefreshTokenRecord)

    # Present original (now-rotated) token — chain revocation cascades.
    reuse = await storage.rotate_refresh_token("t1", "tNew", ttl_seconds=3600)
    assert isinstance(reuse, RefreshReuseDetected)
    for tok in ("t1", "t2", "t3"):
        rec = await storage.get_refresh_token(tok)
        assert rec is not None
        assert rec.revoked_at is not None


async def test_revoke_chain_explicit(storage: OAuthStorage) -> None:
    chain = new_chain_id()
    await storage.store_refresh_token(
        "x1",
        client_id="c1",
        user_email="a@b.c",
        chain_id=chain,
        parent_hash=None,
        scope=None,
        ttl_seconds=3600,
    )
    await storage.revoke_chain(chain)
    rec = await storage.get_refresh_token("x1")
    assert rec is not None and rec.revoked_at is not None


async def test_unknown_refresh_returns_reuse_sentinel(storage: OAuthStorage) -> None:
    # Spec: unknown tokens map to invalid_grant. Storage signals via the
    # same RefreshReuseDetected sentinel since there's no chain to act on.
    result = await storage.rotate_refresh_token("nope", "new", ttl_seconds=3600)
    assert isinstance(result, RefreshReuseDetected)
