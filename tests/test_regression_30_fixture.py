"""Asserts that tests/fixtures/regression_30.json is structurally valid on every test run.

This is the loader-side check from Phase 2.A. If the fixture file is edited
in a way that violates any of the 12 validation rules, this test fails fast.
"""

from tests.fixtures.loader import load_regression_30, FixtureValidationError  # noqa: F401


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
