"""v1.0.5 part 3 (CM-108F) fail-first tests: process-level pacing and the CM-108E review fixes.

V1  two POST /mcp requests through the production HTTP app share one pacer per database
V2  two concurrent tool calls through the HTTP app space their Semantic Scholar starts
V3  an invalid setting stops the process before it serves (app construction; CLI exit code)
V4  rate and budget bounds
V5  a budget-skipped result is not cached (verify and resolve; match and no-match)
V6  Crossref keeps a received response when its rate-limit-header wait would cross the deadline
V7  an arXiv rate-limited match takes the 24 h TTL
V8  redaction stops at quotes and commas
V9  the verify path's own redaction call is what protects a non-HTTP error's text
V10 verify logs a contained error once, redacted

HTTP tests replace all five client classes in citation_mcp.server with subclasses on mock
transports, so nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
import types

import httpx
import pytest

from tests.auth.test_e2e_flow import lifespan_manager
from tests.conftest import (
    POLACK_DOI,
    POLACK_TITLE,
    make_mock_arxiv_client,
    make_mock_crossref_client,
    polack_arxiv_handler,
    polack_crossref_handler,
    polack_openalex_handler,
    polack_pubmed_handler,
    polack_s2_handler,
)

DAY = 24 * 60 * 60
FROZEN = lambda: 0.0  # noqa: E731
SIGNING_KEY = "x" * 64
ISSUER = "https://citation-mcp.example.com"
AUDIENCE = "https://citation-mcp.example.com/"
HOST = "citation-mcp.example.com"


# ---------------------------------------------------------------------------
# HTTP-app harness
# ---------------------------------------------------------------------------


def _install_recording_clients(monkeypatch, s2_starts: list[float] | None = None):
    """Make every client the lifespan builds a mock-transport client, and record it."""
    from citation_mcp import server as S
    from citation_mcp.databases.arxiv import ArxivClient
    from citation_mcp.databases.crossref import CrossrefClient
    from citation_mcp.databases.openalex import OpenAlexClient
    from citation_mcp.databases.pubmed import PubMedClient
    from citation_mcp.databases.semantic_scholar import SemanticScholarClient

    def s2_handler(request):
        if s2_starts is not None:
            s2_starts.append(time.monotonic())
        return polack_s2_handler(request)

    made: dict[str, list] = {k: [] for k in ("crossref", "pubmed", "openalex", "semantic_scholar", "arxiv")}
    for name, base, handler in (
        ("crossref", CrossrefClient, polack_crossref_handler),
        ("pubmed", PubMedClient, polack_pubmed_handler),
        ("openalex", OpenAlexClient, polack_openalex_handler),
        ("semantic_scholar", SemanticScholarClient, s2_handler),
        ("arxiv", ArxivClient, polack_arxiv_handler),
    ):
        def init(self, *a, _base=base, _h=handler, _n=name, **kw):
            kw.setdefault("transport", httpx.MockTransport(_h))
            _base.__init__(self, *a, **kw)
            made[_n].append(self)

        monkeypatch.setattr(S, base.__name__, type(base.__name__, (base,), {"__init__": init}))
    return made


def _env(monkeypatch, tmp_path, **extra):
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("NCBI_API_KEY", "test-ncbi-key")
    monkeypatch.setenv("OPENALEX_API_KEY", "test-openalex-key")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def _app_and_token():
    from citation_mcp.auth.storage import OAuthStorage
    from citation_mcp.auth.tokens import issue_access_token
    from citation_mcp.http_app import build_http_app

    app = build_http_app(
        signing_key=SIGNING_KEY, issuer=ISSUER, audience=AUDIENCE,
        origin_allowlist=("https://claude.ai",), host_allowlist=(HOST, "testserver"),
        storage=OAuthStorage(":memory:"),
    )
    token = issue_access_token(sub="user@example.com", client_id="test-client", scope="",
                               signing_key=SIGNING_KEY, issuer=ISSUER, audience=AUDIENCE)
    return app, token


def _headers(token):
    return {"authorization": f"Bearer {token}", "origin": "https://claude.ai",
            "content-type": "application/json", "accept": "application/json, text/event-stream"}


def _rpc(method, params, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "v105", "version": "0"}}


def _sse_json(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no JSON-RPC payload in response: {resp.status_code} {resp.text[:200]}")


# ---------------------------------------------------------------------------
# V1 / V2: process-level pacing through the production HTTP app
# ---------------------------------------------------------------------------


async def test_v1_two_mcp_requests_share_one_pacer_per_database(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    made = _install_recording_clients(monkeypatch)
    app, token = _app_and_token()
    async with lifespan_manager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=f"http://{HOST}") as http:
            for rid in (1, 2):
                r = await http.post("/mcp", json=_rpc("initialize", INIT, rid), headers=_headers(token))
                assert r.status_code == 200, r.text
    # Stateless HTTP enters the tool lifespan once per request (CM-108E F1).
    assert len(made["semantic_scholar"]) >= 2
    s2, cr, pm, ax = (made[k] for k in ("semantic_scholar", "crossref", "pubmed", "arxiv"))
    assert all(c.pacer is s2[0].pacer for c in s2), "Semantic Scholar pacer shared"
    assert all(c.query_pacer is cr[0].query_pacer for c in cr), "Crossref list/query pacer shared"
    assert all(c.single_pacer is cr[0].single_pacer for c in cr), "Crossref single-record pacer shared"
    assert all(c.pacer is pm[0].pacer for c in pm), "PubMed pacer shared"
    assert all(c._spacing_lock is ax[0]._spacing_lock for c in ax), "arXiv spacing state shared"


async def test_v2_concurrent_tool_calls_space_semantic_scholar_starts(monkeypatch, tmp_path):
    rate = 20.0  # 50 ms spacing: proves sharing without a real sleep over 0.1 s
    _env(monkeypatch, tmp_path, CITATION_MCP_RATE_SEMANTIC_SCHOLAR=str(rate))
    starts: list[float] = []
    _install_recording_clients(monkeypatch, s2_starts=starts)
    app, token = _app_and_token()

    def call(rid, title):
        return _rpc("tools/call", {"name": "verifyCitation",
                                   "arguments": {"doi": POLACK_DOI, "title": title, "force_refresh": True}}, rid)

    async with lifespan_manager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=f"http://{HOST}") as http:
            r1, r2 = await asyncio.gather(
                http.post("/mcp", json=call(1, "A"), headers=_headers(token)),
                http.post("/mcp", json=call(2, "B"), headers=_headers(token)),
            )
    for r in (r1, r2):
        assert r.status_code == 200, r.text
        assert "result" in _sse_json(r), r.text[:300]
    assert len(starts) == 2
    assert abs(starts[1] - starts[0]) >= 1 / rate - 0.005, starts


# ---------------------------------------------------------------------------
# V3 / V4: fail-fast at startup, bounds
# ---------------------------------------------------------------------------


def test_v3_app_construction_raises_on_invalid_setting(monkeypatch, tmp_path):
    from citation_mcp.config import ConfigError

    _env(monkeypatch, tmp_path, CITATION_MCP_RATE_PUBMED="0")
    with pytest.raises(ConfigError) as exc:
        _app_and_token()
    assert "CITATION_MCP_RATE_PUBMED" in str(exc.value)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_v3_cli_exits_nonzero_naming_the_variable(transport, tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and not k.startswith("CITATION_MCP_RATE")}
    env.update({
        "CACHE_DB_PATH": str(tmp_path / "cache.db"), "OAUTH_DB_PATH": str(tmp_path / "oauth.db"),
        "OAUTH_SIGNING_KEY": "ab" * 32, "MCP_HTTP_HOST": "127.0.0.1", "MCP_HTTP_PORT": str(_free_port()),
        "CITATION_MCP_RATE_SEMANTIC_SCHOLAR": "-1",
    })
    try:
        p = subprocess.run([sys.executable, "-m", "citation_mcp.cli", "--transport", transport],
                           env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"process kept running (served) instead of exiting: {(e.stderr or b'')[-300:]!r}")
    assert p.returncode != 0
    assert "CITATION_MCP_RATE_SEMANTIC_SCHOLAR" in p.stderr
    assert "Uvicorn running" not in p.stderr
    assert not (tmp_path / "cache.db").exists()


@pytest.mark.parametrize("var,value,ok", [
    ("CITATION_MCP_RATE_SEMANTIC_SCHOLAR", "0.01", True),
    ("CITATION_MCP_RATE_SEMANTIC_SCHOLAR", "0.0099", False),
    ("CITATION_MCP_RATE_SEMANTIC_SCHOLAR", "1e-320", False),
    ("CITATION_MCP_RATE_CROSSREF_QUERY", "1000", True),
    ("CITATION_MCP_RATE_CROSSREF_QUERY", "1000.5", False),
    ("CITATION_MCP_RATE_CROSSREF_SINGLE", "1e9", False),
    ("CITATION_MCP_RATE_PUBMED", "0.001", False),
    ("CITATION_MCP_BULK_BUDGET_SECONDS", "1", True),
    ("CITATION_MCP_BULK_BUDGET_SECONDS", "0.5", False),
    ("CITATION_MCP_BULK_BUDGET_SECONDS", "3600", True),
    ("CITATION_MCP_BULK_BUDGET_SECONDS", "3601", False),
])
def test_v4_bounds(var, value, ok):
    from citation_mcp.config import ConfigError, load_rate_config

    if ok:
        load_rate_config({var: value})
    else:
        with pytest.raises(ConfigError) as exc:
            load_rate_config({var: value})
        assert var in str(exc.value)


# ---------------------------------------------------------------------------
# V5: budget-skipped results are not cached
# ---------------------------------------------------------------------------


def _spy_ttl(cache):
    seen: list[int] = []
    real_set = cache.set

    async def spy(key, value, type_=None, ttl_seconds=None, **kw):
        seen.append(ttl_seconds)
        return await real_set(key, value, type_=type_, ttl_seconds=ttl_seconds, **kw)

    cache.set = spy
    return seen


async def _spent_s2(monkeypatch):
    """A Semantic Scholar client whose pacer's immediate slot is already used."""
    from citation_mcp.databases.semantic_scholar import SemanticScholarClient
    from citation_mcp.pacing import RatePacer

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")
    pacer = RatePacer(0.7)
    assert await pacer.acquire()
    return SemanticScholarClient(transport=httpx.MockTransport(polack_s2_handler), pacer=pacer)


