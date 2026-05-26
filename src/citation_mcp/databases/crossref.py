"""Async Crossref REST API client (polite pool)."""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any

import httpx

from ..scoring import normalize_doi

_BASE_URL = "https://api.crossref.org"
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_EMAIL = "asarin@ucdavis.edu"
_USER_AGENT_TEMPLATE = "citation-mcp/0.1.0 (mailto:{email})"

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0  # seconds
_BACKOFF_CAP = 10.0


def _user_agent() -> str:
    email = os.environ.get("CROSSREF_POLITE_EMAIL", _DEFAULT_EMAIL)
    return _USER_AGENT_TEMPLATE.format(email=email)


def _earliest_year(item: dict) -> int | None:
    """Crossref date-parts: take earliest of published-print / published-online / created."""
    candidates: list[int] = []
    for field in ("published-print", "published-online", "issued", "created"):
        node = item.get(field)
        if not isinstance(node, dict):
            continue
        parts = node.get("date-parts")
        if not parts:
            continue
        try:
            year = int(parts[0][0])
        except (TypeError, ValueError, IndexError):
            continue
        candidates.append(year)
    if not candidates:
        return None
    return min(candidates)


def _normalize_record(item: dict) -> dict:
    """Convert a raw Crossref work record into the citation-mcp normalized shape."""
    raw_doi = item.get("DOI") or ""
    doi = normalize_doi(raw_doi) if raw_doi else None

    title_list = item.get("title") or []
    title = title_list[0] if isinstance(title_list, list) and title_list else (
        title_list if isinstance(title_list, str) else None
    )

    authors_raw = item.get("author") or []
    authors: list[dict] = []
    for a in authors_raw:
        if not isinstance(a, dict):
            continue
        authors.append(
            {
                "family": a.get("family") or "",
                "given": a.get("given") or "",
            }
        )

    container = item.get("container-title") or []
    journal = container[0] if isinstance(container, list) and container else (
        container if isinstance(container, str) else None
    )

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": _earliest_year(item),
        "journal": journal,
        "volume": item.get("volume"),
        "issue": item.get("issue"),
        "pages": item.get("page"),
        "type": item.get("type"),
        "pmid": None,
        "source": "crossref",
    }


class CrossrefClient:
    """Async Crossref polite-pool client."""

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT, base_url: str = _BASE_URL,
                 transport: httpx.AsyncBaseTransport | None = None):
        headers = {
            "User-Agent": _user_agent(),
            "Accept": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def _request_with_retries(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Issue a request with bounded retries on 429/5xx."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt >= _MAX_RETRIES:
                    raise
                await asyncio.sleep(self._compute_backoff(attempt))
                continue

            # Honor polite-pool rate-limit headers when present.
            await self._maybe_respect_rate_limit(response)

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= _MAX_RETRIES:
                    return response
                retry_after = response.headers.get("Retry-After")
                delay = self._compute_backoff(attempt)
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
                await asyncio.sleep(min(delay, _BACKOFF_CAP))
                continue

            return response
        # Exhausted retries.
        if last_exc:
            raise last_exc
        raise RuntimeError("Crossref request failed after retries.")

    @staticmethod
    def _compute_backoff(attempt: int) -> float:
        delay = min(_BASE_BACKOFF * (2 ** attempt), _BACKOFF_CAP)
        return delay + random.uniform(0, delay / 2)

    @staticmethod
    async def _maybe_respect_rate_limit(response: httpx.Response) -> None:
        limit = response.headers.get("X-Rate-Limit-Limit")
        interval = response.headers.get("X-Rate-Limit-Interval")
        if limit is None or interval is None:
            return
        try:
            limit_n = int(limit)
        except ValueError:
            return
        # Parse "1s" style interval — Crossref uses "1s".
        seconds: float | None = None
        if interval.endswith("s") and interval[:-1].isdigit():
            seconds = float(interval[:-1])
        else:
            try:
                seconds = float(interval)
            except ValueError:
                seconds = None
        if seconds is None or limit_n <= 0:
            return
        # If we're at the floor of remaining capacity, sleep proactively.
        remaining = response.headers.get("X-Rate-Limit-Remaining")
        if remaining is not None:
            try:
                if int(remaining) <= 1:
                    await asyncio.sleep(seconds)
            except ValueError:
                pass

    async def search_by_doi(self, doi: str) -> dict | None:
        """GET /works/{doi}. Returns normalized record dict, or None on 404."""
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        # Crossref's /works/{doi} endpoint is case-insensitive but the polite pool
        # is friendlier with normalized lowercase DOIs.
        response = await self._request_with_retries("GET", f"/works/{normalized}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        return _normalize_record(message)

    async def search_by_metadata(
        self,
        title: str,
        author: str | None = None,
        year: int | None = None,
        journal: str | None = None,
        rows: int = 5,
    ) -> list[dict]:
        """GET /works with bibliographic query parameters. Returns normalized records."""
        params: dict[str, Any] = {"rows": rows}
        if title:
            params["query.title"] = title
        if author:
            params["query.author"] = author
        if journal:
            params["query.container-title"] = journal
        if year is not None:
            # Tolerant ±1 year window.
            params["filter"] = f"from-pub-date:{year - 1},until-pub-date:{year + 1}"

        response = await self._request_with_retries("GET", "/works", params=params)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
        message = payload.get("message") or {}
        items = message.get("items") or []
        return [_normalize_record(item) for item in items if isinstance(item, dict)]

    async def aclose(self) -> None:
        await self._client.aclose()
