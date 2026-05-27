"""End-to-end tests for verify_citation against mocked multi-DB backends."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from citation_mcp.server import _empty_canonical_from_input, verify_citation

from .conftest import (
    POLACK_ARXIV_FEED_EMPTY,
    POLACK_DOI,
    POLACK_OPENALEX_JSON,
    POLACK_PMID,
    POLACK_PUBMED_EFETCH_XML,
    POLACK_PUBMED_ESEARCH_JSON,
    POLACK_RAW_CROSSREF,
    POLACK_S2_JSON,
    POLACK_TITLE,
    make_mock_arxiv_client,
    make_mock_crossref_client,
    make_mock_openalex_client,
    make_mock_pubmed_client,
    make_mock_s2_client,
    polack_arxiv_handler,
    polack_crossref_handler,
    polack_openalex_handler,
    polack_pubmed_handler,
    polack_s2_handler,
    sample_arxiv_handler,
    works_list_response,
    works_response,
)


@pytest_asyncio.fixture
async def multi_db(monkeypatch):
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


def _call_kwargs(clients: dict):
    return {
        "crossref": clients["crossref"],
        "pubmed": clients["pubmed"],
        "openalex": clients["openalex"],
        "semantic_scholar": clients["semantic_scholar"],
        "arxiv": clients["arxiv"],
    }


# ---------------------------------------------------------------------------
# Fixture 1: DOI-only input
# ---------------------------------------------------------------------------


async def test_fixture_1_doi_only(multi_db, cache):
    result = await verify_citation({"doi": POLACK_DOI}, cache=cache, **_call_kwargs(multi_db))
    assert result["verified"] is True
    assert result["match_quality"] == "high"
    assert "crossref" in result["databases_confirmed"]
    assert "pubmed" in result["databases_confirmed"]
    assert "openalex" in result["databases_confirmed"]
    assert "semantic_scholar" in result["databases_confirmed"]
    # Phase 1.B.1: arXiv is opt-in and is NOT queried for DOI inputs.
    assert "arxiv" not in result["databases_queried"]
    assert "arxiv" not in result["databases_confirmed"]
    assert result["canonical"]["doi"] == POLACK_DOI
    assert result["canonical"]["pmid"] == POLACK_PMID
    # PubMed supplies the abstract; ensure Layer 3 promoted it.
    assert result["canonical"]["abstract"] is not None


# ---------------------------------------------------------------------------
# Fixture 2: clean metadata-only input
# ---------------------------------------------------------------------------


async def test_fixture_2_metadata_clean(multi_db, cache):
    input_citation = {
        "title": POLACK_TITLE,
        "authors": ["Polack, Fernando P."],
        "year": 2020,
    }
    result = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    assert result["verified"] is True
    assert result["match_quality"] in ("high", "medium")
    assert result["score_breakdown"]["title_sim"] == 1.0
    assert result["canonical"]["doi"] == POLACK_DOI


# ---------------------------------------------------------------------------
# Fixture 3: year off by 5+
# ---------------------------------------------------------------------------


async def test_fixture_3_wrong_year(multi_db, cache):
    input_citation = {
        "title": POLACK_TITLE,
        "authors": ["Polack, Fernando P."],
        "year": 2015,
    }
    result = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    assert result["verified"] is False
    assert result["match_quality"] == "none"
    assert result["score_breakdown"]["rejected_by"] == "year_off_by_more_than_one"


# ---------------------------------------------------------------------------
# Fixture 4: single-character title typo
# ---------------------------------------------------------------------------


async def test_fixture_4_title_typo(multi_db, cache):
    input_citation = {
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vacccine",  # extra 'c'
        "authors": ["Polack, Fernando P."],
        "year": 2020,
    }
    result = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    assert result["verified"] is True
    assert result["match_quality"] in ("medium", "high")
    assert result["score_breakdown"]["title_sim"] >= 0.95


# ---------------------------------------------------------------------------
# Fixture 5: first-author surname typo + exact title
# ---------------------------------------------------------------------------


async def test_fixture_5_first_author_typo(multi_db, cache):
    input_citation = {
        "title": POLACK_TITLE,
        "authors": ["Polak, Fernando P."],  # missing 'c'
        "year": 2020,
    }
    result = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    assert result["verified"] is True
    assert result["match_quality"] in ("medium", "high")
    assert result["score_breakdown"]["title_sim"] == 1.0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_requires_at_least_one_of_doi_pmid_title(multi_db, cache):
    result = await verify_citation({"year": 2020}, cache=cache, **_call_kwargs(multi_db))
    assert "error" in result
    assert result["error"] == "at_least_one_of_doi_pmid_title_required"


# ---------------------------------------------------------------------------
# Cache hit
# ---------------------------------------------------------------------------


async def test_cache_hit_returns_with_warning(multi_db, cache):
    input_citation = {"doi": POLACK_DOI}
    first = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    second = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    assert any(w.get("source") == "cache" for w in second["warnings"])
    assert first["match_quality"] == second["match_quality"]


# ---------------------------------------------------------------------------
# Phase 1.B new fixture: only Crossref has the record
# ---------------------------------------------------------------------------


async def test_single_db_confirmed_only_crossref(cache, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")

    def empty_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def empty_pubmed(request: httpx.Request) -> httpx.Response:
        # Return an empty PubmedArticleSet so neither esearch nor efetch hits work.
        if request.url.path.endswith("/esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, content=b"<PubmedArticleSet/>",
                              headers={"Content-Type": "text/xml"})

    crossref = make_mock_crossref_client(polack_crossref_handler)
    pubmed = make_mock_pubmed_client(empty_pubmed)
    openalex = make_mock_openalex_client(empty_404)
    s2 = make_mock_s2_client(empty_404)
    arxiv = make_mock_arxiv_client(polack_arxiv_handler, min_interval=0.0)
    try:
        result = await verify_citation(
            {"doi": POLACK_DOI}, cache=cache,
            crossref=crossref, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv,
        )
        assert result["verified"] is True
        assert result["databases_confirmed"] == ["crossref"]
        assert "pubmed" not in result["databases_failed"]
    finally:
        for c in (crossref, pubmed, openalex, s2, arxiv):
            await c.aclose()


# ---------------------------------------------------------------------------
# Phase 1.B new fixture: one DB disagrees on year → discrepancy
# ---------------------------------------------------------------------------


async def test_one_db_disagrees_year(cache, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")

    s2_wrong_year = dict(POLACK_S2_JSON)
    s2_wrong_year["year"] = 2025  # off by 5 from the real 2020

    def s2_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=s2_wrong_year)

    crossref = make_mock_crossref_client(polack_crossref_handler)
    pubmed = make_mock_pubmed_client(polack_pubmed_handler)
    openalex = make_mock_openalex_client(polack_openalex_handler)
    s2 = make_mock_s2_client(s2_handler)
    arxiv = make_mock_arxiv_client(polack_arxiv_handler, min_interval=0.0)
    try:
        result = await verify_citation(
            {"doi": POLACK_DOI}, cache=cache,
            crossref=crossref, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv,
        )
        # DOI input → Layer 1 short-circuits the year sanity guard, so
        # Semantic Scholar IS confirmed and the year disagreement surfaces
        # in the Layer 3 discrepancies list instead.
        assert "semantic_scholar" in result["databases_confirmed"]
        assert "crossref" in result["databases_confirmed"]
        # Canonical year is the earliest non-null.
        assert result["canonical"]["year"] == 2020
        year_discrepancies = [d for d in result["discrepancies"] if d["field"] == "year"]
        assert len(year_discrepancies) == 1
        assert year_discrepancies[0]["values"].get("semantic_scholar") == 2025
    finally:
        for c in (crossref, pubmed, openalex, s2, arxiv):
            await c.aclose()


# ---------------------------------------------------------------------------
# Phase 1.B new fixture: OpenAlex 500 → databases_failed includes openalex
# ---------------------------------------------------------------------------


async def test_one_db_errors_others_proceed(cache, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")

    def openalex_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    crossref = make_mock_crossref_client(polack_crossref_handler)
    pubmed = make_mock_pubmed_client(polack_pubmed_handler)
    openalex = make_mock_openalex_client(openalex_500)
    s2 = make_mock_s2_client(polack_s2_handler)
    arxiv = make_mock_arxiv_client(polack_arxiv_handler, min_interval=0.0)
    try:
        result = await verify_citation(
            {"doi": POLACK_DOI}, cache=cache,
            crossref=crossref, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv,
        )
        assert result["verified"] is True
        assert "openalex" in result["databases_failed"]
        # Other DBs still confirm.
        assert "crossref" in result["databases_confirmed"]
        assert "pubmed" in result["databases_confirmed"]
        assert any(w.get("source") == "openalex" for w in result["warnings"])
    finally:
        for c in (crossref, pubmed, openalex, s2, arxiv):
            await c.aclose()


# ---------------------------------------------------------------------------
# Phase 1.B new fixture: all DBs miss
# ---------------------------------------------------------------------------


async def test_all_dbs_miss(cache, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")

    def empty_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def empty_pubmed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, content=b"<PubmedArticleSet/>",
                              headers={"Content-Type": "text/xml"})

    crossref = make_mock_crossref_client(empty_404)
    pubmed = make_mock_pubmed_client(empty_pubmed)
    openalex = make_mock_openalex_client(empty_404)
    s2 = make_mock_s2_client(empty_404)
    arxiv = make_mock_arxiv_client(polack_arxiv_handler, min_interval=0.0)
    try:
        result = await verify_citation(
            {"doi": "10.1/never-existed"}, cache=cache,
            crossref=crossref, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv,
        )
        assert result["verified"] is False
        assert result["databases_confirmed"] == []
        assert result["match_quality"] == "none"
    finally:
        for c in (crossref, pubmed, openalex, s2, arxiv):
            await c.aclose()


# ---------------------------------------------------------------------------
# Phase 1.B new fixture: title-only input cap → match_quality capped at medium
# ---------------------------------------------------------------------------


async def test_title_only_input_caps_at_medium(multi_db, cache):
    input_citation = {"title": POLACK_TITLE}
    result = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    assert result["verified"] is True
    # The scorer's `capped_at_medium_insufficient_input_fields` flag should
    # have propagated into a warning on the result.
    assert result["match_quality"] == "medium"
    assert any(
        w.get("message") == "capped_at_medium_insufficient_input_fields"
        for w in result["warnings"]
    )


# ---------------------------------------------------------------------------
# Crossref-only error path (Phase 1.A compatibility)
# ---------------------------------------------------------------------------


async def test_crossref_only_500(cache):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")
    client = make_mock_crossref_client(handler)
    try:
        result = await verify_citation({"doi": "10.1/never-existed"}, client, cache)
        assert result["verified"] is False
        assert "crossref" in result["databases_failed"]
        assert any(w.get("source") == "crossref" for w in result["warnings"])
    finally:
        await client.aclose()


async def test_crossref_only_doi_404(cache):
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


# ---------------------------------------------------------------------------
# Phase 1.B.1: arXiv opt-in — only queried when arxiv_id is present
# ---------------------------------------------------------------------------


async def test_arxiv_id_input_queries_arxiv(cache, monkeypatch):
    """When input has arxiv_id, arXiv IS in databases_queried (and may confirm)."""
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def empty_pubmed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, content=b"<PubmedArticleSet/>",
                              headers={"Content-Type": "text/xml"})

    crossref = make_mock_crossref_client(empty_handler)
    pubmed = make_mock_pubmed_client(empty_pubmed)
    openalex = make_mock_openalex_client(empty_handler)
    s2 = make_mock_s2_client(empty_handler)
    arxiv = make_mock_arxiv_client(sample_arxiv_handler, min_interval=0.0)
    try:
        # Input fields match the ARXIV_SAMPLE_FEED entry.
        result = await verify_citation(
            {
                "arxiv_id": "2103.04567",
                "title": "Sample arXiv Paper About Transformers",
                "year": 2021,
            },
            cache=cache,
            crossref=crossref, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv,
        )
        assert "arxiv" in result["databases_queried"]
        assert "arxiv" in result["databases_confirmed"]
        assert result["verified"] is True
        assert result["canonical"]["arxiv_id"] == "2103.04567"
    finally:
        for c in (crossref, pubmed, openalex, s2, arxiv):
            await c.aclose()


async def test_doi_input_skips_arxiv(multi_db, cache):
    """Sanity: DOI input must NOT query arXiv at all."""
    result = await verify_citation({"doi": POLACK_DOI}, cache=cache, **_call_kwargs(multi_db))
    assert "arxiv" not in result["databases_queried"]
    assert "arxiv" not in result["databases_failed"]


async def test_arxiv_rate_limited_is_soft_warning(cache, monkeypatch):
    """When arXiv 429s after retries, surface a warning — NOT databases_failed."""
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    # Compress the 5s backoff for test speed.
    import citation_mcp.databases.arxiv as _ax
    monkeypatch.setattr(_ax, "_BASE_BACKOFF", 0.01)

    def empty_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def empty_pubmed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, content=b"<PubmedArticleSet/>",
                              headers={"Content-Type": "text/xml"})

    def always_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate-limited")

    crossref = make_mock_crossref_client(polack_crossref_handler)
    pubmed = make_mock_pubmed_client(empty_pubmed)
    openalex = make_mock_openalex_client(empty_404)
    s2 = make_mock_s2_client(empty_404)
    # Pass min_interval=0 so retries don't actually wait 3s.
    arxiv = make_mock_arxiv_client(always_429, min_interval=0.0)
    try:
        # Provide enough metadata that Crossref confirms via Layer 2.
        result = await verify_citation(
            {
                "arxiv_id": "2103.04567",
                "title": POLACK_TITLE,
                "authors": ["Polack, Fernando P."],
                "year": 2020,
                "journal": "New England Journal of Medicine",
            },
            cache=cache,
            crossref=crossref, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv,
        )
        # Crossref still confirmed, so verified=True.
        assert result["verified"] is True
        assert "arxiv" not in result["databases_failed"]
        assert any(
            w.get("source") == "arxiv" and w.get("level") == "warning"
            and "rate-limited" in w.get("message", "")
            for w in result["warnings"]
        )
    finally:
        for c in (crossref, pubmed, openalex, s2, arxiv):
            await c.aclose()


# ---------------------------------------------------------------------------
# v0.3.5: no-match canonical fallback routes string authors through
# parse_author_string so NLM-form inputs ("Polack FP") yield a structured
# {family, given} instead of dumping the whole string into family.
# ---------------------------------------------------------------------------


def test_empty_canonical_from_input_parses_nlm_string_authors():
    canonical = _empty_canonical_from_input(
        {"title": "Untitled", "authors": ["Polack FP", "Garcia M"]}
    )
    assert canonical["authors"][0] == {"family": "Polack", "given": "FP"}
    assert canonical["authors"][1] == {"family": "Garcia", "given": "M"}


def test_empty_canonical_from_input_preserves_dict_authors():
    canonical = _empty_canonical_from_input(
        {"title": "Untitled", "authors": [{"family": "Polack", "given": "Fernando P."}]}
    )
    assert canonical["authors"][0] == {"family": "Polack", "given": "Fernando P."}


def test_empty_canonical_from_input_parses_western_string_authors():
    canonical = _empty_canonical_from_input(
        {"title": "Untitled", "authors": ["Fernando Polack"]}
    )
    assert canonical["authors"][0] == {"family": "Polack", "given": "Fernando"}


# ---------------------------------------------------------------------------
# v0.3.5.D: force_refresh bypasses the read-cache but still writes on the way
# out, so subsequent normal calls hit the refreshed entry.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def counted_multi_db(monkeypatch):
    """Same as multi_db but each DB's handler tracks invocation count."""
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


