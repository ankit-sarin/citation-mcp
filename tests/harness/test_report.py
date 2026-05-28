"""Unit tests for harness.report."""

from __future__ import annotations

import re

from harness.comparator_runner import BulkComparisonResult, RowComparisonResult
from harness.report import ValidationRunInputs, build_report
from tests.fixtures.comparator import (
    CitationCountResult,
    HardFieldResult,
    HardTierResult,
    SnapshotTierResult,
    TolerantTierResult,
)


def _hard_pass(*fields: str) -> HardTierResult:
    return HardTierResult(
        fields=[
            HardFieldResult(field=f, actual="x", expected="x", passed=True)
            for f in fields
        ] or [HardFieldResult(field="match_found", actual=True, expected=True, passed=True)]
    )


def _hard_fail(field: str, actual, expected) -> HardTierResult:
    return HardTierResult(
        fields=[
            HardFieldResult(
                field=field, actual=actual, expected=expected, passed=False
            )
        ]
    )


def _tolerant_pass() -> TolerantTierResult:
    return TolerantTierResult()


def _tolerant_cc_fail(actual: int, expected: int) -> TolerantTierResult:
    return TolerantTierResult(
        citation_count=CitationCountResult(
            actual=actual,
            expected_value=expected,
            tolerance_pct=20,
            delta_pct=abs(actual - expected) / expected * 100,
            passed=False,
        )
    )


def _snapshot_none() -> SnapshotTierResult:
    return SnapshotTierResult(
        verdict="NONE",
        diffs=[],
        buckets={"author": 0, "scoring": 0, "bibliographic": 0, "other": 0},
    )


def _snapshot(verdict: str, diff_paths: list[str]) -> SnapshotTierResult:
    return SnapshotTierResult(
        verdict=verdict,
        diffs=[(p, "old", "new") for p in diff_paths],
        buckets={"author": 0, "scoring": 0, "bibliographic": 0, "other": 0},
    )


def _row(row_id: str, hard: HardTierResult, tol: TolerantTierResult, snap: SnapshotTierResult,
         category: str = "identifier_decisive") -> RowComparisonResult:
    return RowComparisonResult(
        row_id=row_id, category=category, hard=hard, tolerant=tol, snapshot=snap
    )


def _make_inputs(
    rows: list[RowComparisonResult],
    cold_wall: float = 5.0,
    warm_cache_hits: int | None = None,
    n_citations: int | None = None,
) -> ValidationRunInputs:
    n = n_citations if n_citations is not None else len(rows)
    return ValidationRunInputs(
        fixture_version="1.0",
        server_url="https://test.example/mcp",
        client_id_suffix="ABCD1234",
        timestamp_utc_iso="2026-05-28T00:00:00+00:00",
        cold_wall_clock_seconds=cold_wall,
        cold_server_elapsed_seconds=cold_wall * 0.9,
        cold_cache_hits=0,
        cold_refreshed_token=False,
        warm_wall_clock_seconds=1.5,
        warm_server_elapsed_seconds=1.2,
        warm_cache_hits=warm_cache_hits if warm_cache_hits is not None else n,
        warm_refreshed_token=False,
        comparison=BulkComparisonResult(rows=rows),
        n_citations=n,
    )


def _all_pass_rows(n: int = 3) -> list[RowComparisonResult]:
    return [
        _row(f"row_{i:03d}", _hard_pass("match_found"), _tolerant_pass(), _snapshot_none())
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------


def test_report_contains_required_sections():
    text = build_report(_make_inputs(_all_pass_rows()))
    for header in (
        "# citation-mcp validation report",
        "## Overall result",
        "## Latency",
        "## Snapshot verdict distribution",
        "## Per-row results",
        "## Needs attention",
    ):
        assert header in text, f"missing header: {header}"


def test_overall_pass_when_all_gates_clear():
    text = build_report(_make_inputs(_all_pass_rows(), cold_wall=5.0))
    assert "**v1.0 gate:** PASS" in text
    assert "**Hard tier:** PASS" in text
    assert "**Tolerant tier:** PASS" in text


def test_overall_fail_when_hard_tier_fails():
    rows = _all_pass_rows(2)
    rows.append(
        _row("row_003", _hard_fail("year", 2023, 2024), _tolerant_pass(), _snapshot_none())
    )
    text = build_report(_make_inputs(rows, cold_wall=5.0))
    assert "**v1.0 gate:** FAIL" in text
    assert "**Hard tier:** FAIL" in text


def test_overall_fail_when_cold_latency_exceeds_gate():
    # Gate is 20.0s after Phase 1.E.2.F.2. Use 21.0s to exceed it.
    text = build_report(_make_inputs(_all_pass_rows(), cold_wall=21.0))
    assert "**v1.0 gate:** FAIL" in text
    assert re.search(r"\*\*Cold-cache latency:\*\* 21\.00s — FAIL", text)


def test_needs_attention_lists_hard_failures():
    rows = _all_pass_rows(1)
    rows.append(
        _row(
            "row_009",
            _hard_fail("confidence", 0.7, 0.9),
            _tolerant_pass(),
            _snapshot_none(),
        )
    )
    text = build_report(_make_inputs(rows))
    # Locate the Needs-attention section and confirm it carries the
    # row id, the field name, and both values.
    attn = text.split("## Needs attention", 1)[1]
    assert "row_009" in attn
    assert "confidence" in attn
    assert "0.7" in attn
    assert "0.9" in attn


def test_needs_attention_lists_snapshot_informational():
    rows = _all_pass_rows(2)
    rows.append(
        _row(
            "row_017",
            _hard_pass("match_found"),
            _tolerant_pass(),
            _snapshot("AUTHOR_AND_SCORING", ["score_breakdown.title_sim"]),
        )
    )
    text = build_report(_make_inputs(rows))
    attn = text.split("## Needs attention", 1)[1]
    assert "row_017" in attn
    assert "AUTHOR_AND_SCORING" in attn
    assert "score_breakdown.title_sim" in attn
    # Gate must still be PASS — snapshot non-NONE is informational only.
    assert "**v1.0 gate:** PASS" in text


def test_cache_hit_anomaly_reported():
    text = build_report(_make_inputs(_all_pass_rows(30), warm_cache_hits=25, n_citations=30))
    attn = text.split("## Needs attention", 1)[1]
    assert "Cache hit anomaly" in attn
    assert "25" in attn
    assert "30" in attn
    # Soft warning — gate stays PASS.
    assert "**v1.0 gate:** PASS" in text
