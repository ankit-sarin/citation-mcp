"""aiosqlite-backed OAuth 2.1 persistence: clients, auth codes, refresh chains.

Schema is created idempotently on first init(). Tokens are stored as SHA-256
hex digests of the plaintext — raw values never touch disk. The plaintext
token leaves the server exactly once at issuance.

Concurrency: a single shared aiosqlite connection guarded by an internal
asyncio.Lock around mutating sequences (consume + rotate). This matches the
existing Cache pattern and is sufficient for the single-process deployment
shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path

import aiosqlite

from .models import (
    AuthCodeRecord,
    REFRESH_REUSE_DETECTED,
    RefreshReuseDetected,
    RefreshTokenRecord,
)


def _default_db_path() -> Path:
    env = os.environ.get("OAUTH_DB_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / "projects" / "citation-mcp" / "data" / "oauth.db"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


class OAuthStorage:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            self._db_path = _default_db_path()
        else:
            self._db_path = (
                db_path if isinstance(db_path, Path) else Path(db_path).expanduser()
            )
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                client_metadata_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS authorization_codes (
                code_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                user_email TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                scope TEXT,
                resource TEXT,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                user_email TEXT NOT NULL,
                chain_id TEXT NOT NULL,
                parent_hash TEXT,
                scope TEXT,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                rotated_at INTEGER,
                revoked_at INTEGER
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_refresh_chain ON refresh_tokens(chain_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_refresh_client ON refresh_tokens(client_id)"
        )
        await self._conn.commit()

    def _ensure(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("OAuthStorage.init() must be called before use.")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Clients (DCR)
    # ------------------------------------------------------------------

    async def register_client(self, metadata: dict) -> tuple[str, dict]:
        """Persist a DCR client. Returns (client_id, registered_metadata).

        client_id is generated server-side via secrets.token_urlsafe(16). The
        returned metadata echoes the input plus the server-added client_id and
        client_id_issued_at fields.
        """
        conn = self._ensure()
        client_id = secrets.token_urlsafe(16)
        created_at = _now()
        registered = dict(metadata)
        registered["client_id"] = client_id
        registered["client_id_issued_at"] = created_at
        await conn.execute(
            "INSERT INTO clients (client_id, client_metadata_json, created_at) VALUES (?, ?, ?)",
            (client_id, json.dumps(registered), created_at),
        )
        await conn.commit()
        return client_id, registered

    async def get_client(self, client_id: str) -> dict | None:
        conn = self._ensure()
        async with conn.execute(
            "SELECT client_metadata_json FROM clients WHERE client_id = ?",
            (client_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    # ------------------------------------------------------------------
    # Authorization codes
    # ------------------------------------------------------------------

    async def store_authorization_code(
        self,
        code: str,
        *,
        client_id: str,
        user_email: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str | None,
        resource: str | None,
        ttl_seconds: int,
    ) -> None:
        conn = self._ensure()
        expires_at = _now() + int(ttl_seconds)
        await conn.execute(
            """
            INSERT INTO authorization_codes
                (code_hash, client_id, user_email, redirect_uri,
                 code_challenge, scope, resource, expires_at, consumed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                _hash(code),
                client_id,
                user_email,
                redirect_uri,
                code_challenge,
                scope,
                resource,
                expires_at,
            ),
        )
        await conn.commit()

    async def consume_authorization_code(self, code: str) -> AuthCodeRecord | None:
        """Single-use, expiry-checked consume. Returns the record on the first
        valid call and None on second-call / expired / unknown.

        Implementation uses UPDATE ... WHERE consumed_at IS NULL AND
        expires_at > ? RETURNING * for atomicity within a single connection.
        """
        conn = self._ensure()
        code_hash = _hash(code)
        now = _now()
        async with self._lock:
            async with conn.execute(
                """
                UPDATE authorization_codes
                   SET consumed_at = ?
                 WHERE code_hash = ?
                   AND consumed_at IS NULL
                   AND expires_at > ?
                RETURNING client_id, user_email, redirect_uri, code_challenge,
                          scope, resource, expires_at, consumed_at
                """,
                (now, code_hash, now),
            ) as cur:
                row = await cur.fetchone()
            await conn.commit()
        if row is None:
            return None
        (
            client_id,
            user_email,
            redirect_uri,
            code_challenge,
            scope,
            resource,
            expires_at,
            consumed_at,
        ) = row
        return AuthCodeRecord(
            code_hash=code_hash,
            client_id=client_id,
            user_email=user_email,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scope=scope,
            resource=resource,
            expires_at=expires_at,
            consumed_at=consumed_at,
        )

    # ------------------------------------------------------------------
    # Refresh tokens (chained rotation)
    # ------------------------------------------------------------------

    async def store_refresh_token(
        self,
        token: str,
        *,
        client_id: str,
        user_email: str,
        chain_id: str,
        parent_hash: str | None,
        scope: str | None,
        ttl_seconds: int,
    ) -> RefreshTokenRecord:
        conn = self._ensure()
        token_hash = _hash(token)
        issued_at = _now()
        expires_at = issued_at + int(ttl_seconds)
        await conn.execute(
            """
            INSERT INTO refresh_tokens
                (token_hash, client_id, user_email, chain_id, parent_hash,
                 scope, issued_at, expires_at, rotated_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                token_hash,
                client_id,
                user_email,
                chain_id,
                parent_hash,
                scope,
                issued_at,
                expires_at,
            ),
        )
        await conn.commit()
        return RefreshTokenRecord(
            token_hash=token_hash,
            client_id=client_id,
            user_email=user_email,
            chain_id=chain_id,
            parent_hash=parent_hash,
            scope=scope,
            issued_at=issued_at,
            expires_at=expires_at,
            rotated_at=None,
            revoked_at=None,
        )

    async def get_refresh_token(self, token: str) -> RefreshTokenRecord | None:
        conn = self._ensure()
        token_hash = _hash(token)
        async with conn.execute(
            """
            SELECT client_id, user_email, chain_id, parent_hash, scope,
                   issued_at, expires_at, rotated_at, revoked_at
              FROM refresh_tokens
             WHERE token_hash = ?
            """,
            (token_hash,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        (
            client_id,
            user_email,
            chain_id,
            parent_hash,
            scope,
            issued_at,
            expires_at,
            rotated_at,
            revoked_at,
        ) = row
        return RefreshTokenRecord(
            token_hash=token_hash,
            client_id=client_id,
            user_email=user_email,
            chain_id=chain_id,
            parent_hash=parent_hash,
            scope=scope,
            issued_at=issued_at,
            expires_at=expires_at,
            rotated_at=rotated_at,
            revoked_at=revoked_at,
        )

    async def revoke_chain(self, chain_id: str) -> None:
        conn = self._ensure()
        now = _now()
        await conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE chain_id = ? AND revoked_at IS NULL",
            (now, chain_id),
        )
        await conn.commit()

    async def rotate_refresh_token(
        self,
        presented_token: str,
        new_token: str,
        ttl_seconds: int,
    ) -> RefreshTokenRecord | RefreshReuseDetected:
        """Atomic rotation. On reuse or revocation, the entire chain is
        revoked and REFRESH_REUSE_DETECTED is returned.
        """
        conn = self._ensure()
        presented_hash = _hash(presented_token)
        new_hash = _hash(new_token)
        now = _now()

        async with self._lock:
            async with conn.execute(
                """
                SELECT client_id, user_email, chain_id, scope,
                       expires_at, rotated_at, revoked_at
                  FROM refresh_tokens
                 WHERE token_hash = ?
                """,
                (presented_hash,),
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                # Unknown token — caller maps to invalid_grant. Don't revoke
                # anything because we don't know what chain it would belong to.
                return REFRESH_REUSE_DETECTED

            (
                client_id,
                user_email,
                chain_id,
                scope,
                expires_at,
                rotated_at,
                revoked_at,
            ) = row

            if revoked_at is not None or rotated_at is not None or expires_at <= now:
                # Reuse / revoked / expired — revoke the entire chain.
                await conn.execute(
                    "UPDATE refresh_tokens SET revoked_at = ? WHERE chain_id = ? AND revoked_at IS NULL",
                    (now, chain_id),
                )
                await conn.commit()
                return REFRESH_REUSE_DETECTED

            new_expires_at = now + int(ttl_seconds)
            await conn.execute(
                "UPDATE refresh_tokens SET rotated_at = ? WHERE token_hash = ?",
                (now, presented_hash),
            )
            await conn.execute(
                """
                INSERT INTO refresh_tokens
                    (token_hash, client_id, user_email, chain_id, parent_hash,
                     scope, issued_at, expires_at, rotated_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    new_hash,
                    client_id,
                    user_email,
                    chain_id,
                    presented_hash,
                    scope,
                    now,
                    new_expires_at,
                ),
            )
            await conn.commit()

        return RefreshTokenRecord(
            token_hash=new_hash,
            client_id=client_id,
            user_email=user_email,
            chain_id=chain_id,
            parent_hash=presented_hash,
            scope=scope,
            issued_at=now,
            expires_at=new_expires_at,
            rotated_at=None,
            revoked_at=None,
        )


def new_chain_id() -> str:
    return str(uuid.uuid4())
