"""Apply the three-tier comparator across a bulkVerifyCitations response.

Pure transformation — no transport awareness. Takes the fixture rows from
regression_30.json and the parsed bulk payload from the harness's MCP
client, and returns a structured pass/fail breakdown plus snapshot-verdict
distribution.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.fixtures.comparator import (
    HardTierResult,
    SnapshotTierResult,
    TolerantTierResult,
    compare_three_tier,
)


@dataclass
class RowComparisonResult:
    row_id: str
    category: str
    hard: HardTierResult
    tolerant: TolerantTierResult
    snapshot: SnapshotTierResult

    @property
    def passed_hard(self) -> bool:
        return self.hard.passed

    @property
    def passed_tolerant(self) -> bool:
        return self.tolerant.passed

    @property
    def snapshot_verdict(self) -> str:
        return self.snapshot.verdict


@dataclass
class BulkComparisonResult:
    rows: list[RowComparisonResult]

    @property
    def all_passed_hard(self) -> bool:
        return all(r.passed_hard for r in self.rows)

    @property
    def all_passed_tolerant(self) -> bool:
        return all(r.passed_tolerant for r in self.rows)

    @property
    def snapshot_verdict_counts(self) -> dict[str, int]:
        counts = {
            "NONE": 0,
            "AUTHOR_ONLY": 0,
            "AUTHOR_AND_SCORING": 0,
            "UNEXPECTED": 0,
        }
        for r in self.rows:
            counts[r.snapshot_verdict] = counts.get(r.snapshot_verdict, 0) + 1
        return counts


def run_comparison(
    fixture_rows: list[dict],
    bulk_payload: dict,
) -> BulkComparisonResult:
    """Apply three-tier comparator to each row.

    Assumes fixture_rows and bulk_payload["results"] are in the same order
    and have the same length (loader rule 2 + bulk endpoint preserves input
    order).
    """
    actual_results = bulk_payload.get("results", [])
    if len(actual_results) != len(fixture_rows):
        raise ValueError(
            f"Length mismatch: {len(actual_results)} results, "
            f"{len(fixture_rows)} fixture rows"
        )

    rows: list[RowComparisonResult] = []
    for fixture_row, actual in zip(fixture_rows, actual_results):
        tier = compare_three_tier(actual=actual, expected=fixture_row["expected"])
        rows.append(
            RowComparisonResult(
                row_id=fixture_row["row_id"],
                category=fixture_row["category"],
                hard=tier.hard,
                tolerant=tier.tolerant,
                snapshot=tier.snapshot,
            )
        )
    return BulkComparisonResult(rows=rows)
