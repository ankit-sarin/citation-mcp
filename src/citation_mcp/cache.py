"""aiosqlite-backed key-value cache with TTL semantics."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .scoring import (
    _author_to_string,
    _normalize_arxiv,
    _normalize_pmid,
    normalize_author_surname,
    normalize_doi,
    normalize_journal,
    normalize_title,
)

_DEFAULT_TTL_SECONDS = 14 * 24 * 60 * 60  # 14 days


def _default_db_path() -> Path:
    env = os.environ.get("CACHE_DB_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / "projects" / "citation-mcp" / "data" / "cache.db"


_CACHE_KEY_VERSION = 3  # v1.0.5: new TTL rule (C6) — retire entries cached under v1.0.4's
# rule, which kept results missing a database for 14 days. v1.0.4 (2): keys cover every
# accepted input field (R4).


def _plain(v: Any) -> str:
    return "" if v is None else str(v).strip()


def make_cache_key(input_citation: dict) -> str:
    """Build a stable cache key from a verifyCitation input.

    Every field the tool accepts (doi, pmid, arxiv_id, title, authors, year,
    journal, volume, issue, pages) contributes after its existing normalisation,
    so two inputs that differ in any of them get different keys (v1.0.4, R4).

    DOI-present → `verify:doi:<sha256(payload)>`
    Else        → `verify:meta:<sha256(payload)>`
    """
    doi = input_citation.get("doi")
    authors = input_citation.get("authors") or []
    payload = {
        "v": _CACHE_KEY_VERSION,
        "doi": normalize_doi(_plain(doi)) if doi else "",
        "pmid": _normalize_pmid(input_citation.get("pmid")) if input_citation.get("pmid") else "",
        "arxiv_id": _normalize_arxiv(_plain(input_citation.get("arxiv_id"))),
        "title": normalize_title(_plain(input_citation.get("title"))),
        "authors": [normalize_author_surname(_author_to_string({k: _plain(v) for k, v in a.items()} if isinstance(a, dict) else a))
                    for a in (authors if isinstance(authors, (list, tuple, str)) else [authors])],
        "year": _plain(input_citation.get("year")),
        "journal": normalize_journal(_plain(input_citation.get("journal"))),
        "volume": _plain(input_citation.get("volume")),
        "issue": _plain(input_citation.get("issue")),
        "pages": _plain(input_citation.get("pages")),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"verify:{'doi' if doi else 'meta'}:{digest}"


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
