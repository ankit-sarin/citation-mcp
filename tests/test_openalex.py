"""Tests for the OpenAlex client."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from citation_mcp.databases.openalex import (
    OpenAlexClient,
    reconstruct_abstract_from_inverted_index,
)

from .conftest import (
    POLACK_OPENALEX_ID,
    POLACK_OPENALEX_JSON,
    POLACK_TITLE,
    make_mock_openalex_client,
    polack_openalex_handler,
)


@pytest_asyncio.fixture
async def openalex_client(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "test-key")
    client = make_mock_openalex_client(polack_openalex_handler)
    try:
        yield client
    finally:
        await client.aclose()


async def test_search_by_doi(openalex_client: OpenAlexClient):
    rec = await openalex_client.search_by_doi("10.1056/NEJMoa2034577")
    assert rec is not None
    assert rec["doi"] == "10.1056/nejmoa2034577"
    assert rec["openalex_id"] == POLACK_OPENALEX_ID
    assert rec["title"] == POLACK_TITLE
    assert rec["year"] == 2020
    assert rec["journal"] == "New England Journal of Medicine"
    assert rec["pmid"] == "33301246"
    assert rec["pages"] == "2603-2615"
    assert rec["source"] == "openalex"
    assert rec["citation_count"] == 12000


async def test_search_by_work_id(openalex_client: OpenAlexClient):
    rec = await openalex_client.search_by_work_id(POLACK_OPENALEX_ID)
    assert rec is not None
    assert rec["openalex_id"] == POLACK_OPENALEX_ID


async def test_search_by_metadata(openalex_client: OpenAlexClient):
    results = await openalex_client.search_by_metadata(
        title=POLACK_TITLE, year=2020, rows=5,
    )
    assert len(results) == 1
    assert results[0]["doi"] == "10.1056/nejmoa2034577"


async def test_api_key_query_param_included(monkeypatch):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url.query.decode("utf-8") if isinstance(request.url.query, bytes) else request.url.query)
        return httpx.Response(200, json=POLACK_OPENALEX_JSON)

    monkeypatch.setenv("OPENALEX_API_KEY", "super-secret-key")
    client = make_mock_openalex_client(handler)
    try:
        await client.search_by_doi("10.1056/NEJMoa2034577")
    finally:
        await client.aclose()
    assert "api_key=super-secret-key" in captured["query"]


async def test_client_disabled_when_no_key(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    client = make_mock_openalex_client(lambda req: httpx.Response(200, json={}))
    try:
        assert client.enabled is False
        assert await client.search_by_doi("10.1056/anything") is None
        assert await client.search_by_metadata("x", rows=1) == []
    finally:
        await client.aclose()


def test_inverted_index_reconstruction():
    inv = {"hello": [0], "world": [1], "again": [2]}
    assert reconstruct_abstract_from_inverted_index(inv) == "hello world again"


def test_inverted_index_handles_repeats():
    inv = {"the": [0, 2], "cat": [1], "ran": [3]}
    assert reconstruct_abstract_from_inverted_index(inv) == "the cat the ran"


def test_inverted_index_empty_returns_none():
    assert reconstruct_abstract_from_inverted_index(None) is None
    assert reconstruct_abstract_from_inverted_index({}) is None
