"""End-to-end tests for verify_citation against a mocked Crossref backend."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from citation_mcp.databases.crossref import CrossrefClient
from citation_mcp.server import verify_citation

from .conftest import (
    POLACK_DOI,
    POLACK_RAW_CROSSREF,
    POLACK_TITLE,
    make_mock_crossref_client,
    works_list_response,
    works_response,
)


@pytest_asyncio.fixture
async def crossref():
    """CrossrefClient that always returns the canonical Polack record."""
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


# ---------------------------------------------------------------------------
# Fixture 1: DOI-only input
# ---------------------------------------------------------------------------


async def test_fixture_1_doi_only(crossref: CrossrefClient, cache):
    input_citation = {"doi": "10.1056/NEJMoa2034577"}
    result = await verify_citation(input_citation, crossref, cache)

    assert result["verified"] is True
    assert result["confidence"] == 1.0
    assert result["match_quality"] == "high"
    assert result["databases_confirmed"] == ["crossref"]
    assert result["score_breakdown"]["layer"] == "identifier"
    assert result["canonical"]["doi"] == POLACK_DOI
    assert result["canonical"]["title"] == POLACK_TITLE


# ---------------------------------------------------------------------------
# Fixture 2: clean metadata-only input
# ---------------------------------------------------------------------------


async def test_fixture_2_metadata_clean(crossref: CrossrefClient, cache):
    input_citation = {
        "title": POLACK_TITLE,
        "authors": ["Polack, Fernando P."],
        "year": 2020,
    }
    result = await verify_citation(input_citation, crossref, cache)

    assert result["verified"] is True
    assert result["confidence"] >= 0.90
    assert result["match_quality"] == "high"
    assert result["score_breakdown"]["title_sim"] == 1.0
    assert result["canonical"]["doi"] == POLACK_DOI


# ---------------------------------------------------------------------------
# Fixture 3: year off by 5+
# ---------------------------------------------------------------------------


async def test_fixture_3_wrong_year(crossref: CrossrefClient, cache):
    input_citation = {
        "title": POLACK_TITLE,
        "authors": ["Polack, Fernando P."],
        "year": 2015,
    }
    result = await verify_citation(input_citation, crossref, cache)

    assert result["verified"] is False
    assert result["match_quality"] == "none"
    assert result["score_breakdown"]["rejected_by"] == "year_off_by_more_than_one"


# ---------------------------------------------------------------------------
# Fixture 4: single-character title typo
# ---------------------------------------------------------------------------


async def test_fixture_4_title_typo(crossref: CrossrefClient, cache):
    input_citation = {
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vacccine",  # extra 'c'
        "authors": ["Polack, Fernando P."],
        "year": 2020,
    }
    result = await verify_citation(input_citation, crossref, cache)

    assert result["verified"] is True
    assert result["match_quality"] in ("medium", "high")
    assert result["confidence"] >= 0.75
    assert result["score_breakdown"]["title_sim"] >= 0.95


# ---------------------------------------------------------------------------
# Fixture 5: first-author surname typo + exact title
# ---------------------------------------------------------------------------


async def test_fixture_5_first_author_typo(crossref: CrossrefClient, cache):
    input_citation = {
        "title": POLACK_TITLE,
        "authors": ["Polak, Fernando P."],  # missing 'c' — Polak vs Polack
        "year": 2020,
    }
    result = await verify_citation(input_citation, crossref, cache)

    # Title sim == 1.0 means sanity guard 2 does NOT trigger.
    assert result["verified"] is True
    assert result["match_quality"] in ("medium", "high")
    assert result["score_breakdown"]["title_sim"] == 1.0
    assert result["score_breakdown"]["first_author_match"] == 0.0
    assert result["confidence"] >= 0.75


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_requires_at_least_one_of_doi_pmid_title(crossref: CrossrefClient, cache):
    result = await verify_citation({"year": 2020}, crossref, cache)
    assert "error" in result
    assert result["error"] == "at_least_one_of_doi_pmid_title_required"


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


async def test_cache_hit_returns_with_warning(crossref: CrossrefClient, cache):
    input_citation = {"doi": "10.1056/NEJMoa2034577"}
    first = await verify_citation(input_citation, crossref, cache)
    second = await verify_citation(input_citation, crossref, cache)

    # Second call should bear a cache-source warning.
    assert any(w.get("source") == "cache" for w in second["warnings"])
    # Core fields should match.
    assert first["match_quality"] == second["match_quality"]
    assert first["confidence"] == second["confidence"]


# ---------------------------------------------------------------------------
# Crossref error path
# ---------------------------------------------------------------------------


async def test_crossref_500_reports_databases_failed(cache):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")
    client = make_mock_crossref_client(handler)
    try:
        result = await verify_citation({"doi": "10.1/never-existed"}, client, cache)
        assert result["verified"] is False
        assert result["databases_failed"] == ["crossref"]
        assert any(w.get("source") == "crossref" for w in result["warnings"])
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# No-match (DOI 404)
# ---------------------------------------------------------------------------


async def test_doi_404_returns_no_match(cache):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    client = make_mock_crossref_client(handler)
    try:
        result = await verify_citation({"doi": "10.1/never-existed"}, client, cache)
        assert result["verified"] is False
        assert result["match_quality"] == "none"
        assert result["databases_confirmed"] == []
        assert result["databases_failed"] == []
    finally:
        await client.aclose()
