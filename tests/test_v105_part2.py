"""v1.0.5 part 2 (CM-108D) fail-first tests.

U1  arXiv checks the deadline before waiting on its spacing
U2  no backoff / Retry-After wait is started if it would end after the deadline, in every
    retrying client; no further request is sent
U3  default bulk budget is 210 s
U4  resolveIdentifier parity: degraded-result TTL, failure containment, CancelledError
U5  email= (and mailto=) values are redacted like api_key= in error and warning text

Deadlines here run on a frozen clock (``lambda: 0.0``) or on the real clock with waits
under 0.1 s, so no test sleeps for real over 0.1 s on the new code.
"""

from __future__ import annotations

import asyncio
import types

import httpx
import pytest

from tests.conftest import (
    POLACK_DOI,
    POLACK_PMID,
    make_mock_crossref_client,
    make_mock_openalex_client,
    make_mock_pubmed_client,
    make_mock_s2_client,
    polack_crossref_handler,
    polack_openalex_handler,
    polack_pubmed_handler,
    polack_s2_handler,
    sample_arxiv_handler,
)

DAY = 24 * 60 * 60
FROZEN = lambda: 0.0  # noqa: E731 — the deadline clock never advances


def _counting(handler, first_status=None, headers=None):
    state = {"n": 0}

    def h(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if first_status is not None and state["n"] == 1:
            return httpx.Response(first_status, headers=headers or {}, text="slow down")
        return handler(request)

    return h, state


# ---------------------------------------------------------------------------
# U1: arXiv spacing vs the deadline
# ---------------------------------------------------------------------------


async def test_u1_arxiv_spacing_past_deadline_is_skipped_without_a_request():
    from citation_mcp import pacing
    from citation_mcp.databases.arxiv import ArxivClient

    h, state = _counting(sample_arxiv_handler)
    client = ArxivClient(transport=httpx.MockTransport(h), min_interval=0.08)
    try:
        await client.search_by_arxiv_id("2103.04567")  # sets the spacing clock
        assert state["n"] == 1
        # The next request may start only 0.08 s from now; the deadline is 0.02 s away.
        with pacing.budget(0.02):
            with pytest.raises(pacing.RateBudgetExhausted):
                await client.search_by_arxiv_id("2103.04567")
    finally:
        await client.aclose()
    assert state["n"] == 1, "no request was sent"


async def test_u1_arxiv_skip_is_reported_like_c4(cache, monkeypatch):
    from citation_mcp import pacing
    from citation_mcp.databases.arxiv import ArxivClient
    from citation_mcp.server import verify_citation

    monkeypatch.setenv("OPENALEX_API_KEY", "test-openalex-key")
    h, state = _counting(sample_arxiv_handler)
    arxiv = ArxivClient(transport=httpx.MockTransport(h), min_interval=0.08)
    crossref = make_mock_crossref_client(polack_crossref_handler)
    try:
        await arxiv.search_by_arxiv_id("2103.04567")
        with pacing.budget(0.02):
            result = await verify_citation({"arxiv_id": "2103.04567", "title": "Some arXiv paper"},
                                           crossref, cache, arxiv=arxiv)
    finally:
        await arxiv.aclose()
        await crossref.aclose()
    assert "arxiv" in result["databases_failed"]
    assert {"source": "arxiv", "level": "warning",
            "message": "arxiv skipped: rate budget exhausted"} in result["warnings"]
    assert state["n"] == 1


# ---------------------------------------------------------------------------
# U2: backoff waits vs the deadline, every retrying client
# ---------------------------------------------------------------------------

# (module, constructor, handler, call). Backoff is made small (~0.05 s) so that the
# unchanged code, which does sleep, stays fast; the frozen deadline clock still says the
# wait would end after a deadline 0.01 s away.


def _crossref():
    from citation_mcp.databases import crossref as mod
    return mod, lambda t: mod.CrossrefClient(transport=t, query_pacer=None, single_pacer=None), \
        polack_crossref_handler, lambda c: c.search_by_doi(POLACK_DOI)


def _pubmed():
    from citation_mcp.databases import pubmed as mod
    return mod, lambda t: mod.PubMedClient(transport=t, pacer=None), \
        polack_pubmed_handler, lambda c: c.search_by_pmid(POLACK_PMID)


def _s2():
    from citation_mcp.databases import semantic_scholar as mod
    return mod, lambda t: mod.SemanticScholarClient(transport=t, pacer=None), \
        polack_s2_handler, lambda c: c.search_by_doi(POLACK_DOI)


def _openalex():
    from citation_mcp.databases import openalex as mod
    return mod, lambda t: mod.OpenAlexClient(transport=t), \
        polack_openalex_handler, lambda c: c.search_by_doi(POLACK_DOI)


def _arxiv():
    from citation_mcp.databases import arxiv as mod
    return mod, lambda t: mod.ArxivClient(transport=t, min_interval=0.0), \
        sample_arxiv_handler, lambda c: c.search_by_arxiv_id("2103.04567")


@pytest.mark.parametrize("make", [_crossref, _pubmed, _s2, _openalex, _arxiv],
                         ids=["crossref", "pubmed", "semantic_scholar", "openalex", "arxiv"])
@pytest.mark.parametrize("status", [429, 503])
async def test_u2_backoff_crossing_deadline_is_skipped(make, status, monkeypatch):
    from citation_mcp import pacing

    monkeypatch.setenv("OPENALEX_API_KEY", "test-openalex-key")
    monkeypatch.setenv("NCBI_API_KEY", "test-ncbi-key")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    mod, build, handler, call = make()
    monkeypatch.setattr(mod, "_BASE_BACKOFF", 0.05)
    monkeypatch.setattr(mod, "random", types.SimpleNamespace(uniform=lambda a, b: 0.0))
    h, state = _counting(handler, first_status=status)
    client = build(httpx.MockTransport(h))
    try:
        with pacing.budget(0.01, clock=FROZEN):
            with pytest.raises(pacing.RateBudgetExhausted):
                await call(client)
    finally:
        await client.aclose()
    assert state["n"] == 1, "no request after a wait that would cross the deadline"


async def test_u2_retry_after_crossing_deadline_is_skipped(monkeypatch):
    """Retry-After is honoured as before, but not waited on past the deadline."""
    from citation_mcp import pacing
    from citation_mcp.databases import semantic_scholar as mod

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    monkeypatch.setattr(mod, "_BASE_BACKOFF", 0.0)
    monkeypatch.setattr(mod, "_BACKOFF_CAP", 0.05)  # unchanged code caps Retry-After at 0.05 s
    h, state = _counting(polack_s2_handler, first_status=429, headers={"Retry-After": "5"})
    client = mod.SemanticScholarClient(transport=httpx.MockTransport(h), pacer=None)
    try:
        with pacing.budget(0.01, clock=FROZEN):
            with pytest.raises(pacing.RateBudgetExhausted):
                await client.search_by_doi(POLACK_DOI)
    finally:
        await client.aclose()
    assert state["n"] == 1


async def test_u2_backoff_within_deadline_still_retries(monkeypatch):
    """Preservation: a backoff that fits the deadline is taken and the retry succeeds."""
    from citation_mcp import pacing
    from citation_mcp.databases import crossref as mod

    monkeypatch.setattr(mod, "_BASE_BACKOFF", 0.0)
    h, state = _counting(polack_crossref_handler, first_status=429)
    client = mod.CrossrefClient(transport=httpx.MockTransport(h), query_pacer=None, single_pacer=None)
    try:
        with pacing.budget(60.0, clock=FROZEN):
            rec = await client.search_by_doi(POLACK_DOI)
    finally:
        await client.aclose()
    assert rec is not None and state["n"] == 2


async def test_u2_verify_reports_backoff_skip_and_returns(cache, polack_multi_db, monkeypatch):
    from citation_mcp import pacing
    from citation_mcp.databases import crossref as mod
    from citation_mcp.server import verify_citation

    monkeypatch.setattr(mod, "_BASE_BACKOFF", 0.05)
    monkeypatch.setattr(mod, "random", types.SimpleNamespace(uniform=lambda a, b: 0.0))
    h, state = _counting(polack_crossref_handler, first_status=429)
    crossref = make_mock_crossref_client(h)
    c = polack_multi_db
    try:
        with pacing.budget(0.01, clock=FROZEN):
            result = await verify_citation(
                {"doi": POLACK_DOI}, crossref, cache, pubmed=c["pubmed"], openalex=c["openalex"],
                semantic_scholar=c["semantic_scholar"], arxiv=c["arxiv"])
    finally:
        await crossref.aclose()
    assert {"source": "crossref", "level": "warning",
            "message": "crossref skipped: rate budget exhausted"} in result["warnings"]
    assert "crossref" in result["databases_failed"]
    assert result["verified"] is True
    assert state["n"] == 1


# ---------------------------------------------------------------------------
# U3: default budget
# ---------------------------------------------------------------------------


def test_u3_default_bulk_budget_is_210():
    from citation_mcp.config import RateConfig, load_rate_config

    assert load_rate_config({}).bulk_budget_seconds == 210.0
    assert RateConfig().bulk_budget_seconds == 210.0


# ---------------------------------------------------------------------------
# U4: resolveIdentifier parity
# ---------------------------------------------------------------------------


def _spy_ttl(cache):
    seen: list[int] = []
    real_set = cache.set

    async def spy(key, value, type_=None, ttl_seconds=None, **kw):
        seen.append(ttl_seconds)
        return await real_set(key, value, type_=type_, ttl_seconds=ttl_seconds, **kw)

    cache.set = spy
    return seen


async def _resolve(cache, polack_multi_db, **override):
    from citation_mcp.server import resolve_identifier

    c = {**polack_multi_db, **override}
    return await resolve_identifier(POLACK_DOI, "doi", None, cache, c["crossref"],
                                    pubmed=c["pubmed"], openalex=c["openalex"],
                                    semantic_scholar=c["semantic_scholar"], arxiv=c["arxiv"])


async def test_u4_resolve_clean_result_is_cached_14_days(cache, polack_multi_db):
    seen = _spy_ttl(cache)
    out = await _resolve(cache, polack_multi_db)
    assert out["databases_failed"] == []
    assert seen == [14 * DAY]


async def test_u4_resolve_degraded_result_is_cached_24_hours(cache, polack_multi_db, monkeypatch):
    from citation_mcp.databases import semantic_scholar as s2mod

    monkeypatch.setattr(s2mod, "_BASE_BACKOFF", 0.0)
    s2_down = make_mock_s2_client(lambda r: httpx.Response(500, text="down"))
    seen = _spy_ttl(cache)
    try:
        out = await _resolve(cache, polack_multi_db, semantic_scholar=s2_down)
    finally:
        await s2_down.aclose()
    assert out["databases_failed"] == ["semantic_scholar"]
    assert seen == [DAY]


async def test_u4_resolve_value_error_is_contained(cache, polack_multi_db):
    def boom(request):
        raise ValueError("unexpected payload")

    crossref = make_mock_crossref_client(boom)
    try:
        out = await _resolve(cache, polack_multi_db, crossref=crossref)
    finally:
        await crossref.aclose()
    assert "crossref" in out["databases_failed"]
    assert out["resolved"]["pmid"] is not None  # the other databases still answered


async def test_u4_resolve_cancelled_error_propagates(cache, polack_multi_db):
    def cancel(request):
        raise asyncio.CancelledError()

    crossref = make_mock_crossref_client(cancel)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _resolve(cache, polack_multi_db, crossref=crossref)
    finally:
        await crossref.aclose()


# ---------------------------------------------------------------------------
# U5: email redaction
# ---------------------------------------------------------------------------

TEST_EMAIL = "redaction.test@example.org"


def test_u5_redaction_blanks_email_and_mailto_like_api_key():
    from citation_mcp.log_redaction import redact_query_string_in_text

    text = ("Client error '429 ' for url 'https://eutils.example/esearch.fcgi?tool=t"
            "&email=redaction.test%40example.org&api_key=SECRET123&db=pubmed'"
            " and https://x.example/w?mailto=redaction.test@example.org&rows=5")
    out = redact_query_string_in_text(text)
    assert "SECRET123" not in out and "api_key=***" in out
    assert "redaction.test" not in out
    assert "email=***" in out and "mailto=***" in out
    assert "db=pubmed" in out and "rows=5" in out


async def test_u5_pubmed_error_warning_shows_neither_key_nor_email(cache, monkeypatch):
    from citation_mcp.databases import pubmed as pmmod
    from citation_mcp.server import verify_citation

    monkeypatch.setenv("NCBI_API_KEY", "SECRET-NCBI-KEY")
    monkeypatch.setenv("CROSSREF_POLITE_EMAIL", TEST_EMAIL)
    monkeypatch.setattr(pmmod, "_BASE_BACKOFF", 0.0)
    pubmed = make_mock_pubmed_client(lambda r: httpx.Response(500, text="down"))
    crossref = make_mock_crossref_client(polack_crossref_handler)
    try:
        result = await verify_citation({"doi": POLACK_DOI}, crossref, cache, pubmed=pubmed)
    finally:
        await pubmed.aclose()
        await crossref.aclose()
    msgs = [w["message"] for w in result["warnings"] if w.get("source") == "pubmed"]
    assert msgs and "eutils" in msgs[0], msgs  # the warning does carry the URL
    for m in msgs:
        assert "SECRET-NCBI-KEY" not in m
        assert "redaction.test" not in m
