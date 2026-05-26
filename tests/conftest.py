"""Shared test fixtures: mocked Crossref client + in-memory cache."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest_asyncio

from citation_mcp.cache import Cache
from citation_mcp.databases.crossref import CrossrefClient

# ---------------------------------------------------------------------------
# Canonical Crossref-shaped record for Polack et al. 2020 (used by fixtures)
# ---------------------------------------------------------------------------

POLACK_DOI = "10.1056/nejmoa2034577"
POLACK_TITLE = "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine"

POLACK_RAW_CROSSREF: dict[str, Any] = {
    "DOI": "10.1056/NEJMoa2034577",
    "title": [POLACK_TITLE],
    "container-title": ["New England Journal of Medicine"],
    "author": [
        {"family": "Polack", "given": "Fernando P."},
        {"family": "Thomas", "given": "Stephen J."},
        {"family": "Kitchin", "given": "Nicholas"},
        {"family": "Absalon", "given": "Judith"},
        {"family": "Gurtman", "given": "Alejandra"},
    ],
    "published-print": {"date-parts": [[2020, 12, 31]]},
    "published-online": {"date-parts": [[2020, 12, 10]]},
    "created": {"date-parts": [[2020, 12, 10]]},
    "volume": "383",
    "issue": "27",
    "page": "2603-2615",
    "type": "journal-article",
}


def works_response(item: dict[str, Any]) -> dict[str, Any]:
    """Wrap a raw Crossref work item in the /works/{doi} response envelope."""
    return {
        "status": "ok",
        "message-type": "work",
        "message-version": "1.0.0",
        "message": item,
    }


def works_list_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of raw Crossref work items in the /works (list) response envelope."""
    return {
        "status": "ok",
        "message-type": "work-list",
        "message-version": "1.0.0",
        "message": {
            "total-results": len(items),
            "items-per-page": len(items),
            "items": items,
        },
    }


# ---------------------------------------------------------------------------
# httpx MockTransport-based CrossrefClient fixture
# ---------------------------------------------------------------------------


HandlerFn = Callable[[httpx.Request], httpx.Response]


def make_mock_crossref_client(handler: HandlerFn) -> CrossrefClient:
    """Build a CrossrefClient whose underlying httpx client is driven by `handler`."""
    transport = httpx.MockTransport(handler)
    return CrossrefClient(transport=transport)


@pytest_asyncio.fixture
async def cache(tmp_path):
    """In-tmp-path Cache instance, initialized and torn down per test."""
    db_path = tmp_path / "test_cache.db"
    c = Cache(db_path=db_path)
    await c.init()
    try:
        yield c
    finally:
        await c.close()


@pytest_asyncio.fixture
async def polack_doi_client():
    """CrossrefClient that returns the canonical Polack record for /works/{doi}."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/works/"):
            return httpx.Response(200, json=works_response(POLACK_RAW_CROSSREF))
        return httpx.Response(404)
    client = make_mock_crossref_client(handler)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def polack_metadata_client():
    """CrossrefClient that returns the canonical Polack record for /works metadata query."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/works":
            return httpx.Response(200, json=works_list_response([POLACK_RAW_CROSSREF]))
        if request.url.path.startswith("/works/"):
            return httpx.Response(200, json=works_response(POLACK_RAW_CROSSREF))
        return httpx.Response(404)
    client = make_mock_crossref_client(handler)
    try:
        yield client
    finally:
        await client.aclose()
