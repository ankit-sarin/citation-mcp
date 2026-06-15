"""Deterministic coverage for Phase CMCP-GATE-0615 — the citation_count surface
is fully NON-GATING in the tolerant tier.

CMCP-DIAG-0615 traced the 2026-06-15 nightly FAIL to row_018: a row matched via
non-carrier DBs (Crossref + PubMed) while BOTH citation_count carriers (OpenAlex
ReadTimeout, Semantic Scholar 429) were down, so canonical.citation_count=None on
a verified=True row. The GATE-OPT1 stub_null guard fired (matched & null count)
even though a null count under a dual-carrier outage is a correct upstream echo,
not a connector stub. CMCP-GATE-0615 removes the entire citation_count surface
(value + stub state) from the tolerant gating set.

This module relocates the stub-detection coverage the live gate gave up, with
pinned per-carrier payloads:

  (i)  matched row + a count carrier returning a real count -> the connector
       surfaces a non-null per-source citation_count (the genuine stub-bug
       target, now exercised deterministically, not via the flaky live gate).
  (ii) matched row + BOTH count carriers down -> the comparator does NOT gate
       (locks the 06-15 row_018 case as a permanent regression). Exercised both
       at the comparator boundary (hand-built actual) and end-to-end through the
       connector (verify_citation with both carriers failing).

Plus the un-reintroducibility lever test: re-arming citation_count in the
comparator's tolerant gating set makes the fixture loader (Rule 14) raise.
"""

from __future__ import annotations

import pytest

from tests.fixtures import comparator as comparator_mod
from tests.fixtures.comparator import (
    TOLERANT_GATE_DENYLIST,
    TOLERANT_GATE_FIELDS,
    compare_tolerant,
)
from tests.fixtures.loader import FixtureValidationError, load_regression_30

# Reuse the self-contained per-DB doubles from the GATE-OPT1 correctness suite.
from tests.test_gate_opt1_correctness import FakeClient, rec


# A baseline tolerant spec shaped like a real fixture row's expected.tolerant:
# a positive citation_count baseline (so stub_null/stub_zero are reachable) and
# an empty allowed_soft_failures list (the row_018 shape — title_only, not arXiv).
_TOLERANT_SPEC = {
    "citation_count": {"value": 3, "tolerance_pct": 20},
    "allowed_soft_failures": [],
}


# ---------------------------------------------------------------------------
# (i) matched row + a count carrier returning a real count -> non-null count
# ---------------------------------------------------------------------------

async def test_matched_carrier_with_count_surfaces_non_null(cache):
    """The genuine stub-bug target: when a count carrier returns a real count on
    a matched row, the connector must surface it (non-null, per-source dict) —
    NOT collapse it to null/zero. This is what stub_null/stub_zero used to guard
    in the live gate; it now lives here, deterministically."""
    from citation_mcp.server import verify_citation

    title = "A Cited Surgical Robotics Study With A Real Count Carrier"
    authors = [{"family": "Ibarra", "given": "Lucia"}]
    doi = "10.1000/gateopt0615.i"
    citation = {"title": title, "authors": ["Lucia Ibarra"], "year": 2021}

    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[
            rec("crossref", doi=doi, title=title, authors=authors, year=2021)]),
        cache=cache,
        openalex=FakeClient("openalex", metadata=[
            rec("openalex", doi=doi, title=title, authors=authors, year=2021,
                citation_count=37)]),
        semantic_scholar=FakeClient("semantic_scholar", metadata=[]),
        force_refresh=True,
    )
    assert res["verified"] is True
    cc = res["canonical"]["citation_count"]
    assert isinstance(cc, dict)               # per-source dict, never a bare int
    assert cc.get("openalex") == 37           # real count surfaced, not stubbed
    assert cc.get("openalex") not in (None, 0)


# ---------------------------------------------------------------------------
# (ii) matched row + BOTH count carriers down -> comparator does NOT gate
# ---------------------------------------------------------------------------

