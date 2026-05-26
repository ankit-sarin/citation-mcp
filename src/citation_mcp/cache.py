"""aiosqlite-backed key-value cache with TTL semantics."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .scoring import normalize_author_surname, normalize_doi, normalize_title

_DEFAULT_TTL_SECONDS = 14 * 24 * 60 * 60  # 14 days


def _default_db_path() -> Path:
    env = os.environ.get("CACHE_DB_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / "projects" / "citation-mcp" / "data" / "cache.db"


def make_cache_key(input_citation: dict) -> str:
    """Build a stable cache key from a verifyCitation input.

    DOI-present → `verify:doi:<normalized_doi>`
    Else        → `verify:meta:<sha256(normalized_title|first_author_surname|year)>`
    """
    doi = input_citation.get("doi")
    if doi:
        return f"verify:doi:{normalize_doi(doi)}"

    title = normalize_title(input_citation.get("title") or "")
    authors = input_citation.get("authors") or []
    first_author = normalize_author_surname(authors[0]) if authors else ""
    year = input_citation.get("year")
    year_s = str(year) if year is not None else ""
    payload = f"{title}|{first_author}|{year_s}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"verify:meta:{digest}"


class Cache:
    """Async key-value cache backed by SQLite."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            self._db_path = _default_db_path()
        else:
            self._db_path = Path(db_path).expanduser() if not isinstance(db_path, Path) else db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        # In-memory paths (":memory:") have no parent directory.
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL,
                type TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expires ON cache_entries(expires_at)"
        )
        await self._conn.commit()

    def _ensure_open(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Cache.init() must be called before use.")
        return self._conn

    async def get(self, key: str) -> Any | None:
        conn = self._ensure_open()
        now = int(time.time())
        async with conn.execute(
            "SELECT value, expires_at FROM cache_entries WHERE key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        value_blob, expires_at = row
        if expires_at <= now:
            await self.delete(key)
            return None
        return json.loads(value_blob)

    async def set(
        self,
        key: str,
        value: Any,
        type_: str,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        conn = self._ensure_open()
        expires_at = int(time.time()) + int(ttl_seconds)
        payload = json.dumps(value, default=str).encode("utf-8")
        await conn.execute(
            """
            INSERT INTO cache_entries (key, value, type, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                type = excluded.type,
                expires_at = excluded.expires_at
            """,
            (key, payload, type_, expires_at),
        )
        await conn.commit()

    async def delete(self, key: str) -> None:
        conn = self._ensure_open()
        await conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
        await conn.commit()

    async def purge_expired(self) -> int:
        conn = self._ensure_open()
        now = int(time.time())
        cursor = await conn.execute(
            "DELETE FROM cache_entries WHERE expires_at <= ?",
            (now,),
        )
        await conn.commit()
        return cursor.rowcount or 0

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
