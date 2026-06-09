"""Opt-in LIVE upstream-contract check for canonical.arxiv_id provenance.

This is the live counterpart to the deterministic guard in
tests/test_arxiv_id_authority.py. It hits the real upstream DBs to confirm the
contract the mocks assume still holds in production:

  * row_017 (title-only): arXiv is not queried, and Semantic Scholar is the
    sole carrier of arxiv_id (sources it via externalIds.ArXiv).
  * row_028 (explicit arxiv + resolved DOI): arxiv_id survives suppressing
    arXiv, because SS independently carries it (single-drop redundancy).

Gated behind CITATION_MCP_LIVE=1 so it never runs in the normal suite. It must
NEVER flake: unauthenticated / rate-limited / unavailable upstreams cause a
pytest.skip with a clear reason, never a failure. Suppression uses
verify_citation's own graceful-degradation seam (passing a client as None).

Origin: the arxiv_id volatility audit (a scratch suppression probe, now folded
into this gated test as the permanent upstream-contract form).
"""

from __future__ import annotations

import os
import tempfile

import pytest

from citation_mcp.cache import Cache
from citation_mcp.databases.crossref import CrossrefClient
from citation_mcp.databases.pubmed import PubMedClient
from citation_mcp.databases.openalex import OpenAlexClient
from citation_mcp.databases.semantic_scholar import SemanticScholarClient
from citation_mcp.databases.arxiv import ArxivClient
from citation_mcp.server import verify_citation

pytestmark = pytest.mark.skipif(
    not os.environ.get("CITATION_MCP_LIVE"),
    reason="live upstream-contract check; set CITATION_MCP_LIVE=1 to run",
)


def _ss_unavailable(res: dict) -> bool:
    """True if Semantic Scholar failed or was rate-limited this call."""
    if "semantic_scholar" in (res.get("databases_failed") or []):
        return True
    for w in res.get("warnings") or []:
        if w.get("source") == "semantic_scholar":
            return True
    return False


async def _run(citation: dict, suppress: set[str]):
    def c(name, factory):
        return None if name in suppress else factory()

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        cache = Cache(db_path=tmp.name)
        await cache.init()
        return await verify_citation(
            citation,
            crossref=CrossrefClient(),
            cache=cache,
            pubmed=c("pubmed", PubMedClient),
            openalex=c("openalex", OpenAlexClient),
            semantic_scholar=c("semantic_scholar", SemanticScholarClient),
            arxiv=c("arxiv", ArxivClient),
            force_refresh=True,
        )


async def test_live_row017_ss_is_sole_arxiv_carrier():
    """title-only: arXiv not queried; SS supplies arxiv_id when it answers."""
    citation = {"title": "SRT-H: A hierarchical framework for autonomous surgery "
                         "via language-conditioned imitation learning."}
    res = await _run(citation, suppress=set())

    # Structural contract — independent of SS availability.
    assert "arxiv" not in res["databases_queried"], "arXiv must not be queried for title-only input"

    if _ss_unavailable(res):
        pytest.skip("Semantic Scholar unavailable/rate-limited; cannot assert SS-sourced arxiv_id")
    assert res["canonical"]["arxiv_id"] == "2505.10251"


async def test_live_row028_arxiv_id_survives_dropping_arxiv():
    """explicit-arxiv + resolved DOI: SS independently carries arxiv_id, so it
    survives suppressing the arXiv backend (single-drop redundancy)."""
    citation = {"arxiv_id": "1512.03385", "title": "Deep Residual Learning for Image Recognition",
                "authors": ["Kaiming He", "Xiangyu Zhang", "Shaoqing Ren", "Jian Sun"], "year": 2015}
    res = await _run(citation, suppress={"arxiv"})

    if _ss_unavailable(res):
        pytest.skip("Semantic Scholar unavailable/rate-limited; cannot assert SS-backed redundancy")
    assert res["canonical"]["arxiv_id"] == "1512.03385"
