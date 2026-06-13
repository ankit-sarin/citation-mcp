"""Deterministic, mocked correctness coverage for Phase GATE-OPT1.

The live nightly gate was reduced (Phase GATE-OPT1) to assert only
connector-invariant properties; every POSITIVE value/decision (match_found=True,
resolved identifiers, confidence, match_quality, year, first author,
citation_count value) was demoted to the non-gating snapshot tier because those
fields flip with upstream-DB availability and indexing, producing recurring
false nightly FAILs (06-05 / 06-08 / 06-12).

This module is where that correctness coverage now lives — pinned per-DB
payloads, no network, no auth, no flake. It generalizes the test-double pattern
from tests/test_arxiv_id_authority.py (FakeClient + verify_citation injection,
reusing the shared `cache` fixture from tests/conftest.py).

  T1 — positive bibliographic match -> verified True, correct canonical
        doi/year/first-author, confidence 1.0, match_quality high.
  T2 — row_013 specifically: OpenAlex as sole carrier of the Cursi 2022
        IEEE T-ASE record -> verified True, confidence 1.0. This is the exact
        coverage the live gate gave up (sole-carrier collapse on 06-12).
  T3 — identifier-decisive short-circuit (DOI / PMID / arXiv) -> confidence 1.0.
  T4 — canonical.citation_count is a per-source dict, never a bare int.
  T5 — fabrication: no DB returns a match -> verified False, null
        doi/pmid/arxiv (the carrier-invariant negative).
  T6 — earliest-year preprint duality: preprint + proceedings records present
        -> Layer 3 selects the earliest (preprint) year (locks row_016/028).
"""

from __future__ import annotations

import httpx
import pytest

from citation_mcp.scoring import merge_canonical_records
from citation_mcp.server import verify_citation


# ---------------------------------------------------------------------------
# Test doubles — copied from tests/test_arxiv_id_authority.py to keep this
# regression guard self-contained (deferred consolidation: a shared
# tests/_doubles.py is a v0.4 hygiene candidate).
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


# Pinned row_013 record: Cursi et al. 2022, IEEE T-ASE. Multi-word title avoids
# the <5-token short-title weight adjustment so exact matches score cleanly.
CURSI_DOI = "10.1109/tase.2022.3219590"
CURSI_TITLE = (
    "Task Accuracy Enhancement for a Surgical Macro-Micro Manipulator With "
    "Probabilistic Neural Networks and Uncertainty Minimization"
)
CURSI_AUTHORS = [{"family": "Cursi", "given": "Francesco"},
                 {"family": "Bai", "given": "Weibang"}]


# ---------------------------------------------------------------------------
# T1 — positive bibliographic match correctness
# ---------------------------------------------------------------------------

async def test_t1_positive_bibliographic_match(cache):
    title = "A Multi Database Confirmed Surgical Robotics Benchmark Study"
    authors = [{"family": "Smith", "given": "Jane"},
               {"family": "Okafor", "given": "Chidi"}]
    doi = "10.1000/gateopt1.t1"
    citation = {"title": title, "authors": ["Jane Smith", "Chidi Okafor"], "year": 2021}

    record = lambda src: rec(src, doi=doi, title=title, authors=authors, year=2021)
    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[record("crossref")]),
        cache=cache,
        openalex=FakeClient("openalex", metadata=[record("openalex")]),
        semantic_scholar=FakeClient("semantic_scholar", metadata=[record("semantic_scholar")]),
        force_refresh=True,
    )
    assert res["verified"] is True
    assert res["confidence"] == 1.0
    assert res["match_quality"] == "high"
    assert res["canonical"]["doi"] == doi
    assert res["canonical"]["year"] == 2021
    assert res["canonical"]["authors"][0]["family"] == "Smith"


# ---------------------------------------------------------------------------
# T2 — row_013: OpenAlex sole carrier (the coverage the live gate gave up)
# ---------------------------------------------------------------------------

async def test_t2_row013_openalex_sole_carrier_matches(cache):
    """row_013 baseline: db_confirmed == ['openalex'] (sole carrier). When the
    bibliographic input resolves through OpenAlex alone, the connector must
    still confirm the match at confidence 1.0 — this is exactly the assertion
    the live gate surrendered when OpenAlex timed out on 2026-06-12."""
    citation = {
        "title": CURSI_TITLE,
        "authors": ["Francesco Cursi", "Weibang Bai"],
        "year": 2022,
    }
    openalex_rec = rec("openalex", doi=CURSI_DOI, title=CURSI_TITLE,
                       authors=CURSI_AUTHORS, year=2022)
    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[]),
        cache=cache,
        openalex=FakeClient("openalex", metadata=[openalex_rec]),
        semantic_scholar=FakeClient("semantic_scholar", metadata=[]),
        force_refresh=True,
    )
    assert res["verified"] is True
    assert res["confidence"] == 1.0
    assert res["match_quality"] == "high"
    assert res["canonical"]["doi"] == CURSI_DOI
    assert res["canonical"]["year"] == 2022
    assert res["databases_confirmed"] == ["openalex"]


async def test_t2b_row013_openalex_down_collapses_to_no_match(cache):
    """The flip side, documenting WHY the live gate can't assert row_013's
    match_found: with OpenAlex (sole carrier) failing and no other DB carrying
    the record, the connector correctly degrades to no-match. This is an
    upstream-availability artifact, NOT a connector regression — so it must not
    gate live."""
    citation = {
        "title": CURSI_TITLE,
        "authors": ["Francesco Cursi", "Weibang Bai"],
        "year": 2022,
    }
    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[]),
        cache=cache,
        openalex=FakeClient("openalex", fail=True),
        semantic_scholar=FakeClient("semantic_scholar", metadata=[]),
        force_refresh=True,
    )
    assert res["verified"] is False
    assert "openalex" in res["databases_failed"]
    assert res["canonical"]["doi"] is None


