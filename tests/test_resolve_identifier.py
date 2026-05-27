"""Tests for the resolveIdentifier tool handler."""

from __future__ import annotations

import httpx
import pytest_asyncio

from citation_mcp.server import resolve_identifier

from .conftest import (
    POLACK_DOI,
    POLACK_OPENALEX_ID,
    POLACK_PMID,
    POLACK_S2_ID,
    make_mock_crossref_client,
    make_mock_openalex_client,
    make_mock_pubmed_client,
    make_mock_s2_client,
    make_mock_arxiv_client,
    polack_arxiv_handler,
    polack_crossref_handler,
    polack_openalex_handler,
    polack_pubmed_handler,
    polack_s2_handler,
    sample_arxiv_handler,
)


@pytest_asyncio.fixture
async def all_clients(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    clients = {
        "crossref": make_mock_crossref_client(polack_crossref_handler),
        "pubmed": make_mock_pubmed_client(polack_pubmed_handler),
        "openalex": make_mock_openalex_client(polack_openalex_handler),
        "semantic_scholar": make_mock_s2_client(polack_s2_handler),
        "arxiv": make_mock_arxiv_client(polack_arxiv_handler, min_interval=0.0),
    }
    try:
        yield clients
    finally:
        for c in clients.values():
            await c.aclose()


async def test_resolve_from_doi(all_clients, cache):
    result = await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None,
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=all_clients["pubmed"],
        openalex=all_clients["openalex"],
        semantic_scholar=all_clients["semantic_scholar"],
        arxiv=all_clients["arxiv"],
    )
    assert result["resolved"]["doi"] == POLACK_DOI
    assert result["resolved"]["pmid"] == POLACK_PMID
    assert result["resolved"]["openalex"] == POLACK_OPENALEX_ID
    assert result["resolved"]["semantic_scholar"] == POLACK_S2_ID


async def test_resolve_from_pmid(all_clients, cache):
    result = await resolve_identifier(
        identifier=POLACK_PMID, from_type="pmid", to_types=None,
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=all_clients["pubmed"],
        openalex=all_clients["openalex"],
        semantic_scholar=all_clients["semantic_scholar"],
        arxiv=all_clients["arxiv"],
    )
    assert result["resolved"]["pmid"] == POLACK_PMID
    assert result["resolved"]["doi"] == POLACK_DOI
    assert result["resolved"]["openalex"] == POLACK_OPENALEX_ID


async def test_resolve_from_openalex(all_clients, cache):
    result = await resolve_identifier(
        identifier=POLACK_OPENALEX_ID, from_type="openalex", to_types=None,
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=all_clients["pubmed"],
        openalex=all_clients["openalex"],
        semantic_scholar=all_clients["semantic_scholar"],
        arxiv=all_clients["arxiv"],
    )
    assert result["resolved"]["openalex"] == POLACK_OPENALEX_ID
    assert result["resolved"]["doi"] == POLACK_DOI


async def test_filter_by_to_types(all_clients, cache):
    result = await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=["doi", "pmid"],
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=all_clients["pubmed"],
        openalex=all_clients["openalex"],
        semantic_scholar=all_clients["semantic_scholar"],
        arxiv=all_clients["arxiv"],
    )
    assert set(result["resolved"].keys()) == {"doi", "pmid"}
    assert result["resolved"]["doi"] == POLACK_DOI
    assert result["resolved"]["pmid"] == POLACK_PMID


async def test_invalid_from_type_errors(all_clients, cache):
    result = await resolve_identifier(
        identifier="anything", from_type="nope", to_types=None,
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=all_clients["pubmed"],
        openalex=all_clients["openalex"],
        semantic_scholar=all_clients["semantic_scholar"],
        arxiv=all_clients["arxiv"],
    )
    assert result["error"] == "invalid_from_type"


async def test_resolve_from_arxiv(cache, monkeypatch):
    """from_type='arxiv' DOES exercise the arXiv client and returns its IDs."""
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    crossref = make_mock_crossref_client(polack_crossref_handler)
    pubmed = make_mock_pubmed_client(polack_pubmed_handler)
    openalex = make_mock_openalex_client(polack_openalex_handler)
    s2 = make_mock_s2_client(polack_s2_handler)
    # Use the sample arXiv feed so the lookup actually returns a record (with DOI).
    arxiv = make_mock_arxiv_client(sample_arxiv_handler, min_interval=0.0)
    try:
        result = await resolve_identifier(
            identifier="2103.04567", from_type="arxiv", to_types=None,
            cache=cache,
            crossref=crossref, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv,
        )
        # arXiv supplies its own ID + the DOI from arxiv:doi field.
        assert result["resolved"]["arxiv"] == "2103.04567"
        assert result["resolved"]["doi"] == "10.9999/test.arxiv"
        assert "arxiv" in result["databases_queried"]
    finally:
        for c in (crossref, pubmed, openalex, s2, arxiv):
            await c.aclose()


