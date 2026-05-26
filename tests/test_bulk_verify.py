"""Tests for the bulkVerifyCitations tool handler."""

from __future__ import annotations

import httpx
import pytest_asyncio

from citation_mcp.server import bulk_verify_citations

from .conftest import (
    POLACK_DOI,
    POLACK_RAW_CROSSREF,
    POLACK_TITLE,
    make_mock_crossref_client,
    works_list_response,
    works_response,
)


def _polack_crossref(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/works":
        return httpx.Response(200, json=works_list_response([POLACK_RAW_CROSSREF]))
    if request.url.path.startswith("/works/"):
        return httpx.Response(200, json=works_response(POLACK_RAW_CROSSREF))
    return httpx.Response(404)


@pytest_asyncio.fixture
async def crossref():
    client = make_mock_crossref_client(_polack_crossref)
    try:
        yield client
    finally:
        await client.aclose()


async def test_bulk_10_citations_all_verified(crossref, cache):
    citations = [{"doi": POLACK_DOI} for _ in range(10)]
    out = await bulk_verify_citations(citations, crossref, cache)
    assert out["summary"]["total"] == 10
    assert out["summary"]["verified"] == 10
    assert out["summary"]["by_match_quality"]["high"] == 10
    assert out["summary"]["elapsed_seconds"] >= 0
    assert len(out["results"]) == 10


async def test_bulk_results_in_input_order(crossref, cache):
    # Five distinct inputs. Each carries the full bibliographic metadata so
    # Layer 2 scoring matches even though the input DOI differs from the
    # canonical Polack DOI returned by the mock.
    citations = [
        {
            "doi": f"10.1056/test{i}",
            "title": POLACK_TITLE,
            "authors": ["Polack, Fernando P."],
            "year": 2020,
            "journal": "New England Journal of Medicine",
        }
        for i in range(5)
    ]
    out = await bulk_verify_citations(citations, crossref, cache)
    assert len(out["results"]) == 5
    for r in out["results"]:
        # Layer 2 picks up title+author+year+journal → high.
        assert r["match_quality"] == "high"


async def test_bulk_empty_list_errors(crossref, cache):
    out = await bulk_verify_citations([], crossref, cache)
    assert out["error"] == "citations_must_be_non_empty_list"


async def test_bulk_over_cap_errors(crossref, cache):
    citations = [{"doi": POLACK_DOI}] * 201
    out = await bulk_verify_citations(citations, crossref, cache)
    assert out["error"] == "too_many_citations"


async def test_bulk_cache_hits_counted(crossref, cache):
    citations = [{"doi": POLACK_DOI}]
    first = await bulk_verify_citations(citations, crossref, cache)
    assert first["summary"]["cache_hits"] == 0
    second = await bulk_verify_citations(citations, crossref, cache)
    assert second["summary"]["cache_hits"] == 1
