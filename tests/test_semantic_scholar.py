"""Tests for the Semantic Scholar client."""

from __future__ import annotations

import httpx
import pytest_asyncio

from citation_mcp.databases.semantic_scholar import SemanticScholarClient

from .conftest import (
    POLACK_PMID,
    POLACK_S2_ID,
    POLACK_S2_JSON,
    POLACK_TITLE,
    make_mock_s2_client,
    polack_s2_handler,
)


@pytest_asyncio.fixture
async def s2_client(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-key")
    client = make_mock_s2_client(polack_s2_handler)
    try:
        yield client
    finally:
        await client.aclose()


async def test_search_by_doi(s2_client: SemanticScholarClient):
    rec = await s2_client.search_by_doi("10.1056/NEJMoa2034577")
    assert rec is not None
    assert rec["doi"] == "10.1056/nejmoa2034577"
    assert rec["pmid"] == POLACK_PMID
    assert rec["title"] == POLACK_TITLE
    assert rec["year"] == 2020
    assert rec["journal"] == "New England Journal of Medicine"
    assert rec["paper_id"] == POLACK_S2_ID
    assert rec["citation_count"] == 11800
    assert rec["source"] == "semantic_scholar"


async def test_search_by_paper_id(s2_client: SemanticScholarClient):
    rec = await s2_client.search_by_paper_id(POLACK_S2_ID)
    assert rec is not None
    assert rec["paper_id"] == POLACK_S2_ID


async def test_search_by_pmid(s2_client: SemanticScholarClient):
    rec = await s2_client.search_by_pmid(POLACK_PMID)
    assert rec is not None
    assert rec["pmid"] == POLACK_PMID


async def test_search_by_metadata(s2_client: SemanticScholarClient):
    results = await s2_client.search_by_metadata(
        title=POLACK_TITLE, year=2020, rows=5,
    )
    assert len(results) == 1
    assert results[0]["paper_id"] == POLACK_S2_ID


async def test_x_api_key_header_included(monkeypatch):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["x_api_key"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json=POLACK_S2_JSON)

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "secret-s2-key")
    client = make_mock_s2_client(handler)
    try:
        await client.search_by_doi("10.1056/NEJMoa2034577")
    finally:
        await client.aclose()
    assert captured["x_api_key"] == "secret-s2-key"


async def test_no_key_omits_header(monkeypatch):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["x_api_key"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json=POLACK_S2_JSON)

    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    client = make_mock_s2_client(handler)
    try:
        await client.search_by_doi("10.1056/NEJMoa2034577")
    finally:
        await client.aclose()
    assert captured["x_api_key"] == ""