async def test_v5_skipped_match_is_not_cached(cache, monkeypatch):
    from citation_mcp.server import verify_citation

    s2 = await _spent_s2(monkeypatch)
    crossref = make_mock_crossref_client(polack_crossref_handler)
    seen = _spy_ttl(cache)
    try:
        r = await verify_citation({"doi": POLACK_DOI}, crossref, cache, semantic_scholar=s2,
                                  budget_seconds=0.05)
    finally:
        await s2.aclose()
        await crossref.aclose()
    assert r["verified"] is True and "semantic_scholar" in r["databases_failed"]
    assert seen == [], "a budget-skipped result must not be cached"


async def test_v5_skipped_no_match_is_not_cached(cache, monkeypatch):
    from citation_mcp.server import verify_citation

    s2 = await _spent_s2(monkeypatch)
    crossref = make_mock_crossref_client(
        lambda r: httpx.Response(200, json={"status": "ok", "message": {"items": []}}))
    seen = _spy_ttl(cache)
    try:
        r = await verify_citation({"title": "Nothing matches this title at all"}, crossref, cache,
                                  semantic_scholar=s2, budget_seconds=0.05)
    finally:
        await s2.aclose()
        await crossref.aclose()
    assert r["match_quality"] == "none" and "semantic_scholar" in r["databases_failed"]
    assert seen == []