# ---------------------------------------------------------------------------
# T3 — identifier-decisive short-circuit
# ---------------------------------------------------------------------------

async def test_t3_doi_short_circuit_confidence_one(cache):
    res = await verify_citation(
        {"doi": CURSI_DOI},
        crossref=FakeClient("crossref", doi=rec("crossref", doi=CURSI_DOI,
                                                title=CURSI_TITLE, year=2022)),
        cache=cache,
        force_refresh=True,
    )
    assert res["verified"] is True
    assert res["confidence"] == 1.0
    assert res["match_quality"] == "high"
    assert res["canonical"]["doi"] == CURSI_DOI


async def test_t3_pmid_short_circuit_confidence_one(cache):
    res = await verify_citation(
        {"pmid": "35080901"},
        crossref=FakeClient("crossref", metadata=[]),
        cache=cache,
        pubmed=FakeClient("pubmed", pmid=rec("pubmed", pmid="35080901",
                                             title=CURSI_TITLE, year=2022)),
        force_refresh=True,
    )
    assert res["verified"] is True
    assert res["confidence"] == 1.0
    assert res["canonical"]["pmid"] == "35080901"


async def test_t3_arxiv_short_circuit_confidence_one(cache):
    # verifyCitation requires at least one of doi/pmid/title (loader Rule 8:
    # arXiv rows always carry a title too). The explicit arxiv_id still drives
    # the Layer-1 identifier-decisive match.
    res = await verify_citation(
        {"arxiv_id": "1706.03762", "title": "Attention Is All You Need"},
        crossref=FakeClient("crossref", metadata=[]),
        cache=cache,
        arxiv=FakeClient("arxiv", arxiv=rec("arxiv", arxiv_id="1706.03762",
                                            title="Attention Is All You Need", year=2017)),
        force_refresh=True,
    )
    assert res["verified"] is True
    assert res["confidence"] == 1.0
    assert res["canonical"]["arxiv_id"] == "1706.03762"


# ---------------------------------------------------------------------------
# T4 — citation_count is a per-source dict, never a bare int
# ---------------------------------------------------------------------------

async def test_t4_citation_count_is_per_source_dict(cache):
    """Item 2 structural invariant. Only openalex + semantic_scholar feed
    citation_count, and they feed it as a per-source dict — never a bare int."""
    title = "A Cited Robotics Survey With Two Counting Sources"
    authors = [{"family": "Nguyen", "given": "An"}]
    doi = "10.1000/gateopt1.t4"
    citation = {"title": title, "authors": ["An Nguyen"], "year": 2020}

    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[
            rec("crossref", doi=doi, title=title, authors=authors, year=2020)]),
        cache=cache,
        openalex=FakeClient("openalex", metadata=[
            rec("openalex", doi=doi, title=title, authors=authors, year=2020,
                citation_count=42)]),
        semantic_scholar=FakeClient("semantic_scholar", metadata=[
            rec("semantic_scholar", doi=doi, title=title, authors=authors, year=2020,
                citation_count=45)]),
        force_refresh=True,
    )
    cc = res["canonical"]["citation_count"]
    assert isinstance(cc, dict)
    assert cc == {"openalex": 42, "semantic_scholar": 45}
    assert not isinstance(cc, int)


# ---------------------------------------------------------------------------
# T5 — fabrication: no DB matches -> carrier-invariant negative
# ---------------------------------------------------------------------------

async def test_t5_fabrication_returns_no_match_and_null_identifiers(cache):
    """A fabricated citation that no DB can match must surface as
    match_found=False with null doi/pmid/arxiv. A real DB matching a
    fabrication, or surfacing an identifier for one, is always a connector bug —
    this is the negative the live gate still enforces (row_029) and the
    deterministic backstop for it."""
    citation = {
        "title": "Quantum Entanglement Assisted Laparoscopic Cholecystectomy in Zero Gravity",
        "authors": ["Nemo Nonexistent"],
        "year": 2023,
    }
    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[]),
        cache=cache,
        openalex=FakeClient("openalex", metadata=[]),
        semantic_scholar=FakeClient("semantic_scholar", metadata=[]),
        pubmed=FakeClient("pubmed", metadata=[]),
        force_refresh=True,
    )
    assert res["verified"] is False
    assert res["canonical"]["doi"] is None
    assert res["canonical"]["pmid"] is None
    assert res["canonical"]["arxiv_id"] is None


# ---------------------------------------------------------------------------
# T6 — earliest-year preprint duality (locks row_016 / row_028 logic)
# ---------------------------------------------------------------------------

def test_t6_layer3_selects_earliest_preprint_year():
    """row_016/row_028 trap: a preprint (earlier year) and its later
    conference-proceedings publication coexist. Layer 3 must resolve
    canonical.year to the EARLIEST non-null year across DBs."""
    title = "Deep Residual Learning for Image Recognition"
    preprint = rec("semantic_scholar", arxiv_id="1512.03385", title=title, year=2015)
    proceedings = rec("crossref", doi="10.1109/cvpr.2016.90", title=title, year=2016)

    merged = merge_canonical_records([proceedings, preprint])
    assert merged["canonical"]["year"] == 2015

    # Order-independent: same result if proceedings is listed first or last.
    merged_rev = merge_canonical_records([preprint, proceedings])
    assert merged_rev["canonical"]["year"] == 2015