def test_matched_both_carriers_down_does_not_gate():
    """The 06-15 row_018 lock, at the comparator boundary. A matched row whose
    only count carriers (OpenAlex + Semantic Scholar) are both down returns a
    null count — a correct upstream echo. The stub_null anomaly is still COMPUTED
    and EMITTED (informational), but the tolerant tier must PASS."""
    actual = {
        "verified": True,
        "canonical": {"citation_count": None},
        "discrepancies": [],
        "databases_failed": ["openalex", "semantic_scholar"],
    }
    result = compare_tolerant(actual, _TOLERANT_SPEC)

    # Anomaly still computed + emitted for visibility ...
    assert result.citation_count is not None
    assert result.citation_count.anomaly == "stub_null"
    assert result.citation_count.passed is False
    # ... but it does NOT contribute to the verdict.
    assert result.passed is True


async def test_matched_both_carriers_down_end_to_end_does_not_gate(cache):
    """Same lock, end-to-end through the connector: a row that matches via
    Crossref while both count carriers fail (429/timeout) yields a null count,
    and feeding that real response through the comparator still PASSES tolerant.
    This is the deterministic reconstruction of the 06-15 row_018 mechanism."""
    from citation_mcp.server import verify_citation

    title = "A Multi Database Study Whose Count Carriers Are Both Offline"
    authors = [{"family": "Okonkwo", "given": "Ada"}]
    doi = "10.1000/gateopt0615.ii"
    citation = {"title": title, "authors": ["Ada Okonkwo"], "year": 2019}

    res = await verify_citation(
        citation,
        crossref=FakeClient("crossref", metadata=[
            rec("crossref", doi=doi, title=title, authors=authors, year=2019)]),
        cache=cache,
        openalex=FakeClient("openalex", fail=True),
        semantic_scholar=FakeClient("semantic_scholar", fail=True),
        force_refresh=True,
    )
    assert res["verified"] is True
    assert {"openalex", "semantic_scholar"} <= set(res["databases_failed"])

    result = compare_tolerant(res, _TOLERANT_SPEC)
    # null count under a dual-carrier outage on a matched row -> non-gating.
    assert result.passed is True


def test_structural_remains_the_sole_tolerant_gate():
    """The contract: tolerant `passed` is determined SOLELY by discrepancy
    structural well-formedness. A null-count matched row passes; a malformed
    discrepancy entry on the SAME row fails — proving structural is the gate and
    citation_count is not."""
    base = {"verified": True, "canonical": {"citation_count": None}}

    ok = compare_tolerant({**base, "discrepancies": []}, _TOLERANT_SPEC)
    assert ok.passed is True

    malformed = compare_tolerant(
        {**base, "discrepancies": [{"rule": "", "field": "year"}]},  # empty rule, no resolved_to
        _TOLERANT_SPEC,
    )
    assert malformed.discrepancies_structural.passed is False
    assert malformed.passed is False


# ---------------------------------------------------------------------------
# Un-reintroducibility lever: re-arming citation_count gating fails in CI
# ---------------------------------------------------------------------------

def test_tolerant_gate_denylist_is_disjoint_from_gate_fields():
    """Static invariant: the shipped gating set never includes a denylisted
    field. (Rule 14 enforces this at fixture-load; this asserts the ship state.)"""
    assert "citation_count" in TOLERANT_GATE_DENYLIST
    assert set(TOLERANT_GATE_FIELDS).isdisjoint(TOLERANT_GATE_DENYLIST)


def test_rearming_citation_count_gating_trips_rule_14(monkeypatch):
    """Inject a citation_count gating assertion into the tolerant gating set and
    confirm the loader's Rule 14 invariant raises. This is the un-reintroducibility
    lever: re-arming the flaky gate fails in the test suite, not at 2am."""
    # Sanity: the un-injected fixture loads clean.
    load_regression_30()

    monkeypatch.setattr(
        comparator_mod,
        "TOLERANT_GATE_FIELDS",
        TOLERANT_GATE_FIELDS + ("citation_count",),
    )
    with pytest.raises(FixtureValidationError, match=r"Rule 14"):
        load_regression_30()