async def test_v5_skipped_resolve_is_not_cached(cache, monkeypatch):
    from citation_mcp.server import resolve_identifier

    s2 = await _spent_s2(monkeypatch)
    crossref = make_mock_crossref_client(polack_crossref_handler)
    seen = _spy_ttl(cache)
    try:
        r = await resolve_identifier(POLACK_DOI, "doi", None, cache, crossref, semantic_scholar=s2,
                                     budget_seconds=0.05)
    finally:
        await s2.aclose()
        await crossref.aclose()
    assert "semantic_scholar" in r["databases_failed"]
    assert seen == []


# ---------------------------------------------------------------------------
# V6: Crossref keeps a received response
# ---------------------------------------------------------------------------


async def test_v6_crossref_returns_response_when_header_wait_would_cross_deadline():
    from citation_mcp import pacing
    from citation_mcp.databases.crossref import CrossrefClient

    def h(request):
        r = polack_crossref_handler(request)
        return httpx.Response(200, content=r.content, headers={
            "content-type": "application/json", "X-Rate-Limit-Limit": "3",
            "X-Rate-Limit-Interval": "1s", "X-Rate-Limit-Remaining": "1"})

    client = CrossrefClient(transport=httpx.MockTransport(h), query_pacer=None, single_pacer=None)
    t0 = time.monotonic()
    try:
        with pacing.budget(0.5, clock=FROZEN):  # the 1 s header wait would end after the deadline
            rec = await client.search_by_doi(POLACK_DOI)
    finally:
        await client.aclose()
    assert rec is not None and rec.get("doi")
    assert time.monotonic() - t0 < 0.5, "the wait was skipped, not taken"


