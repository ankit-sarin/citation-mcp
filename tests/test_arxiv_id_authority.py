"""Deterministic guard for the canonical.arxiv_id authority + redundancy logic.

Mocks per-DB contributions (no network, no auth, no flake) and locks in the
four invariants established empirically and re-checked live by
tests/test_arxiv_id_authority_live.py (the opt-in upstream-contract check).
These are the regression guard for the Layer-3 merge authority list and the
verify_citation dispatch/echo paths:

  (i)   canonical.arxiv_id is fed ONLY by the [arxiv, semantic_scholar]
        authority list — crossref/pubmed/openalex never feed it.
  (ii)  title-only input -> arXiv is NOT queried -> SS is the sole carrier ->
        arxiv_id is None when SS contributes nothing.
  (iii) explicit-arxiv + no-DOI -> no-match echo path surfaces the supplied id
        even on total match loss -> arxiv_id is never None.
  (iv)  explicit-arxiv (resolved DOI via metadata) -> arxiv_id present when
        either arXiv OR SS is up; None only on simultaneous arXiv+SS loss.
"""

from __future__ import annotations

import httpx
import pytest

from citation_mcp.scoring import merge_canonical_records
from citation_mcp.server import verify_citation

# The `cache` fixture (in-process Cache with init/close teardown) is the shared
# one from tests/conftest.py — reused here, not redefined, so teardown behavior
# is identical to every existing test.


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

def rec(source: str, **fields) -> dict:
    """A normalized per-DB record skeleton with one `source` and overrides."""
    base = {
        "source": source, "doi": None, "pmid": None, "arxiv_id": None,
        "openalex_id": None, "paper_id": None, "title": None, "authors": None,
        "journal": None, "year": None, "citation_count": None, "type": None,
    }
    base.update(fields)
    return base


class FakeClient:
    """Per-DB stub. Each lookup returns its canned value, or raises to model a
    DB that is up-but-failing (429/5xx) — the graceful-degradation path."""

    def __init__(self, source, *, doi=None, pmid=None, arxiv=None,
                 metadata=None, fail=False, enabled=True):
        self.source = source
        self._doi = doi
        self._pmid = pmid
        self._arxiv = arxiv
        self._metadata = metadata or []
        self._fail = fail
        self.enabled = enabled  # openalex gate in verify_citation

    async def search_by_doi(self, doi):
        if self._fail:
            raise httpx.HTTPError("simulated DB failure")
        return self._doi

    async def search_by_pmid(self, pmid):
        if self._fail:
            raise httpx.HTTPError("simulated DB failure")
        return self._pmid

    async def search_by_arxiv_id(self, arxiv_id):
        if self._fail:
            raise httpx.HTTPError("simulated DB failure")
        return self._arxiv

    async def search_by_metadata(self, **kwargs):
        if self._fail:
            raise httpx.HTTPError("simulated DB failure")
        return list(self._metadata)


# Multi-word titles avoid the <5-token short-title weight adjustment so exact
# matches score cleanly.
T017 = "SRT-H: A hierarchical framework for autonomous surgery via imitation learning"
T028 = "Deep Residual Learning for Image Recognition"
T027 = "Attention Is All You Need"


# ---------------------------------------------------------------------------
# (i) Authority list — merge layer, fully deterministic
# ---------------------------------------------------------------------------

def test_i_canonical_arxiv_id_only_from_arxiv_or_ss():
    # crossref/pubmed/openalex all carry an arxiv_id field — none may feed it.
    non_authority = [
        rec("crossref", arxiv_id="9999.00001", doi="10.1/x", title="t", year=2020),
        rec("pubmed", arxiv_id="9999.00002", pmid="123", title="t", year=2020),
        rec("openalex", arxiv_id="9999.00003", openalex_id="W1", title="t", year=2020),
    ]
    merged = merge_canonical_records(non_authority)
    assert merged["canonical"]["arxiv_id"] is None

    # semantic_scholar is in the authority list -> it feeds arxiv_id.
    merged_ss = merge_canonical_records(non_authority + [rec("semantic_scholar", arxiv_id="2505.10251")])
    assert merged_ss["canonical"]["arxiv_id"] == "2505.10251"

    # arxiv outranks semantic_scholar (authority order ["arxiv", "semantic_scholar"]).
    merged_both = merge_canonical_records(
        non_authority
        + [rec("semantic_scholar", arxiv_id="2505.10251"), rec("arxiv", arxiv_id="2409.14287")]
    )
    assert merged_both["canonical"]["arxiv_id"] == "2409.14287"


# ---------------------------------------------------------------------------
# (ii) title-only -> arXiv not queried -> SS sole carrier
# ---------------------------------------------------------------------------

