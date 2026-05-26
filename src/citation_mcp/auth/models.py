"""Pydantic-free dataclass records and sentinels for the OAuth storage layer.

These are the records returned by the storage API. They are deliberately not
pydantic models — storage is an internal interface and dataclasses keep the
test surface narrow.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthCodeRecord:
    code_hash: str
    client_id: str
    user_email: str
    redirect_uri: str
    code_challenge: str
    scope: str | None
    resource: str | None
    expires_at: int
    consumed_at: int | None


@dataclass(frozen=True)
class RefreshTokenRecord:
    token_hash: str
    client_id: str
    user_email: str
    chain_id: str
    parent_hash: str | None
    scope: str | None
    issued_at: int
    expires_at: int
    rotated_at: int | None
    revoked_at: int | None


class RefreshReuseDetected:
    """Sentinel returned by rotate_refresh_token() when reuse is detected.

    Per OAuth 2.1, presenting a previously-rotated refresh token implies the
    chain is compromised; the entire chain must be revoked atomically. The
    storage layer signals this with this sentinel rather than an exception so
    the route handler can choose its own error mapping (RFC 6749 invalid_grant).
    """

    __slots__ = ()


REFRESH_REUSE_DETECTED = RefreshReuseDetected()
