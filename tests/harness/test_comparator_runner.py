"""Unit tests for harness.comparator_runner.

All inputs are hand-built — no real regression_30.json — so test failures
point at the runner logic, not at fixture drift.
"""

from __future__ import annotations

import pytest

from harness.comparator_runner import (
    BulkComparisonResult,
    RowComparisonResult,
    run_comparison,
)


def _row(row_id: str, hard: dict, tolerant: dict, snapshot: dict, category: str = "identifier_decisive") -> dict:
    return {
        "row_id": row_id,
        "category": category,
        "input": {"doi": "10.0/test"},
        "expected": {"hard": hard, "tolerant": tolerant, "snapshot": snapshot},
    }


def _result(**fields) -> dict:
    """Build a synthetic per-citation result. Defaults align with snapshot
    shape so the snapshot tier sees no key-presence diffs.
    """
    base = {
        "verified": True,
        "confidence": 0.95,
        "match_quality": "high",
        "databases_confirmed": [],
        "databases_queried": [],
        "databases_failed": [],
        "canonical": {},
        "discrepancies": [],
        "requires_review": False,
        "warnings": [],
        "score_breakdown": {},
    }
    base.update(fields)
    return base


def test_all_rows_pass():
    snap_a = _result(canonical={"doi": "10.0/A"})
    snap_b = _result(canonical={"doi": "10.0/B"})
    snap_c = _result(canonical={"doi": "10.0/C"})
    fixture_rows = [
        _row("row_001", {"match_found": True}, {}, snap_a),
        _row("row_002", {"match_found": True}, {}, snap_b),
        _row("row_003", {"match_found": True}, {}, snap_c),
    ]
    payload = {"results": [
        _result(canonical={"doi": "10.0/A"}, match_found=True),
        _result(canonical={"doi": "10.0/B"}, match_found=True),
        _result(canonical={"doi": "10.0/C"}, match_found=True),
    ]}
    # Note: snapshot tier would diff "match_found" key (absent in snapshot,
    # present in actual). Re-set the snapshot to include it.
    for i, fr in enumerate(fixture_rows):
        fr["expected"]["snapshot"] = payload["results"][i]

    result = run_comparison(fixture_rows, payload)
    assert isinstance(result, BulkComparisonResult)
    assert result.all_passed_hard is True
    assert result.all_passed_tolerant is True
    assert all(r.snapshot_verdict == "NONE" for r in result.rows)


def test_one_row_fails_hard():
    # `year` goes through HARD_FIELD_EXTRACTORS → canonical.year, so the
    # synthetic response must place year inside canonical (not top-level).
    payload = {"results": [
        _result(canonical={"year": 2024}),    # passes
        _result(canonical={"year": 2023}),    # fails hard on year
    ]}
    fixture_rows = [
        _row("row_001", {"year": 2024}, {}, payload["results"][0]),
        _row("row_002", {"year": 2024}, {}, payload["results"][1]),
    ]

    result = run_comparison(fixture_rows, payload)
    assert result.all_passed_hard is False
    assert result.rows[0].passed_hard is True
    assert result.rows[1].passed_hard is False


def test_one_row_fails_tolerant():
    # Under the structural citation_count guard, only stub-shaped anomalies
    # gate. row_001 returns a healthy (matched) count → passes; row_002 is a
    # matched row whose count collapsed to null (resolver stub) → stub_null.
    # _result() defaults verified=True, so the stub guard is in scope.
    snap = _result(canonical={"citation_count": {"openalex": 100}})
    fixture_rows = [
        _row(
            "row_001",
            {},
            {"citation_count": {"value": 100, "tolerance_pct": 20}},
            snap,
        ),
        _row(
            "row_002",
            {},
            {"citation_count": {"value": 100, "tolerance_pct": 20}},
            snap,
        ),
    ]
    payload = {"results": [
        _result(canonical={"citation_count": {"openalex": 110}}),  # healthy → pass
        _result(canonical={}),                         # null citation_count → stub_null → fail
    ]}
    fixture_rows[0]["expected"]["snapshot"] = payload["results"][0]
    fixture_rows[1]["expected"]["snapshot"] = payload["results"][1]

    result = run_comparison(fixture_rows, payload)
    assert result.all_passed_tolerant is False
    assert result.rows[0].passed_tolerant is True
    assert result.rows[1].passed_tolerant is False


def test_snapshot_verdicts_aggregated_correctly():
    # Four scenarios:
    #   row_001 NONE                — snapshot == actual exactly
    #   row_002 AUTHOR_ONLY         — only canonical.authors[0].family differs
    #   row_003 AUTHOR_AND_SCORING  — only score_breakdown.title_sim differs
    #   row_004 UNEXPECTED          — only canonical.year differs (biblio)

    actuals = [
        _result(canonical={"authors": [{"family": "Smith"}]}),
        _result(canonical={"authors": [{"family": "Jones"}]}),
        _result(score_breakdown={"title_sim": 0.7}),
        _result(canonical={"year": 2021}),
    ]
    snaps = [
        actuals[0],                                                 # identical
        _result(canonical={"authors": [{"family": "Smith"}]}),       # diff at family
        _result(score_breakdown={"title_sim": 0.9}),                 # diff at title_sim
        _result(canonical={"year": 2020}),                           # diff at year
    ]
    fixture_rows = [
        _row("row_001", {}, {}, snaps[0]),
        _row("row_002", {}, {}, snaps[1]),
        _row("row_003", {}, {}, snaps[2]),
        _row("row_004", {}, {}, snaps[3]),
    ]
    result = run_comparison(fixture_rows, {"results": actuals})
    assert result.snapshot_verdict_counts == {
        "NONE": 1,
        "AUTHOR_ONLY": 1,
        "AUTHOR_AND_SCORING": 1,
        "UNEXPECTED": 1,
    }


def test_length_mismatch_raises():
    fixture_rows = [
        _row("row_001", {}, {}, _result()),
        _row("row_002", {}, {}, _result()),
        _row("row_003", {}, {}, _result()),
    ]
    payload = {"results": [_result(), _result()]}
    with pytest.raises(ValueError) as exc_info:
        run_comparison(fixture_rows, payload)
    assert "Length mismatch" in str(exc_info.value)


def test_row_order_preserved():
    # Deliberately non-alphabetical row_id ordering.
    fixture_rows = [
        _row("row_017", {}, {}, _result()),
        _row("row_003", {}, {}, _result()),
        _row("row_022", {}, {}, _result()),
    ]
    payload = {"results": [_result(), _result(), _result()]}
    # Align snapshots so verdicts are NONE — keeps the test focused on order.
    for i, fr in enumerate(fixture_rows):
        fr["expected"]["snapshot"] = payload["results"][i]

    result = run_comparison(fixture_rows, payload)
    assert [r.row_id for r in result.rows] == ["row_017", "row_003", "row_022"]
