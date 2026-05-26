"""Async Semantic Scholar Graph API client.

Auth: `x-api-key: <key>` header from SEMANTIC_SCHOLAR_API_KEY. With a key,
20 req/sec semaphore; without, fall back to 1 req/sec and log a warning.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

import httpx

from ..scoring import normalize_doi
from ..log_redaction import reraise_redacted

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.semanticscholar.org/graph/v1"
_DEFAULT_TIMEOUT = 15.0

_RATE_LIMIT_AUTHENTICATED = 20
_RATE_LIMIT_ANONYMOUS = 1

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0
_BACKOFF_CAP = 10.0

_FIELDS = (
    "paperId,corpusId,externalIds,title,authors,year,venue,publicationVenue,"
    "journal,abstract,citationCount,publicationTypes"
)


def _api_key() -> str | None:
    return os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None


def _split_name(display_name: str) -> dict:
    if not display_name:
        return {"family": "", "given": ""}
    s = display_name.strip()
    if "," in s:
        family, _, given = s.partition(",")
        return {"family": family.strip(), "given": given.strip()}
    tokens = s.split()
    if len(tokens) == 1:
        return {"family": tokens[0], "given": ""}
    return {"family": tokens[-1], "given": " ".join(tokens[:-1])}


def _normalize_record(item: dict) -> dict:
    """Convert a raw Semantic Scholar paper into the citation-mcp normalized shape."""
    ext = item.get("externalIds") or {}
    raw_doi = ext.get("DOI") or ""
    doi = normalize_doi(raw_doi) if raw_doi else None
    pmid = ext.get("PubMed") or None
    arxiv_id = ext.get("ArXiv") or None

    authors_raw = item.get("authors") or []
    authors: list[dict] = []
    for a in authors_raw:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or ""
        if name:
            authors.append(_split_name(name))

    venue = item.get("publicationVenue") or {}
    journal_obj = item.get("journal") or {}
    journal_name = (
        (venue.get("name") if isinstance(venue, dict) else None)
        or (journal_obj.get("name") if isinstance(journal_obj, dict) else None)
        or item.get("venue")
        or None
    )

    pub_types = item.get("publicationTypes") or []
    pub_type = pub_types[0] if pub_types else None

    return {
        "paper_id": item.get("paperId"),
        "doi": doi,
        "pmid": str(pmid) if pmid is not None else None,
        "arxiv_id": str(arxiv_id) if arxiv_id is not None else None,
        "title": item.get("title"),
        "authors": authors,
        "year": item.get("year"),
        "journal": journal_name,
        "volume": None,
        "issue": None,
        "pages": None,
        "abstract": item.get("abstract"),
        "citation_count": item.get("citationCount"),
        "type": pub_type,
        "source": "semantic_scholar",
    }


class SemanticScholarClient:
    """Async Semantic Scholar Graph API client."""

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        base_url: str = _BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = _api_key()
        if self._api_key is None:
            logger.warning(
                "SEMANTIC_SCHOLAR_API_KEY is not set; Semantic Scholar client "
                "will operate at 1 req/sec (anonymous tier)."
            )
            rps = _RATE_LIMIT_ANONYMOUS
        else:
            rps = _RATE_LIMIT_AUTHENTICATED
        self._enabled = True
        self._semaphore = asyncio.Semaphore(rps)
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers=headers,
        )

    async def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        async with self._semaphore:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    response = await self._client.get(url, params=params)
                except (httpx.TransportError, httpx.TimeoutException):
                    if attempt >= _MAX_RETRIES:
                        raise
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt >= _MAX_RETRIES:
                        return response
                    retry_after = response.headers.get("Retry-After")
                    delay = self._backoff(attempt)
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                    await asyncio.sleep(min(delay, _BACKOFF_CAP))
                    continue
                return response
            raise RuntimeError("Semantic Scholar request failed after retries.")

    @staticmethod
    def _backoff(attempt: int) -> float:
        delay = min(_BASE_BACKOFF * (2 ** attempt), _BACKOFF_CAP)
        return delay + random.uniform(0, delay / 2)

    async def _get_paper(self, path: str) -> dict | None:
        response = await self._request(path, params={"fields": _FIELDS})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as _err:
                reraise_redacted(_err)
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return _normalize_record(payload)

    async def search_by_doi(self, doi: str) -> dict | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        return await self._get_paper(f"/paper/DOI:{normalized}")

    async def search_by_paper_id(self, paper_id: str) -> dict | None:
        pid = (paper_id or "").strip()
        if not pid:
            return None
        return await self._get_paper(f"/paper/{pid}")

    async def search_by_pmid(self, pmid: str) -> dict | None:
        pmid_s = str(pmid).strip().lstrip("0") or "0"
        if not pmid_s.isdigit():
            return None
        return await self._get_paper(f"/paper/PMID:{pmid_s}")

    async def search_by_metadata(
        self,
        title: str,
        author: str | None = None,
        year: int | None = None,
        rows: int = 5,
    ) -> list[dict]:
        if not title:
            return []
        params: dict[str, Any] = {
            "query": title,
            "limit": rows,
            "fields": _FIELDS,
        }
        if year is not None:
            params["year"] = f"{year - 1}-{year + 1}"
        response = await self._request("/paper/search", params=params)
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as _err:
                reraise_redacted(_err)
        try:
            payload = response.json()
        except ValueError:
            return []
        data = payload.get("data") or []
        return [_normalize_record(it) for it in data if isinstance(it, dict)]

    async def aclose(self) -> None:
        await self._client.aclose()