# ---------------------------------------------------------------------------
# V7: arXiv rate-limited counts as degraded
# ---------------------------------------------------------------------------


async def test_v7_arxiv_rate_limited_match_gets_24_hours(cache, monkeypatch):
    from citation_mcp.databases import arxiv as axmod
    from citation_mcp.server import verify_citation

    monkeypatch.setattr(axmod, "_BASE_BACKOFF", 0.0)
    monkeypatch.setattr(axmod, "random", types.SimpleNamespace(uniform=lambda a, b: 0.0))
    arxiv = make_mock_arxiv_client(lambda r: httpx.Response(429, text="slow down"))
    crossref = make_mock_crossref_client(polack_crossref_handler)
    seen = _spy_ttl(cache)
    try:
        r = await verify_citation({"arxiv_id": "2103.04567", "title": POLACK_TITLE,
                                   "authors": ["Polack, Fernando P."], "year": 2020},
                                  crossref, cache, arxiv=arxiv)
    finally:
        await arxiv.aclose()
        await crossref.aclose()
    assert r["match_quality"] != "none"
    assert "arxiv" not in r["databases_failed"]
    assert any(w.get("source") == "arxiv" and "rate-limited" in w.get("message", "") for w in r["warnings"])
    assert seen == [DAY]


# ---------------------------------------------------------------------------
# V8 / V9 / V10: redaction and logging
# ---------------------------------------------------------------------------


def test_v8_redaction_stops_at_quotes_and_commas():
    from citation_mcp.log_redaction import redact_query_string_in_text

    text = ("for url 'https://x.example/y?db=pubmed&email=someone%40example.org', "
            "then \"https://z.example/w?api_key=SECRET\", and https://v.example/?mailto=a@b.org,x")
    out = redact_query_string_in_text(text)
    assert out == ("for url 'https://x.example/y?db=pubmed&email=***', "
                   "then \"https://z.example/w?api_key=***\", and https://v.example/?mailto=***,x")


LEAKY = ("payload error from https://eutils.example/esearch.fcgi?tool=t"
         "&email=redaction.test%40example.org&api_key=SECRET-KEY-V9")


def _leaky(request):
    raise ValueError(LEAKY)


async def test_v9_non_http_error_text_is_redacted_by_the_verify_path(cache):
    """httpx never touched this message, so only server.py's own redaction call can blank it."""
    from citation_mcp.server import verify_citation

    crossref = make_mock_crossref_client(_leaky)
    try:
        r = await verify_citation({"doi": POLACK_DOI}, crossref, cache)
    finally:
        await crossref.aclose()
    msgs = [w["message"] for w in r["warnings"] if w.get("source") == "crossref"]
    assert msgs and msgs[0].startswith("crossref request failed: ValueError: payload error from")
    assert "redaction.test" not in msgs[0] and "SECRET-KEY-V9" not in msgs[0]


async def test_v10_verify_logs_a_contained_error_once_redacted(cache, caplog):
    from citation_mcp.server import verify_citation

    crossref = make_mock_crossref_client(_leaky)
    caplog.set_level(logging.WARNING, logger="citation_mcp")
    try:
        await verify_citation({"doi": POLACK_DOI}, crossref, cache)
    finally:
        await crossref.aclose()
    recs = [r for r in caplog.records if r.name == "citation_mcp" and "crossref" in r.getMessage()]
    assert len(recs) == 1, [r.getMessage() for r in recs]
    assert recs[0].levelno == logging.WARNING
    msg = recs[0].getMessage()
    assert "ValueError" in msg and "redaction.test" not in msg and "SECRET-KEY-V9" not in msg
