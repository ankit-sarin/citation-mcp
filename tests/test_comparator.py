"""Unit tests for tests/fixtures/comparator.py.

All tests use hand-built synthetic dicts so failures point at comparator
logic, not at regression_30.json drift.
"""

from __future__ import annotations

from tests.fixtures import comparator
from tests.fixtures.comparator import (
    CitationCountResult,
    DiscrepancyForbiddenResult,
    DiscrepancyRequiredResult,
    HardFieldResult,
    HardTierResult,
    SoftFailuresResult,
    SnapshotTierResult,
    ThreeTierResult,
    TolerantTierResult,
    classify_snapshot,
    compare_hard,
    compare_three_tier,
    compare_tolerant,
)


# =============================================================================
# Hard tier
# =============================================================================


def test_hard_all_fields_match():
    actual = {"a": 1, "b": "x", "c": True}
    expected_hard = {"a": 1, "b": "x", "c": True}
    result = compare_hard(actual, expected_hard)
    assert isinstance(result, HardTierResult)
    assert result.passed is True
    assert len(result.fields) == 3
    assert all(f.passed for f in result.fields)


def test_hard_one_field_mismatch():
    actual = {"a": 1, "b": "x"}
    expected_hard = {"a": 1, "b": "y"}
    result = compare_hard(actual, expected_hard)
    assert result.passed is False
    by_field = {f.field: f for f in result.fields}
    assert by_field["a"].passed is True
    assert by_field["b"].passed is False
    assert by_field["b"].actual == "x"
    assert by_field["b"].expected == "y"


def test_hard_missing_field_in_actual():
    actual = {"a": 1}
    expected_hard = {"a": 1, "b": "y"}
    result = compare_hard(actual, expected_hard)
    assert result.passed is False
    by_field = {f.field: f for f in result.fields}
    assert by_field["b"].actual is comparator._MISSING
    assert by_field["b"].passed is False


def test_hard_path_a_minimal_field_set():
    # Row_029 shape: adversarial Path A — only match_found + rejected_by.
    # After Phase 1.E.2.F.2, both fields use the extractor registry, so
    # actual must mirror the live response shape (verified flag +
    # score_breakdown.rejected_by), not the flat-key form.
    expected_hard = {
        "match_found": False,
        "rejected_by": "first_author_mismatch",
    }
    actual = {
        "verified": False,
        "score_breakdown": {"rejected_by": "first_author_mismatch"},
        "confidence": 0.0,
        "match_quality": "low",
    }
    result = compare_hard(actual, expected_hard)
    assert result.passed is True
    assert {f.field for f in result.fields} == {"match_found", "rejected_by"}


def test_hard_actual_has_extra_fields():
    expected_hard = {"x": 1}
    actual = {"x": 1, "y": 2, "z": 3, "score_breakdown": {"deep": "nested"}}
    result = compare_hard(actual, expected_hard)
    assert result.passed is True
    assert len(result.fields) == 1
    assert result.fields[0].field == "x"


def test_hard_empty_expected_passes_vacuously():
    expected_hard: dict = {}
    actual = {"anything": "at all"}
    result = compare_hard(actual, expected_hard)
    assert result.passed is True
    assert result.fields == []


def test_hard_extractor_match_found_from_verified():
    """match_found is derived from response.verified."""
    expected_hard = {"match_found": True}
    actual = {"verified": True}
    result = compare_hard(actual, expected_hard)
    assert result.passed is True


def test_hard_extractor_doi_resolved_from_canonical():
    """doi_resolved is at canonical.doi, not top-level."""
    expected_hard = {"doi_resolved": "10.1234/example"}
    actual = {"canonical": {"doi": "10.1234/example"}}
    result = compare_hard(actual, expected_hard)
    assert result.passed is True


def test_hard_extractor_first_author_surname_handles_empty_authors():
    """Empty authors list returns None for first_author_surname."""
    expected_hard = {"first_author_surname": None}
    actual = {"canonical": {"authors": []}}
    result = compare_hard(actual, expected_hard)
    assert result.passed is True


