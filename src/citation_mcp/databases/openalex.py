"""Async OpenAlex REST API client.

Authentication: as of February 2026, OpenAlex deprecated the `mailto=` polite-pool
mechanism. The free-tier daily quota is gated by an API key passed as the
`api_key=` query parameter (read from OPENALEX_API_KEY). Per-second limits are
generous; the binding constraint is the daily quota.

Singleton lookups (`/works/doi:<doi>`, `/works/W123...`) are unlimited under
the daily quota; search calls cost ~10 credits each.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import Any

import httpx

from ..scoring import normalize_doi
from ..log_redaction import reraise_redacted

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openalex.org"
_DEFAULT_TIMEOUT = 15.0
_RATE_LIMIT_RPS = 50

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0
_BACKOFF_CAP = 10.0

_OPENALEX_ID_RE = re.compile(r"(W\d+)", re.IGNORECASE)


def _api_key() -> str | None:
    return os.environ.get("OPENALEX_API_KEY") or None


def _extract_openalex_id(url_or_id: str | None) -> str | None:
    if not url_or_id:
        return None
    m = _OPENALEX_ID_RE.search(url_or_id)
    if not m:
        return None
    return m.group(1).upper()


def reconstruct_abstract_from_inverted_index(inv: dict | None) -> str | None:
    """OpenAlex abstracts are stored as {word: [positions]}; rebuild the text."""
    if not inv or not isinstance(inv, dict):
        return None
    positions: dict[int, str] = {}
    for word, posns in inv.items():
        if not isinstance(posns, list):
            continue
        for p in posns:
            try:
                positions[int(p)] = str(word)
            except (TypeError, ValueError):
                continue
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions.keys()))


def _split_name(display_name: str) -> dict:
    """Split 'First Middle Last' into {family, given} by last-token convention."""
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
    """Convert a raw OpenAlex Work into the citation-mcp normalized shape."""
    raw_doi = item.get("doi") or ""
    if raw_doi:
        doi = normalize_doi(raw_doi)
    else:
        doi = None

    openalex_id = _extract_openalex_id(item.get("id"))

    ids = item.get("ids") or {}
    raw_pmid = ids.get("pmid") or ""
    pmid: str | None = None
    if raw_pmid:
        m = re.search(r"(\d+)$", str(raw_pmid))
        if m:
            pmid = m.group(1)

    authorships = item.get("authorships") or []
    authors: list[dict] = []
    for a in authorships:
        if not isinstance(a, dict):
            continue
        author_obj = a.get("author") or {}
        display = author_obj.get("display_name") or ""
        if display:
            authors.append(_split_name(display))

    # host_venue was deprecated by OpenAlex in late 2025 and is being removed —
    # use primary_location.source.display_name as the sole journal source.
    primary = item.get("primary_location") or {}
    primary_source = (primary.get("source") or {}) if isinstance(primary, dict) else {}
    journal = primary_source.get("display_name") or None

    biblio = item.get("biblio") or {}
    volume = biblio.get("volume")
    issue = biblio.get("issue")
    first_page = biblio.get("first_page")
    last_page = biblio.get("last_page")
    if first_page and last_page:
        pages = f"{first_page}-{last_page}"
    else:
        pages = first_page or last_page or None

    abstract = reconstruct_abstract_from_inverted_index(
        item.get("abstract_inverted_index")
    )

    return {
        "doi": doi,
        "openalex_id": openalex_id,
        "pmid": pmid,
        "title": item.get("display_name") or item.get("title"),
        "authors": authors,
        "year": item.get("publication_year"),
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "abstract": abstract,
        "citation_count": item.get("cited_by_count"),
        "type": item.get("type"),
        "source": "openalex",
    }


class OpenAlexClient:
    """Async OpenAlex REST client."""

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        base_url: str = _BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = _api_key()
        if self._api_key is None:
            logger.warning(
                "OPENALEX_API_KEY is not set; OpenAlex client disabled. "
                "Set the env var to enable OpenAlex queries."
            )
            self._enabled = False
        else:
            self._enabled = True
        self._semaphore = asyncio.Semaphore(_RATE_LIMIT_RPS)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        p: dict[str, Any] = {}
        if self._api_key:
            p["api_key"] = self._api_key
        if extra:
            p.update({k: v for k, v in extra.items() if v is not None})
        return p

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
            raise RuntimeError("OpenAlex request failed after retries.")

    @staticmethod
    def _backoff(attempt: int) -> float:
        delay = min(_BASE_BACKOFF * (2 ** attempt), _BACKOFF_CAP)
        return delay + random.uniform(0, delay / 2)

    async def search_by_doi(self, doi: str) -> dict | None:
        if not self._enabled:
            return None
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        response = await self._request(f"/works/doi:{normalized}", params=self._params())
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

    async def search_by_work_id(self, openalex_id: str) -> dict | None:
        if not self._enabled:
            return None
        wid = _extract_openalex_id(openalex_id) or openalex_id.strip().upper()
        if not wid:
            return None
        response = await self._request(f"/works/{wid}", params=self._params())
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
        return _normalize_record(payload)

    async def search_by_metadata(
        self,
        title: str,
        author: str | None = None,
        year: int | None = None,
        journal: str | None = None,
        rows: int = 5,
    ) -> list[dict]:
        if not self._enabled:
            return []
        if not title:
            return []
        filters: list[str] = []
        if year is not None:
            filters.append(f"publication_year:{year - 1}|{year}|{year + 1}")
        params = self._params({
            "search": title,
            "per-page": rows,
        })
        if filters:
            params["filter"] = ",".join(filters)
        response = await self._request("/works", params=params)
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
        items = payload.get("results") or []
        return [_normalize_record(it) for it in items if isinstance(it, dict)]

    async def aclose(self) -> None:
        await self._client.aclose()
