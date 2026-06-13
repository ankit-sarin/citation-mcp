"""Asserts that tests/fixtures/regression_30.json is structurally valid on every test run.

This is the loader-side check from Phase 2.A. If the fixture file is edited
in a way that violates any of the 12 validation rules, this test fails fast.
"""

import copy

import pytest

from tests.fixtures.loader import (  # noqa: F401
    load_regression_30,
    FixtureValidationError,
    HARD_DENYLIST_ALWAYS,
    HARD_DENYLIST_IDENTIFIERS,
    _validate,
)


def test_regression_30_fixture_loads_and_validates():
    """Load the fixture and confirm all structural rules pass."""
    fixture = load_regression_30()
    assert fixture["fixture_version"] == "1.0"
    assert len(fixture["rows"]) == 30


def test_regression_30_fixture_has_30_unique_row_ids():
    """Spot-check: row_id contiguous and unique."""
    fixture = load_regression_30()
    row_ids = [r["row_id"] for r in fixture["rows"]]
    assert len(set(row_ids)) == 30
    assert row_ids == [f"row_{i:03d}" for i in range(1, 31)]


# =============================================================================
# Phase GATE-OPT1 — hard-tier reduction
# =============================================================================


def test_all_matched_rows_have_empty_hard_block():
    """After GATE-OPT1 every positive value/decision demotes to snapshot, so
    every matched (non-no-match) row carries an empty expected.hard block. The
    sole exception is the no-match row_029, which retains the carrier-invariant
    negative."""
    fixture = load_regression_30()
    for r in fixture["rows"]:
        h = r["expected"]["hard"]
        if r["row_id"] == "row_029":
            assert h.get("match_found") is False
            assert h.get("doi_resolved") is None
            assert h.get("pmid_resolved") is None
            assert h.get("arxiv_id_resolved") is None
        else:
            assert h == {}, f"{r['row_id']} hard block not empty: {h}"


def test_rule7_reads_completeness_from_snapshot_uniformly():
    """Rule 7 now reads match_found/first-author/year/match_quality from the
    snapshot for every non-adversarial row (no named-set special case)."""
    fixture = load_regression_30()  # must not raise
    for r in fixture["rows"]:
        if r["category"] == "adversarial":
            continue
        s = r["expected"]["snapshot"]
        assert s["verified"] is True
        assert s["canonical"]["year"] is not None
        assert s["match_quality"] is not None
        assert s["canonical"]["authors"][0]["family"]


@pytest.mark.parametrize("rid", ["row_016", "row_028", "row_005"])
def test_rule7_raises_when_snapshot_year_is_null(rid):
    """Rule 7 fails for ANY non-adversarial row if its snapshot canonical.year
    is null — proving the uniform snapshot-read (not just a named set)."""
    fixture = copy.deepcopy(load_regression_30())
    row = next(r for r in fixture["rows"] if r["row_id"] == rid)
    row["expected"]["snapshot"]["canonical"]["year"] = None
    with pytest.raises(FixtureValidationError, match=r"Rule 7"):
        _validate(fixture)


# =============================================================================
# Phase GATE-OPT1 — Rule 13 denylist guard (Acceptance Gate 3)
# =============================================================================


@pytest.mark.parametrize("field", sorted(HARD_DENYLIST_ALWAYS))
def test_rule13_raises_on_always_denylisted_field(field):
    """Injecting any always-denylisted positive field into a row's hard block
    must raise at load. This is the un-reintroducibility lever."""
    fixture = copy.deepcopy(load_regression_30())
    fixture["rows"][0]["expected"]["hard"][field] = "anything"
    with pytest.raises(FixtureValidationError, match=r"Rule 13"):
        _validate(fixture)


def test_rule13_raises_on_positive_match_found():
    """match_found=True re-armed in hard must raise (the positive form is
    denylisted; only the False negative survives)."""
    fixture = copy.deepcopy(load_regression_30())
    fixture["rows"][0]["expected"]["hard"]["match_found"] = True
    with pytest.raises(FixtureValidationError, match=r"Rule 13"):
        _validate(fixture)


@pytest.mark.parametrize("field", sorted(HARD_DENYLIST_IDENTIFIERS))
def test_rule13_raises_on_non_null_identifier(field):
    """A non-null identifier in hard must raise; a null identifier is the
    permitted carrier-invariant negative."""
    fixture = copy.deepcopy(load_regression_30())
    fixture["rows"][0]["expected"]["hard"][field] = "10.1234/x"
    with pytest.raises(FixtureValidationError, match=r"Rule 13"):
        _validate(fixture)


@pytest.mark.parametrize("field", sorted(HARD_DENYLIST_IDENTIFIERS))
def test_rule13_allows_null_identifier_negative(field):
    """A null identifier in hard is the carrier-invariant negative — permitted
    (row_029 carries exactly these)."""
    fixture = copy.deepcopy(load_regression_30())
    fixture["rows"][0]["expected"]["hard"][field] = None
    _validate(fixture)  # must NOT raise