def test_hard_extractor_rejected_by_from_score_breakdown():
    """rejected_by is at score_breakdown.rejected_by."""
    expected_hard = {"rejected_by": "title_similarity_below_floor"}
    actual = {"score_breakdown": {"rejected_by": "title_similarity_below_floor"}}
    result = compare_hard(actual, expected_hard)
    assert result.passed is True


# =============================================================================
# Tolerant tier — citation_count
#
# The longitudinal ±tolerance_pct band is no longer gating (organic upstream
# citation drift was firing false-alarm gate failures — see row_018 on
# 2026-06-01). Gating reduced to structural anomalies: actual==null while
# baseline was a positive count, or actual==0 while baseline was positive.
# The order-of-magnitude tripwire is opt-in via
# CITATION_COUNT_ORDER_OF_MAGNITUDE_GUARD. delta_pct is retained as
# informational telemetry.
# =============================================================================


def _tol(**kwargs):
    """Build an expected_tolerant dict from kwargs."""
    return kwargs


def test_tolerant_citation_count_within_band_passes():
    expected = _tol(citation_count={"value": 100, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 110}}
    result = compare_tolerant(actual, expected)
    cc = result.citation_count
    assert isinstance(cc, CitationCountResult)
    assert cc.passed is True
    assert cc.anomaly is None
    assert cc.actual == 110
    assert cc.delta_pct == 10.0
    assert result.passed is True


def test_tolerant_citation_count_low_count_plus_one_does_not_gate():
    """row_018 regression: baseline 3, actual 4 → 33% delta but not a stub.
    Must pass under the structural guard. This is the case that fired the
    false-alarm gate failure on 2026-06-01."""
    expected = _tol(citation_count={"value": 3, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 4}}
    result = compare_tolerant(actual, expected)
    cc = result.citation_count
    assert cc.passed is True
    assert cc.anomaly is None
    assert cc.actual == 4
    assert cc.delta_pct is not None and abs(cc.delta_pct - 33.33) < 0.01


