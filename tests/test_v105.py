"""v1.0.5 (CM-108C) fail-first tests: per-database pacing, time budget,
failure containment and degraded-result TTL.

T1–T3  RatePacer primitive (virtual clock, no real sleeps)
T4–T5  where pacing applies in the Crossref, PubMed and Semantic Scholar clients
T6     bulk-call time budget: skipped databases, no exception
T7–T8  failure containment in _fetch_from_db (ValueError contained, CancelledError propagates)
T9     degraded-result TTL and cache-key version
T10    configuration from the environment
T11    output-shape preservation (key sets captured from v1.0.4 at 0ca96b3)

New-API imports are made inside each test so that, on the unchanged code, each test
fails on its own rather than the whole module failing at collection.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from tests._virtual_time import VirtualTime
from tests.conftest import (
    POLACK_DOI,
    POLACK_PMID,
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
)

DAY = 24 * 60 * 60


class CountingPacer:
    """Stand-in pacer: counts acquires, always grants."""

    def __init__(self) -> None:
        self.calls = 0
        self.deadlines: list[float | None] = []

    async def acquire(self, deadline: float | None = None) -> bool:
        self.calls += 1
        self.deadlines.append(deadline)
        return True


def _first_429_then(handler):
    state = {"n": 0}

    def h(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, text="slow down")
        return handler(request)

    return h, state


# ---------------------------------------------------------------------------
# T1–T3: RatePacer
# ---------------------------------------------------------------------------


async def test_t1_concurrent_acquirers_are_spaced_fifo_without_burst():
    from citation_mcp.pacing import RatePacer

    vt = VirtualTime(start=100.0)
    pacer = RatePacer(0.7, clock=vt.clock, sleep=vt.sleep)
    grants: list[tuple[int, float]] = []

    async def one(i: int) -> None:
        assert await pacer.acquire() is True
        grants.append((i, vt.now))

    await vt.run(*[one(i) for i in range(10)])

    assert [i for i, _ in grants] == list(range(10)), "FIFO order"
    times = [t for _, t in grants]
    assert times[0] == pytest.approx(100.0), "first acquirer starts immediately"
    assert times[1] > times[0], "no burst: the second acquirer waits"
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 1 / 0.7 - 1e-9 for g in gaps), gaps


@pytest.mark.parametrize("rate", [0.7, 2.5])
async def test_t2_fractional_rates_give_expected_spacing(rate):
    from citation_mcp.pacing import RatePacer

    vt = VirtualTime()
    pacer = RatePacer(rate, clock=vt.clock, sleep=vt.sleep)
    times: list[float] = []

    async def one() -> None:
        await pacer.acquire()
        times.append(vt.now)

    await vt.run(*[one() for _ in range(6)])
    expected = [k / rate for k in range(6)]
    assert times == pytest.approx(expected)


async def test_t3_deadline_returns_false_without_sleeping_past_it():
    from citation_mcp.pacing import RatePacer

    vt = VirtualTime()
    pacer = RatePacer(1.0, clock=vt.clock, sleep=vt.sleep)

    async def scenario():
        assert await pacer.acquire(deadline=0.5) is True  # fits: starts at 0
        assert vt.now == 0.0
        assert await pacer.acquire(deadline=0.5) is False  # next slot is 1.0 > 0.5
        assert vt.now == 0.0, "a refused acquire must not sleep"
        assert await pacer.acquire(deadline=1.0) is True  # slot 1.0 fits exactly
        assert vt.now == pytest.approx(1.0), "the refusal did not consume a slot"

    await vt.run(scenario())
    assert all(s <= 1.0 + 1e-9 for s in vt.sleeps)


# ---------------------------------------------------------------------------
# T4–T5: where pacing applies
# ---------------------------------------------------------------------------


async def test_t4_crossref_list_and_single_use_separate_pacers():
    from citation_mcp.databases.crossref import CrossrefClient

    q, s = CountingPacer(), CountingPacer()
    client = CrossrefClient(transport=httpx.MockTransport(polack_crossref_handler),
                            query_pacer=q, single_pacer=s)
    try:
        await client.search_by_doi(POLACK_DOI)
        assert (q.calls, s.calls) == (0, 1)
        await client.search_by_metadata(title=POLACK_TITLE)
        assert (q.calls, s.calls) == (1, 1)
    finally:
        await client.aclose()


async def test_t5_crossref_retry_acquires_pacer_again(monkeypatch):
    from citation_mcp.databases import crossref as mod

    monkeypatch.setattr(mod, "_BASE_BACKOFF", 0.0)
    h, state = _first_429_then(polack_crossref_handler)
    single = CountingPacer()
    client = mod.CrossrefClient(transport=httpx.MockTransport(h),
                                query_pacer=CountingPacer(), single_pacer=single)
    try:
        rec = await client.search_by_doi(POLACK_DOI)
    finally:
        await client.aclose()
    assert rec is not None and state["n"] == 2
    assert single.calls == 2


async def test_t5_pubmed_retry_acquires_pacer_again(monkeypatch):
    from citation_mcp.databases import pubmed as mod

    monkeypatch.setenv("NCBI_API_KEY", "test-ncbi-key")
    monkeypatch.setattr(mod, "_BASE_BACKOFF", 0.0)
    h, state = _first_429_then(polack_pubmed_handler)
    pacer = CountingPacer()
    client = mod.PubMedClient(transport=httpx.MockTransport(h), pacer=pacer)
    try:
        rec = await client.search_by_pmid(POLACK_PMID)
    finally:
        await client.aclose()
    assert rec is not None and state["n"] == 2
    assert pacer.calls == 2


async def test_t5_semantic_scholar_retry_acquires_pacer_again(monkeypatch):
    from citation_mcp.databases import semantic_scholar as mod

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    monkeypatch.setattr(mod, "_BASE_BACKOFF", 0.0)
    h, state = _first_429_then(polack_s2_handler)
    pacer = CountingPacer()
    client = mod.SemanticScholarClient(transport=httpx.MockTransport(h), pacer=pacer)
    try:
        rec = await client.search_by_doi(POLACK_DOI)
    finally:
        await client.aclose()
    assert rec is not None and state["n"] == 2
    assert pacer.calls == 2


# ---------------------------------------------------------------------------
# T6: time budget
# ---------------------------------------------------------------------------


async def test_t6_bulk_budget_exhaustion_skips_databases_without_raising(cache, monkeypatch):
    from citation_mcp.databases.crossref import CrossrefClient
    from citation_mcp.databases.pubmed import PubMedClient
    from citation_mcp.databases.semantic_scholar import SemanticScholarClient
    from citation_mcp.pacing import RatePacer
    from citation_mcp.server import bulk_verify_citations

    monkeypatch.setenv("OPENALEX_API_KEY", "test-openalex-key")
    monkeypatch.setenv("NCBI_API_KEY", "test-ncbi-key")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    # Real clock, slow pacers, a 0.05 s budget: each pacer's first acquire starts at
    # once; every later one is refused immediately, so no real sleep happens.
    crossref = CrossrefClient(transport=httpx.MockTransport(polack_crossref_handler),
                              query_pacer=RatePacer(2.5), single_pacer=RatePacer(2.5))
    pubmed = PubMedClient(transport=httpx.MockTransport(polack_pubmed_handler),
                          pacer=RatePacer(0.7))
    s2 = SemanticScholarClient(transport=httpx.MockTransport(polack_s2_handler),
                               pacer=RatePacer(0.7))
    openalex = make_mock_openalex_client(polack_openalex_handler)
    arxiv = make_mock_arxiv_client(polack_arxiv_handler)
    citations = [{"doi": POLACK_DOI, "title": f"{POLACK_TITLE} {i}"} for i in range(5)]
    t0 = time.monotonic()
    try:
        out = await bulk_verify_citations(
            citations, crossref, cache, pubmed=pubmed, openalex=openalex,
            semantic_scholar=s2, arxiv=arxiv, budget_seconds=0.05,
        )
    finally:
        for c in (crossref, pubmed, s2, openalex, arxiv):
            await c.aclose()
    assert time.monotonic() - t0 < 2.0
    results = out["results"]
    assert len(results) == 5 and all("verified" in r for r in results)
    skipped = 0
    for r in results:
        for db in r["databases_failed"]:
            if db in ("crossref", "pubmed", "semantic_scholar"):
                assert {"source": db, "level": "warning",
                        "message": f"{db} skipped: rate budget exhausted"} in r["warnings"]
                skipped += 1
    # Only one start per pacer fits in the budget: at least 4 of 5 citations lose S2.
    s2_skipped = sum("semantic_scholar" in r["databases_failed"] for r in results)
    assert s2_skipped >= 4
    assert skipped >= 12


async def test_t6_resolve_identifier_budget_exhaustion_is_reported(cache, monkeypatch):
    from citation_mcp.databases.semantic_scholar import SemanticScholarClient
    from citation_mcp.pacing import RatePacer
    from citation_mcp.server import resolve_identifier

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    pacer = RatePacer(0.7)
    assert await pacer.acquire() is True  # use up the immediate slot
    s2 = SemanticScholarClient(transport=httpx.MockTransport(polack_s2_handler), pacer=pacer)
    crossref = make_mock_crossref_client(polack_crossref_handler)
    try:
        out = await resolve_identifier(POLACK_DOI, "doi", None, cache, crossref,
                                       semantic_scholar=s2, budget_seconds=0.05)
    finally:
        await s2.aclose()
        await crossref.aclose()
    assert "semantic_scholar" in out["databases_failed"]
    assert {"source": "semantic_scholar", "level": "warning",
            "message": "semantic_scholar skipped: rate budget exhausted"} in out["warnings"]


# ---------------------------------------------------------------------------
# T7–T8: failure containment
# ---------------------------------------------------------------------------


async def _verify_with_crossref_handler(cache, polack_multi_db, crossref_handler):
    from citation_mcp.server import verify_citation

    crossref = make_mock_crossref_client(crossref_handler)
    try:
        return await verify_citation(
            {"doi": POLACK_DOI}, crossref, cache,
            pubmed=polack_multi_db["pubmed"], openalex=polack_multi_db["openalex"],
            semantic_scholar=polack_multi_db["semantic_scholar"], arxiv=polack_multi_db["arxiv"],
        )
    finally:
        await crossref.aclose()


async def test_t7_value_error_in_one_client_is_contained(cache, polack_multi_db):
    def boom(request: httpx.Request) -> httpx.Response:
        raise ValueError("unexpected payload")

    result = await _verify_with_crossref_handler(cache, polack_multi_db, boom)
    assert "crossref" in result["databases_failed"]
    errs = [w for w in result["warnings"] if w.get("source") == "crossref"]
    assert errs == [{"source": "crossref", "level": "error",
                     "message": "crossref request failed: ValueError: unexpected payload"}]
    assert result["verified"] is True  # the other databases still answer


async def test_t8_cancelled_error_propagates(cache, polack_multi_db):
    def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _verify_with_crossref_handler(cache, polack_multi_db, cancel)


# ---------------------------------------------------------------------------
# T9: degraded-result TTL and cache-key version
# ---------------------------------------------------------------------------


def _spy_ttl(cache):
    seen: list[int] = []
    real_set = cache.set

    async def spy(key, value, type_=None, ttl_seconds=None, **kw):
        seen.append(ttl_seconds)
        return await real_set(key, value, type_=type_, ttl_seconds=ttl_seconds, **kw)

    cache.set = spy
    return seen


async def test_t9_ttl_matched_clean_is_14_days(cache, polack_multi_db):
    seen = _spy_ttl(cache)
    result = await _verify_with_crossref_handler(cache, polack_multi_db, polack_crossref_handler)
    assert result["match_quality"] != "none" and result["databases_failed"] == []
    assert seen == [14 * DAY]


async def test_t9_ttl_matched_with_failed_database_is_24_hours(cache, polack_multi_db, monkeypatch):
    from citation_mcp.databases import semantic_scholar as s2mod
    from citation_mcp.server import verify_citation

    monkeypatch.setattr(s2mod, "_BASE_BACKOFF", 0.0)
    s2_down = make_mock_s2_client(lambda r: httpx.Response(500, text="down"))
    seen = _spy_ttl(cache)
    try:
        result = await verify_citation(
            {"doi": POLACK_DOI}, polack_multi_db["crossref"], cache,
            pubmed=polack_multi_db["pubmed"], openalex=polack_multi_db["openalex"],
            semantic_scholar=s2_down, arxiv=polack_multi_db["arxiv"],
        )
    finally:
        await s2_down.aclose()
    assert result["match_quality"] != "none"
    assert result["databases_failed"] == ["semantic_scholar"]
    assert seen == [DAY]


async def test_t9_ttl_no_match_is_24_hours(cache, monkeypatch):
    from citation_mcp.server import verify_citation

    crossref = make_mock_crossref_client(lambda r: httpx.Response(
        200, json={"status": "ok", "message": {"items": []}}))
    seen = _spy_ttl(cache)
    try:
        result = await verify_citation({"title": "No such paper anywhere at all"}, crossref, cache)
    finally:
        await crossref.aclose()
    assert result["match_quality"] == "none"
    assert seen == [DAY]


def test_t9_cache_key_version_is_3():
    from citation_mcp import cache as cache_mod

    assert cache_mod._CACHE_KEY_VERSION == 3


# ---------------------------------------------------------------------------
# T10: configuration
# ---------------------------------------------------------------------------

_RATE_VARS = {
    "CITATION_MCP_RATE_SEMANTIC_SCHOLAR": ("semantic_scholar", 0.7),
    "CITATION_MCP_RATE_CROSSREF_QUERY": ("crossref_query", 2.5),
    "CITATION_MCP_RATE_CROSSREF_SINGLE": ("crossref_single", 8.0),
    "CITATION_MCP_RATE_PUBMED": ("pubmed", 7.0),
    "CITATION_MCP_BULK_BUDGET_SECONDS": ("bulk_budget_seconds", 240.0),
}


def test_t10_defaults_apply_when_unset():
    from citation_mcp.config import load_rate_config

    cfg = load_rate_config({})
    for _, (attr, default) in _RATE_VARS.items():
        assert getattr(cfg, attr) == pytest.approx(default)


@pytest.mark.parametrize("var", sorted(_RATE_VARS))
def test_t10_each_variable_overrides(var):
    from citation_mcp.config import load_rate_config

    attr, _ = _RATE_VARS[var]
    cfg = load_rate_config({var: "1.25"})
    assert getattr(cfg, attr) == pytest.approx(1.25)


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "nan", "inf"])
@pytest.mark.parametrize("var", sorted(_RATE_VARS))
def test_t10_invalid_value_names_the_variable(var, bad):
    from citation_mcp.config import ConfigError, load_rate_config

    with pytest.raises(ConfigError) as exc:
        load_rate_config({var: bad})
    assert var in str(exc.value)


async def test_t10_invalid_value_stops_startup(monkeypatch, tmp_path):
    from citation_mcp.config import ConfigError
    from citation_mcp.server import _app_lifespan

    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("CITATION_MCP_RATE_SEMANTIC_SCHOLAR", "0")
    with pytest.raises(ConfigError) as exc:
        async with _app_lifespan(None):
            pass
    assert "CITATION_MCP_RATE_SEMANTIC_SCHOLAR" in str(exc.value)
    assert not (tmp_path / "cache.db").exists(), "config is checked before the cache opens"


async def test_t10_startup_builds_clients_with_configured_rates(monkeypatch, tmp_path):
    from citation_mcp.server import _app_lifespan

    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("NCBI_API_KEY", "test-ncbi-key")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    monkeypatch.setenv("CITATION_MCP_RATE_SEMANTIC_SCHOLAR", "0.5")
    monkeypatch.setenv("CITATION_MCP_BULK_BUDGET_SECONDS", "90")
    async with _app_lifespan(None) as ctx:
        assert ctx.semantic_scholar.pacer.rate == pytest.approx(0.5)
        assert ctx.crossref.query_pacer.rate == pytest.approx(2.5)
        assert ctx.crossref.single_pacer.rate == pytest.approx(8.0)
        assert ctx.pubmed.pacer.rate == pytest.approx(7.0)
        assert ctx.rates.bulk_budget_seconds == pytest.approx(90.0)


async def test_t10_keyless_pubmed_rate_stays_as_today(monkeypatch, tmp_path):
    from citation_mcp.server import _app_lifespan

    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    async with _app_lifespan(None) as ctx:
        assert ctx.pubmed.pacer.rate == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# T11: output-shape preservation (key sets captured from v1.0.4 at 0ca96b3)
# ---------------------------------------------------------------------------

V104_VERIFY_KEYS = {"canonical", "confidence", "databases_confirmed", "databases_failed",
                    "databases_queried", "discrepancies", "match_quality", "requires_review",
                    "score_breakdown", "verified", "warnings"}
V104_CANONICAL_KEYS = {"abstract", "arxiv_id", "authors", "citation_count", "doi", "issue",
                       "journal", "journal_iso_abbrev", "openalex_id", "pages", "paper_id",
                       "pmid", "sources", "title", "type", "volume", "year"}
V104_BULK_KEYS = {"results", "summary"}
V104_BULK_SUMMARY_KEYS = {"by_match_quality", "cache_hits", "elapsed_seconds", "total",
                          "verified", "with_warnings"}
V104_RESOLVE_KEYS = {"databases_failed", "databases_queried", "from_type", "identifier",
                     "resolved", "warnings"}
V104_RESOLVED_KEYS = {"arxiv", "doi", "openalex", "pmid", "semantic_scholar"}


async def test_t11_output_key_sets_unchanged(cache, polack_multi_db):
    from citation_mcp.server import bulk_verify_citations, resolve_identifier, verify_citation

    c = polack_multi_db
    kw = {k: v for k, v in c.items() if k != "crossref"}
    v = await verify_citation({"doi": POLACK_DOI}, c["crossref"], cache, **kw)
    b = await bulk_verify_citations([{"doi": POLACK_DOI}, {"title": POLACK_TITLE}],
                                    c["crossref"], cache, **kw)
    r = await resolve_identifier(POLACK_DOI, "doi", None, cache, c["crossref"], **kw)
    assert set(v) == V104_VERIFY_KEYS
    assert set(v["canonical"]) == V104_CANONICAL_KEYS
    assert set(b) == V104_BULK_KEYS
    assert set(b["summary"]) == V104_BULK_SUMMARY_KEYS
    assert all(set(x) == V104_VERIFY_KEYS for x in b["results"])
    assert set(r) == V104_RESOLVE_KEYS
    assert set(r["resolved"]) == V104_RESOLVED_KEYS