async def test_force_refresh_bypasses_cache(counted_multi_db, cache):
    multi_db, counts = counted_multi_db
    input_citation = {"doi": POLACK_DOI}
    first = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    crossref_after_first = counts["crossref"]
    assert crossref_after_first > 0
    second = await verify_citation(
        input_citation, cache=cache, force_refresh=True, **_call_kwargs(multi_db)
    )
    # No cache warning on the forced call.
    assert not any(w.get("source") == "cache" for w in second["warnings"])
    # DB was re-queried — crossref handler invoked again.
    assert counts["crossref"] > crossref_after_first
    # Result quality unchanged because the upstream DBs return the same record.
    assert first["match_quality"] == second["match_quality"]


async def test_force_refresh_writes_fresh_to_cache(counted_multi_db, cache):
    multi_db, counts = counted_multi_db
    input_citation = {"doi": POLACK_DOI}
    # Call 1: populates cache.
    await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    crossref_after_first = counts["crossref"]
    # Call 2: force_refresh — bypass read, write fresh.
    second = await verify_citation(
        input_citation, cache=cache, force_refresh=True, **_call_kwargs(multi_db)
    )
    crossref_after_second = counts["crossref"]
    assert not any(w.get("source") == "cache" for w in second["warnings"])
    assert crossref_after_second > crossref_after_first
    # Call 3: default force_refresh=False — should hit the refreshed cache.
    third = await verify_citation(input_citation, cache=cache, **_call_kwargs(multi_db))
    assert any(w.get("source") == "cache" for w in third["warnings"])
    # Crossref handler NOT invoked on the cached third call.
    assert counts["crossref"] == crossref_after_second