async def test_ii_title_only_arxiv_not_queried_and_ss_is_sole_carrier(cache):
    citation = {"title": T017}
    crossref = FakeClient("crossref", metadata=[rec("crossref", title=T017)])

    # SS absent -> no carrier -> arxiv_id None; arXiv must not be queried.
    res_no_ss = await verify_citation(
        citation, crossref=crossref, cache=cache,
        semantic_scholar=None, arxiv=FakeClient("arxiv", arxiv=rec("arxiv", arxiv_id="X")),
        force_refresh=True,
    )
    assert "arxiv" not in res_no_ss["databases_queried"]
    assert res_no_ss["canonical"]["arxiv_id"] is None

    # SS up and carrying the externalId -> SS is the sole source of arxiv_id.
    ss_up = FakeClient("semantic_scholar", metadata=[rec("semantic_scholar", title=T017, arxiv_id="2505.10251")])
    res_ss = await verify_citation(
        citation, crossref=crossref, cache=cache,
        semantic_scholar=ss_up, arxiv=FakeClient("arxiv", arxiv=rec("arxiv", arxiv_id="X")),
        force_refresh=True,
    )
    assert "arxiv" not in res_ss["databases_queried"]
    assert res_ss["canonical"]["arxiv_id"] == "2505.10251"

    # SS up but failing (429/5xx) -> contributes nothing -> arxiv_id None.
    ss_down = FakeClient("semantic_scholar", fail=True)
    res_ss_down = await verify_citation(
        citation, crossref=crossref, cache=cache,
        semantic_scholar=ss_down, arxiv=FakeClient("arxiv", arxiv=rec("arxiv", arxiv_id="X")),
        force_refresh=True,
    )
    assert res_ss_down["canonical"]["arxiv_id"] is None
    assert "semantic_scholar" in res_ss_down["databases_failed"]


# ---------------------------------------------------------------------------
# (iii) explicit-arxiv + no-DOI -> echo on total match loss -> never None
# ---------------------------------------------------------------------------

async def test_iii_explicit_arxiv_echo_survives_total_match_loss(cache):
    citation = {
        "arxiv_id": "1706.03762", "title": T027,
        "authors": ["Ashish Vaswani", "Noam Shazeer"], "year": 2017,
    }
    # Every DB contributes nothing.
    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[]),
        cache=cache,
        pubmed=FakeClient("pubmed", metadata=[]),
        semantic_scholar=FakeClient("semantic_scholar", metadata=[]),
        arxiv=FakeClient("arxiv", arxiv=None),
        force_refresh=True,
    )
    assert res["verified"] is False
    # No-match echo path surfaces the supplied identifier — never None.
    assert res["canonical"]["arxiv_id"] == "1706.03762"


# ---------------------------------------------------------------------------
# (iv) explicit-arxiv + resolved DOI -> redundant; None only on double loss
# ---------------------------------------------------------------------------

def _row028_clients(*, arxiv_fail: bool, ss_fail: bool):
    AX = "1512.03385"
    authors = [{"family": "He", "given": "Kaiming"}, {"family": "Zhang", "given": "Xiangyu"}]
    # Crossref resolves the CVPR DOI via metadata, but carries no arxiv_id.
    crossref = FakeClient("crossref", metadata=[
        rec("crossref", doi="10.1109/cvpr.2016.90", title=T028, authors=authors, year=2015)
    ])
    arxiv = FakeClient("arxiv", arxiv=rec("arxiv", arxiv_id=AX, title=T028, year=2015), fail=arxiv_fail)
    ss = FakeClient("semantic_scholar", metadata=[
        rec("semantic_scholar", arxiv_id=AX, title=T028, authors=authors, year=2015)
    ], fail=ss_fail)
    return crossref, arxiv, ss


def _row028_input():
    return {
        "arxiv_id": "1512.03385", "title": T028,
        "authors": ["Kaiming He", "Xiangyu Zhang"], "year": 2015,
    }


@pytest.mark.parametrize(
    "arxiv_fail,ss_fail,expected",
    [
        (False, False, "1512.03385"),  # both up -> arxiv wins authority
        (True, False, "1512.03385"),   # arXiv down -> SS carries it
        (False, True, "1512.03385"),   # SS down -> arXiv carries it
        (True, True, None),            # simultaneous loss -> None (crossref still matches)
    ],
)
async def test_iv_explicit_arxiv_with_doi_redundancy(arxiv_fail, ss_fail, expected, cache):
    crossref, arxiv, ss = _row028_clients(arxiv_fail=arxiv_fail, ss_fail=ss_fail)
    res = await verify_citation(
        _row028_input(), crossref=crossref, cache=cache,
        semantic_scholar=ss, arxiv=arxiv, force_refresh=True,
    )
    # A match is always confirmed (crossref via bibliographic) — this isolates
    # the arxiv_id redundancy from total match loss.
    assert res["verified"] is True
    assert res["canonical"]["arxiv_id"] == expected
