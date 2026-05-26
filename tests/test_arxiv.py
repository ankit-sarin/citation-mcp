"""Tests for the arXiv client."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest_asyncio

from citation_mcp.databases.arxiv import ArxivClient

from .conftest import ARXIV_SAMPLE_FEED, POLACK_ARXIV_FEED_EMPTY, make_mock_arxiv_client


def _sample_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=ARXIV_SAMPLE_FEED,
                          headers={"Content-Type": "application/atom+xml"})


def _empty_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=POLACK_ARXIV_FEED_EMPTY,
                          headers={"Content-Type": "application/atom+xml"})


@pytest_asyncio.fixture
async def arxiv_client():
    client = make_mock_arxiv_client(_sample_handler, min_interval=0.0)
    try:
        yield client
    finally:
        await client.aclose()


async def test_search_by_arxiv_id(arxiv_client: ArxivClient):
    rec = await arxiv_client.search_by_arxiv_id("2103.04567")
    assert rec is not None
    assert rec["arxiv_id"] == "2103.04567"
    assert rec["title"] == "Sample arXiv Paper About Transformers"
    assert rec["year"] == 2021
    assert rec["doi"] == "10.9999/test.arxiv"
    assert rec["journal"].startswith("Phys. Rev. D")
    assert rec["type"] == "journal-article"
    assert rec["source"] == "arxiv"
    assert rec["authors"][0]["family"] == "Doe"


async def test_arxiv_id_version_stripped(arxiv_client: ArxivClient):
    rec = await arxiv_client.search_by_arxiv_id("2103.04567v3")
    assert rec is not None
    assert rec["arxiv_id"] == "2103.04567"


async def test_search_by_doi(arxiv_client: ArxivClient):
    rec = await arxiv_client.search_by_doi("10.9999/test.arxiv")
    assert rec is not None
    assert rec["doi"] == "10.9999/test.arxiv"


async def test_search_by_metadata(arxiv_client: ArxivClient):
    results = await arxiv_client.search_by_metadata(
        title="Transformers", author="Doe", year=2021, rows=5,
    )
    assert len(results) == 1
    assert results[0]["arxiv_id"] == "2103.04567"


async def test_empty_feed_returns_no_results():
    client = make_mock_arxiv_client(_empty_handler, min_interval=0.0)
    try:
        assert await client.search_by_arxiv_id("0000.00000") is None
        assert await client.search_by_doi("10.9999/none") is None
        assert await client.search_by_metadata("title", rows=5) == []
    finally:
        await client.aclose()


async def test_rate_limit_enforces_min_spacing():
    """Two back-to-back requests must be at least min_interval apart."""
    client = make_mock_arxiv_client(_sample_handler, min_interval=0.5)
    try:
        start = time.monotonic()
        await client.search_by_arxiv_id("2103.04567")
        await client.search_by_arxiv_id("2103.04567")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.5
    finally:
        await client.aclose()


async def test_no_spacing_for_first_request():
    """The very first request after construction does not wait."""
    client = make_mock_arxiv_client(_sample_handler, min_interval=3.0)
    try:
        start = time.monotonic()
        await client.search_by_arxiv_id("2103.04567")
        elapsed = time.monotonic() - start
        # Should be well under the 3-second interval (single request, no prior).
        assert elapsed < 1.0
    finally:
        await client.aclose()
