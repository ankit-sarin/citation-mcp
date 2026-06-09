"""Asserts that tests/fixtures/regression_30.json is structurally valid on every test run.

This is the loader-side check from Phase 2.A. If the fixture file is edited
in a way that violates any of the 12 validation rules, this test fails fast.
"""

import copy

import pytest

from tests.fixtures.loader import (  # noqa: F401
    load_regression_30,
    FixtureValidationError,
    _validate,
    YEAR_FROM_SNAPSHOT_ROWS,
)


def test_regression_30_fixture_loads_and_validates():
    """Load the fixture and confirm all 12 Phase 2.A rules pass."""
    fixture = load_regression_30()
    assert fixture["fixture_version"] == "1.0"
    assert len(fixture["rows"]) == 30


def test_regression_30_fixture_has_30_unique_row_ids():
    """Spot-check: row_id contiguous and unique."""
    fixture = load_regression_30()
    row_ids = [r["row_id"] for r in fixture["rows"]]
    assert len(set(row_ids)) == 30
    assert row_ids == [f"row_{i:03d}" for i in range(1, 31)]


def test_rule7_year_relocated_to_snapshot_for_named_set():
    """row_016/row_028 have `year` de-gated out of expected.hard; Rule 7 reads
    the mandatory non-null year from expected.snapshot.canonical.year instead.

    Confirms the fixture loads (a) with `year` absent from expected.hard for
    both rows and (b) with a readable non-null snapshot canonical.year.
    """
    assert YEAR_FROM_SNAPSHOT_ROWS == {"row_016", "row_028"}
    fixture = load_regression_30()  # must not raise
    rows = {r["row_id"]: r for r in fixture["rows"]}
    for rid in YEAR_FROM_SNAPSHOT_ROWS:
        r = rows[rid]
        assert "year" not in r["expected"]["hard"]
        assert r["expected"]["snapshot"]["canonical"]["year"] is not None


@pytest.mark.parametrize("rid", sorted(YEAR_FROM_SNAPSHOT_ROWS))
def test_rule7_raises_when_relocated_snapshot_year_is_null(rid):
    """Rule 7 must still fail for the named set if the relocated source —
    expected.snapshot.canonical.year — is null. Closes the v11 coverage gap
    on the Path B year relocation."""
    fixture = copy.deepcopy(load_regression_30())
    row = next(r for r in fixture["rows"] if r["row_id"] == rid)
    row["expected"]["snapshot"]["canonical"]["year"] = None
    with pytest.raises(FixtureValidationError, match=r"Rule 7"):
        _validate(fixture)
