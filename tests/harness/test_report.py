"""Unit tests for harness.report."""

from __future__ import annotations

import json
import re

from harness.comparator_runner import BulkComparisonResult, RowComparisonResult
from harness.report import ValidationRunInputs, build_report, write_report
from tests.fixtures.comparator import (
    CitationCountResult,
    HardFieldResult,
    HardTierResult,
    SnapshotTierResult,
    SoftFailuresResult,
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


def _tolerant_cc_fail(actual, expected: int, anomaly: str = "stub_null") -> TolerantTierResult:
    """Build a failing citation_count tolerant result under the structural
    guard. Defaults to stub_null (the row_018-class anomaly is non-gating
    by design — band drift no longer fails the tier)."""
    delta_pct = (
        abs(actual - expected) / expected * 100
        if (actual is not None and expected) else None
    )
    return TolerantTierResult(
        citation_count=CitationCountResult(
            actual=actual,
            expected_value=expected,
            tolerance_pct=20,
            delta_pct=delta_pct,
            passed=False,
            anomaly=anomaly,
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


def test_high_latency_does_not_fail_gate():
    """Phase 1.E.2.F.3: latency is informational, not a gate criterion.
    A 27s cold pass must still produce v1.0 gate PASS so transient
    upstream-DB slowdowns don't fire false-alarm digests."""
    text = build_report(_make_inputs(_all_pass_rows(), cold_wall=27.0))
    assert "**v1.0 gate:** PASS" in text
    # Latency line is informational (italic) — no "FAIL", no "gate:" threshold.
    assert "27.00s" in text
    assert "informational" in text
    assert "FAIL" not in text.split("## Latency", 1)[0]  # no FAIL in Overall section
    assert "gate: <" not in text  # no threshold framing


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


def test_soft_failure_does_not_fail_gate():
    """Phase 1.E.2.F.3: upstream-DB unavailability is informational only.
    A row with a disallowed databases_failed entry must still produce
    v1.0 gate PASS, and the failure must appear in the informational
    upstream-availability subsection of Needs attention."""
    rows = _all_pass_rows(2)
    soft_fail_tolerant = TolerantTierResult(
        soft_failures=SoftFailuresResult(
            allowed=["arxiv"],
            actual_failures=["semantic_scholar"],
            disallowed=["semantic_scholar"],
            passed=False,
        )
    )
    rows.append(
        _row(
            "row_009",
            _hard_pass("match_found"),
            soft_fail_tolerant,
            _snapshot_none(),
        )
    )
    text = build_report(_make_inputs(rows))
    # Gate must still be PASS — soft_failures is no longer gate-relevant.
    assert "**v1.0 gate:** PASS" in text
    # Informational subsection must appear in Needs attention.
    attn = text.split("## Needs attention", 1)[1]
    assert "Upstream database availability" in attn
    assert "informational" in attn
    assert "semantic_scholar" in attn
    assert "row_009" in attn


def test_cache_hit_anomaly_reported():
    text = build_report(_make_inputs(_all_pass_rows(30), warm_cache_hits=25, n_citations=30))
    attn = text.split("## Needs attention", 1)[1]
    assert "Cache hit anomaly" in attn
    assert "25" in attn
    assert "30" in attn
    # Soft warning — gate stays PASS.
    assert "**v1.0 gate:** PASS" in text


def test_write_report_persists_raw_responses(tmp_path):
    """write_report must emit a .raw.json sidecar carrying the per-row
    actual responses. Triage of a failing row should not require re-running
    the harness — the row_018 incident on 2026-06-01 had to infer actual
    values from diff-paths alone because nothing on disk carried them."""
    row = _row(
        "row_018",
        _hard_pass("match_found"),
        _tolerant_pass(),
        _snapshot("UNEXPECTED", ["canonical.citation_count.openalex"]),
        category="title_only",
    )
    # Plumb actual into the row — this is the value that should survive
    # to disk verbatim.
    row.actual = {
        "verified": True,
        "canonical": {
            "doi": "10.1007/s11548-024-03178-z",
            "citation_count": {"openalex": 4, "semantic_scholar": 2},
        },
    }

    inputs = _make_inputs([row])
    md_path = write_report(inputs, tmp_path)

    raw_path = md_path.with_name(md_path.stem + ".raw.json")
    assert raw_path.exists(), f"raw sidecar missing at {raw_path}"

    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert payload["fixture_version"] == "1.0"
    assert len(payload["rows"]) == 1
    persisted = payload["rows"][0]
    assert persisted["row_id"] == "row_018"
    # The actual values that previously had to be inferred:
    assert persisted["actual"]["canonical"]["citation_count"]["openalex"] == 4
    assert persisted["actual"]["canonical"]["citation_count"]["semantic_scholar"] == 2
    # Snapshot diffs survive as well (path + old + new).
    snap = persisted["snapshot"]
    assert snap["verdict"] == "UNEXPECTED"
    assert any(
        d[0] == "canonical.citation_count.openalex" for d in snap["diffs"]
    )