def test_tolerant_citation_count_far_above_band_still_passes():
    """A 100→200 drift used to fail the band. Under the structural guard it
    passes — not null, not zero, and below the (disabled-by-default) 10×
    tripwire."""
    expected = _tol(citation_count={"value": 100, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 200}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is True
    assert result.citation_count.anomaly is None
    assert result.citation_count.delta_pct == 100.0


def test_tolerant_citation_count_far_below_band_still_passes():
    """A 100→70 drift used to fail the band. Under the structural guard it
    passes — not null, not zero."""
    expected = _tol(citation_count={"value": 100, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 70}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is True
    assert result.citation_count.anomaly is None
    assert result.citation_count.delta_pct == 30.0


def test_tolerant_citation_count_zero_baseline_zero_actual_passes():
    expected = _tol(citation_count={"value": 0, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 0}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is True
    assert result.citation_count.anomaly is None
    assert result.citation_count.delta_pct is None


def test_tolerant_citation_count_zero_baseline_positive_actual_passes():
    """baseline=0 + actual=5 → organic citation growth from an uncited paper.
    Not a stub (the structural rule keys on baseline>0). Passes."""
    expected = _tol(citation_count={"value": 0, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 5}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is True
    assert result.citation_count.anomaly is None


def test_tolerant_citation_count_stub_null_gates():
    """actual==null while baseline was positive → resolver returned a stub."""
    expected = _tol(citation_count={"value": 100, "tolerance_pct": 20})
    actual = {"canonical": {}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is False
    assert result.citation_count.anomaly == "stub_null"
    assert result.citation_count.actual is None
    assert result.citation_count.delta_pct is None


def test_tolerant_citation_count_stub_zero_gates():
    """actual==0 while baseline was positive → resolver returned a stub."""
    expected = _tol(citation_count={"value": 3, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 0}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is False
    assert result.citation_count.anomaly == "stub_zero"
    assert result.citation_count.actual == 0


def test_tolerant_citation_count_order_of_magnitude_off_by_default():
    """CITATION_COUNT_ORDER_OF_MAGNITUDE_GUARD defaults to False — even a
    1000× divergence passes the structural guard. Forces a deliberate
    opt-in for the tripwire."""
    assert comparator.CITATION_COUNT_ORDER_OF_MAGNITUDE_GUARD is False
    expected = _tol(citation_count={"value": 3, "tolerance_pct": 20})
    actual = {"canonical": {"citation_count": 3000}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is True
    assert result.citation_count.anomaly is None


def test_tolerant_citation_count_order_of_magnitude_fires_when_enabled(monkeypatch):
    """When the flag is enabled, actual ≥ 10×baseline gates."""
    monkeypatch.setattr(
        comparator, "CITATION_COUNT_ORDER_OF_MAGNITUDE_GUARD", True
    )

    # Exactly 10× → fires (≥ is inclusive).
    expected = _tol(citation_count={"value": 3, "tolerance_pct": 20})
    r_high = compare_tolerant({"canonical": {"citation_count": 30}}, expected)
    assert r_high.citation_count.passed is False
    assert r_high.citation_count.anomaly == "order_of_magnitude"

    # Exactly 1/10× → fires.
    expected_hi = _tol(citation_count={"value": 30, "tolerance_pct": 20})
    r_low = compare_tolerant({"canonical": {"citation_count": 3}}, expected_hi)
    assert r_low.citation_count.passed is False
    assert r_low.citation_count.anomaly == "order_of_magnitude"

    # Just under 10× → does NOT fire (29 < 3×10).
    r_under = compare_tolerant({"canonical": {"citation_count": 29}}, expected)
    assert r_under.citation_count.passed is True
    assert r_under.citation_count.anomaly is None


def test_tolerant_citation_count_dict_actual_uses_max():
    """Live responses return canonical.citation_count as a per-source dict.
    Comparator must aggregate via max() before evaluation."""
    expected = {"citation_count": {"value": 21, "tolerance_pct": 20}}
    actual = {"canonical": {"citation_count": {"openalex": 20, "semantic_scholar": 21}}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count is not None
    assert result.citation_count.passed is True
    assert result.citation_count.actual == 21          # max of {20, 21}
    assert result.citation_count.delta_pct == 0.0


def test_tolerant_citation_count_empty_dict_actual_gates():
    """Empty dict for citation_count → no value present → stub_null gate."""
    expected = {"citation_count": {"value": 21, "tolerance_pct": 20}}
    actual = {"canonical": {"citation_count": {}}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is False
    assert result.citation_count.anomaly == "stub_null"
    assert result.citation_count.actual is None


def test_tolerant_citation_count_dict_all_none_values_gates():
    """All-None per-source values (e.g., all sources failed) → stub_null."""
    expected = {"citation_count": {"value": 21, "tolerance_pct": 20}}
    actual = {"canonical": {"citation_count": {"openalex": None, "semantic_scholar": None}}}
    result = compare_tolerant(actual, expected)
    assert result.citation_count.passed is False
    assert result.citation_count.anomaly == "stub_null"
    assert result.citation_count.actual is None


# =============================================================================
# Tolerant tier — discrepancies
# =============================================================================


def test_tolerant_discrepancies_required_all_present():
    expected = _tol(
        discrepancies_required=[{"rule": "doi_mismatch", "field": "doi"}]
    )
    actual = {
        "discrepancies": [
            {"rule": "doi_mismatch", "field": "doi", "extra": "ignored"}
        ]
    }
    result = compare_tolerant(actual, expected)
    dr = result.discrepancies_required
    assert isinstance(dr, DiscrepancyRequiredResult)
    assert dr.passed is True
    assert dr.missing == []


def test_tolerant_discrepancies_required_one_missing():
    expected = _tol(
        discrepancies_required=[
            {"rule": "doi_mismatch", "field": "doi"},
            {"rule": "year_diff", "field": "year"},
        ]
    )
    actual = {"discrepancies": [{"rule": "doi_mismatch", "field": "doi"}]}
    result = compare_tolerant(actual, expected)
    assert result.discrepancies_required.passed is False
    assert result.discrepancies_required.missing == [("year_diff", "year")]


def test_tolerant_discrepancies_required_empty_list_passes():
    # Phase 2.E adversarial pattern: deliberately empty required list.
    expected = _tol(discrepancies_required=[])
    actual = {
        "discrepancies": [
            {"rule": "anything", "field": "at_all"},
            {"rule": "other", "field": "stuff"},
        ]
    }
    result = compare_tolerant(actual, expected)
    assert result.discrepancies_required.passed is True
    assert result.discrepancies_required.missing == []


def test_tolerant_discrepancies_forbidden_none_present():
    expected = _tol(discrepancies_forbidden=[{"rule": "X", "field": "Y"}])
    actual = {"discrepancies": []}
    result = compare_tolerant(actual, expected)
    df = result.discrepancies_forbidden
    assert isinstance(df, DiscrepancyForbiddenResult)
    assert df.passed is True
    assert df.present == []


def test_tolerant_discrepancies_forbidden_one_present():
    expected = _tol(discrepancies_forbidden=[{"rule": "X", "field": "Y"}])
    actual = {"discrepancies": [{"rule": "X", "field": "Y"}]}
    result = compare_tolerant(actual, expected)
    assert result.discrepancies_forbidden.passed is False
    assert result.discrepancies_forbidden.present == [("X", "Y")]


# =============================================================================
# Tolerant tier — soft_failures
# =============================================================================


def test_tolerant_soft_failures_all_allowed():
    expected = _tol(allowed_soft_failures=["arxiv"])
    actual = {"databases_failed": ["arxiv"]}
    result = compare_tolerant(actual, expected)
    sf = result.soft_failures
    assert isinstance(sf, SoftFailuresResult)
    assert sf.passed is True
    assert sf.disallowed == []


def test_tolerant_soft_failures_disallowed_present():
    expected = _tol(allowed_soft_failures=["arxiv"])
    actual = {"databases_failed": ["arxiv", "crossref"]}
    result = compare_tolerant(actual, expected)
    assert result.soft_failures.passed is False
    assert result.soft_failures.disallowed == ["crossref"]


def test_tolerant_empty_databases_failed():
    expected = _tol(allowed_soft_failures=["arxiv"])
    actual = {"databases_failed": []}
    result = compare_tolerant(actual, expected)
    assert result.soft_failures.passed is True
    assert result.soft_failures.disallowed == []


def test_tolerant_soft_failure_does_not_affect_gate_verdict():
    """Per Phase 1.E.2.F.3 recalibration: soft_failures sub-check is computed
    and reported, but does NOT contribute to TolerantTierResult.passed
    (the tolerant gate verdict). A disallowed upstream-DB failure must
    leave the tolerant gate PASSING when the gate-relevant sub-checks
    (citation_count band, discrepancies_required, discrepancies_forbidden)
    all pass."""
    expected = _tol(
        citation_count={"value": 100, "tolerance_pct": 20},
        discrepancies_required=[],
        discrepancies_forbidden=[],
        allowed_soft_failures=["arxiv"],
    )
    actual = {
        "canonical": {"citation_count": 105},     # not a stub
        "discrepancies": [],                       # required empty, forbidden absent
        "databases_failed": ["semantic_scholar"],  # disallowed → soft_failures.passed=False
    }
    result = compare_tolerant(actual, expected)
    assert result.soft_failures is not None
    assert result.soft_failures.passed is False           # sub-check fails
    assert result.soft_failures.disallowed == ["semantic_scholar"]
    assert result.citation_count.passed is True
    assert result.discrepancies_required.passed is True
    assert result.discrepancies_forbidden.passed is True
    # The gate verdict on the tolerant tier ignores soft_failures.
    assert result.passed is True


# =============================================================================
# Snapshot tier
# =============================================================================


def test_snapshot_identical_returns_NONE():
    snap = {"a": 1, "canonical": {"year": 2024}}
    result = classify_snapshot(actual=snap, expected_snapshot=snap)
    assert isinstance(result, SnapshotTierResult)
    assert result.verdict == "NONE"
    assert result.diffs == []
    assert result.buckets == {
        "author": 0,
        "scoring": 0,
        "bibliographic": 0,
        "other": 0,
    }


def test_snapshot_author_diff_returns_AUTHOR_ONLY():
    expected_snap = {"canonical": {"authors": [{"family": "Smith"}]}}
    actual = {"canonical": {"authors": [{"family": "Jones"}]}}
    result = classify_snapshot(actual, expected_snap)
    assert result.verdict == "AUTHOR_ONLY"
    assert result.buckets["author"] == 1
    assert result.buckets["scoring"] == 0
    assert result.buckets["bibliographic"] == 0
    assert any("canonical.authors[0].family" == p for p, _, _ in result.diffs)


def test_snapshot_scoring_diff_returns_AUTHOR_AND_SCORING():
    expected_snap = {"score_breakdown": {"title_sim": 0.9}}
    actual = {"score_breakdown": {"title_sim": 0.7}}
    result = classify_snapshot(actual, expected_snap)
    assert result.verdict == "AUTHOR_AND_SCORING"
    assert result.buckets["scoring"] == 1
    assert result.buckets["author"] == 0


def test_snapshot_biblio_diff_returns_UNEXPECTED():
    expected_snap = {"canonical": {"year": 2020}}
    actual = {"canonical": {"year": 2021}}
    result = classify_snapshot(actual, expected_snap)
    assert result.verdict == "UNEXPECTED"
    assert result.buckets["bibliographic"] == 1


def test_snapshot_warnings_diff_ignored():
    """warnings array is stripped before diff — different warnings don't
    change the verdict."""
    expected = {"canonical": {"year": 2024}, "warnings": []}
    actual = {"canonical": {"year": 2024}, "warnings": [{"source": "cache"}]}
    result = classify_snapshot(actual, expected)
    assert result.verdict == "NONE"
    assert result.diffs == []


def test_snapshot_discrepancies_diff_routes_to_scoring():
    # Decision 2A: discrepancies diffs continue to route as scoring paths.
    # This test pins that semantic so future refactors don't silently
    # introduce a separate DISCREPANCIES_ONLY verdict.
    expected_snap = {
        "discrepancies": [{"rule": "rule_old", "field": "doi"}]
    }
    actual = {
        "discrepancies": [{"rule": "rule_new", "field": "doi"}]
    }
    result = classify_snapshot(actual, expected_snap)
    assert result.verdict == "AUTHOR_AND_SCORING"
    assert result.buckets["scoring"] >= 1
    assert result.buckets["author"] == 0
    assert result.buckets["bibliographic"] == 0
    assert result.buckets["other"] == 0


# =============================================================================
# Three-tier compose
# =============================================================================


def test_three_tier_compose_uses_subresults():
    # Snapshot is structurally aligned with actual EXCEPT for the one
    # author-family field — so the only snapshot diff is author-tier.
    # Hard tier has a year mismatch (2024 expected, 2023 actual).
    # Tolerant tier (citation_count within band) passes.
    snapshot_baseline = {
        "match_found": True,
        "year": 2023,
        "canonical": {
            "citation_count": 105,
            "authors": [{"family": "Smith"}],
        },
        "match_quality": "high",
        "discrepancies": [],
        "databases_failed": [],
    }
    expected_block = {
        "hard": {
            "match_found": True,
            "year": 2024,  # hard mismatch vs actual.year == 2023
        },
        "tolerant": {
            "citation_count": {"value": 100, "tolerance_pct": 20},
            "discrepancies_required": [],
            "allowed_soft_failures": [],
        },
        "snapshot": snapshot_baseline,
    }
    actual = {
        "match_found": True,
        "year": 2023,
        "canonical": {
            "citation_count": 105,
            "authors": [{"family": "Jones"}],  # only diff vs snapshot
        },
        "match_quality": "high",
        "discrepancies": [],
        "databases_failed": [],
    }
    result = compare_three_tier(actual, expected_block)
    assert isinstance(result, ThreeTierResult)
    assert result.passed_hard is False
    assert result.passed_tolerant is True
    assert result.snapshot_verdict == "AUTHOR_ONLY"
    # Confirm sub-results are present and reused, not re-implemented:
    assert isinstance(result.hard, HardTierResult)
    assert isinstance(result.tolerant, TolerantTierResult)
    assert isinstance(result.snapshot, SnapshotTierResult)
    assert result.tolerant.citation_count.delta_pct == 5.0