async def test_doi_resolve_does_not_query_arxiv(all_clients, cache):
    """from_type='doi' must NOT query arXiv (Phase 1.B.1 opt-in)."""
    result = await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None,
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=all_clients["pubmed"],
        openalex=all_clients["openalex"],
        semantic_scholar=all_clients["semantic_scholar"],
        arxiv=all_clients["arxiv"],
    )
    assert "arxiv" not in result["databases_queried"]


async def test_cache_hit_second_call(all_clients, cache):
    await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None,
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=all_clients["pubmed"],
        openalex=all_clients["openalex"],
        semantic_scholar=all_clients["semantic_scholar"],
        arxiv=all_clients["arxiv"],
    )
    # Drop all clients so a second call must come from cache.
    second = await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None,
        cache=cache,
        crossref=all_clients["crossref"],
        pubmed=None, openalex=None, semantic_scholar=None, arxiv=None,
    )
    assert second["resolved"]["pmid"] == POLACK_PMID


# ---------------------------------------------------------------------------
# v0.3.5.D: force_refresh bypasses the read-cache but still writes on the way
# out so subsequent normal calls hit the refreshed entry.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def counted_clients(monkeypatch):
    """all_clients variant whose handlers track per-DB invocation counts."""
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    counts = {"crossref": 0, "pubmed": 0, "openalex": 0, "semantic_scholar": 0}

    def _wrap(name: str, base):
        def _handler(request: httpx.Request) -> httpx.Response:
            counts[name] += 1
            return base(request)
        return _handler

    clients = {
        "crossref": make_mock_crossref_client(_wrap("crossref", polack_crossref_handler)),
        "pubmed": make_mock_pubmed_client(_wrap("pubmed", polack_pubmed_handler)),
        "openalex": make_mock_openalex_client(_wrap("openalex", polack_openalex_handler)),
        "semantic_scholar": make_mock_s2_client(_wrap("semantic_scholar", polack_s2_handler)),
        "arxiv": make_mock_arxiv_client(polack_arxiv_handler, min_interval=0.0),
    }
    try:
        yield clients, counts
    finally:
        for c in clients.values():
            await c.aclose()


def _resolve_kwargs(clients: dict):
    return {
        "cache": None,  # overridden by caller
        "crossref": clients["crossref"],
        "pubmed": clients["pubmed"],
        "openalex": clients["openalex"],
        "semantic_scholar": clients["semantic_scholar"],
        "arxiv": clients["arxiv"],
    }


async def test_resolve_force_refresh_bypasses_cache(counted_clients, cache):
    clients, counts = counted_clients
    kwargs = _resolve_kwargs(clients)
    kwargs["cache"] = cache
    # First call populates the cache.
    await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None, **kwargs
    )
    crossref_after_first = counts["crossref"]
    assert crossref_after_first > 0
    # Forced refresh — DB clients must be re-invoked.
    await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None,
        force_refresh=True, **kwargs
    )
    assert counts["crossref"] > crossref_after_first


async def test_resolve_force_refresh_writes_fresh_to_cache(counted_clients, cache):
    clients, counts = counted_clients
    kwargs = _resolve_kwargs(clients)
    kwargs["cache"] = cache
    # Call 1: populate.
    await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None, **kwargs
    )
    crossref_after_first = counts["crossref"]
    # Call 2: forced refresh — bypass read, write fresh.
    await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None,
        force_refresh=True, **kwargs
    )
    crossref_after_second = counts["crossref"]
    assert crossref_after_second > crossref_after_first
    # Call 3: default force_refresh=False — must hit refreshed cache; no new DB hits.
    third = await resolve_identifier(
        identifier=POLACK_DOI, from_type="doi", to_types=None, **kwargs
    )
    assert third["resolved"]["pmid"] == POLACK_PMID
    assert counts["crossref"] == crossref_after_second
