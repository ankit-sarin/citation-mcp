"""Async arXiv Atom API client.

Rate-limit: arXiv asks for ≥3 seconds between requests. We enforce this with a
coroutine-safe lock that records the timestamp of each release and sleeps the
next caller until the gap has elapsed. This is intentionally NOT a semaphore —
the constraint is min-time-between-requests, not max-concurrency.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from ..scoring import normalize_doi

logger = logging.getLogger(__name__)

_BASE_URL = "https://export.arxiv.org/api/query"
_DEFAULT_TIMEOUT = 30.0
_MIN_INTERVAL_SECONDS = 3.0

_MAX_RETRIES = 1   # initial attempt + 1 retry
_BASE_BACKOFF = 5.0


class ArxivRateLimited(Exception):
    """Raised when arXiv returns 429 after all retries.

    Callers should treat this as a soft signal (not a hard databases_failed
    entry) — arXiv 429s are routine and the other DBs typically cover the work.
    """

_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"

_ARXIV_ID_RE = re.compile(r"abs/([^/]+?)(v\d+)?$")
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)


def _strip_version(arxiv_id: str) -> str:
    return _ARXIV_VERSION_RE.sub("", arxiv_id.strip())


def _atom(tag: str) -> str:
    return f"{{{_ATOM_NS}}}{tag}"


def _arxiv(tag: str) -> str:
    return f"{{{_ARXIV_NS}}}{tag}"


def _parse_year(published: str | None) -> int | None:
    if not published:
        return None
    m = re.match(r"^(\d{4})", published.strip())
    if not m:
        return None
    return int(m.group(1))


def _parse_entry(entry: ET.Element) -> dict | None:
    raw_id = entry.findtext(_atom("id"))
    if not raw_id:
        return None
    m = _ARXIV_ID_RE.search(raw_id)
    arxiv_id = m.group(1) if m else _strip_version(raw_id.split("/")[-1])

    title = entry.findtext(_atom("title")) or ""
    title = re.sub(r"\s+", " ", title).strip() or None
    summary = entry.findtext(_atom("summary")) or ""
    abstract = re.sub(r"\s+", " ", summary).strip() or None
    published = entry.findtext(_atom("published"))
    year = _parse_year(published)

    authors: list[dict] = []
    for a in entry.findall(_atom("author")):
        name = (a.findtext(_atom("name")) or "").strip()
        if not name:
            continue
        if "," in name:
            family, _, given = name.partition(",")
            authors.append({"family": family.strip(), "given": given.strip()})
        else:
            tokens = name.split()
            if len(tokens) == 1:
                authors.append({"family": tokens[0], "given": ""})
            else:
                authors.append({"family": tokens[-1], "given": " ".join(tokens[:-1])})

    doi_text = entry.findtext(_arxiv("doi"))
    doi = normalize_doi(doi_text) if doi_text else None
    journal_ref = entry.findtext(_arxiv("journal_ref"))
    journal = journal_ref.strip() if journal_ref else None

    return {
        "arxiv_id": arxiv_id,
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "abstract": abstract,
        "type": "journal-article" if journal else "preprint",
        "source": "arxiv",
    }


def _parse_feed(xml_bytes: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("arXiv XML parse failed: %s", e)
        return []
    out: list[dict] = []
    for entry in root.findall(_atom("entry")):
        # arXiv returns a synthetic empty entry with an error <title> on miss;
        # filter to entries that look like real records.
        if entry.findtext(_atom("id"), "").strip() == "http://arxiv.org/api/errors":
            continue
        parsed = _parse_entry(entry)
        if parsed and parsed.get("arxiv_id"):
            out.append(parsed)
    return out


class ArxivClient:
    """Async arXiv Atom API client with min-spacing enforcement."""

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        base_url: str = _BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = _MIN_INTERVAL_SECONDS,
    ) -> None:
        self._min_interval = min_interval
        self._spacing_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        # Note: arXiv API requires the absolute URL; we don't use base_url path
        # but pass the full URL on each call. We still init the client with
        # base_url for convenience in tests that use MockTransport.
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "citation-mcp/0.2.1"},
        )

    async def _request(self, params: dict[str, Any]) -> httpx.Response:
        last_429 = False
        for attempt in range(_MAX_RETRIES + 1):
            async with self._spacing_lock:
                if self._last_request_at is not None:
                    elapsed = time.monotonic() - self._last_request_at
                    if elapsed < self._min_interval:
                        await asyncio.sleep(self._min_interval - elapsed)
                try:
                    response = await self._client.get(self._base_url, params=params)
                finally:
                    self._last_request_at = time.monotonic()
            if response.status_code == 429:
                last_429 = True
                if attempt >= _MAX_RETRIES:
                    raise ArxivRateLimited(
                        "arXiv rate-limited after "
                        f"{_MAX_RETRIES + 1} attempts"
                    )
                await asyncio.sleep(_BASE_BACKOFF + random.uniform(0, 1.0))
                continue
            if 500 <= response.status_code < 600:
                if attempt >= _MAX_RETRIES:
                    return response
                await asyncio.sleep(_BASE_BACKOFF + random.uniform(0, 1.0))
                continue
            return response
        if last_429:
            raise ArxivRateLimited("arXiv rate-limited (all retries 429)")
        raise RuntimeError("arXiv request failed after retries.")

    async def search_by_arxiv_id(self, arxiv_id: str) -> dict | None:
        if not arxiv_id:
            return None
        clean = _strip_version(arxiv_id.replace("arXiv:", "").replace("arxiv:", ""))
        response = await self._request({"id_list": clean, "max_results": 1})
        if response.status_code != 200:
            response.raise_for_status()
        records = _parse_feed(response.content)
        return records[0] if records else None

    async def search_by_doi(self, doi: str) -> dict | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        response = await self._request({
            "search_query": f"doi:{normalized}",
            "max_results": 1,
        })
        if response.status_code != 200:
            response.raise_for_status()
        records = _parse_feed(response.content)
        return records[0] if records else None

    async def search_by_metadata(
        self,
        title: str,
        author: str | None = None,
        year: int | None = None,
        rows: int = 5,
    ) -> list[dict]:
        if not title:
            return []
        parts: list[str] = [f"ti:\"{title}\""]
        if author:
            parts.append(f"au:\"{author}\"")
        if year is not None:
            # arXiv submittedDate filter format: [YYYYMMDDHHMM TO YYYYMMDDHHMM]
            start = f"{year - 1}01010000"
            end = f"{year + 1}12312359"
            parts.append(f"submittedDate:[{start} TO {end}]")
        query = " AND ".join(parts)
        response = await self._request({
            "search_query": query,
            "max_results": rows,
        })
        if response.status_code != 200:
            response.raise_for_status()
        return _parse_feed(response.content)

    async def aclose(self) -> None:
        await self._client.aclose()
