"""Offline replay of a saved harness sidecar through the reduced gate.

Feeds a persisted ``report_*.raw.json`` sidecar (the per-row ``actual``
responses captured on a real nightly run) back through the *current*
three-tier comparator and prints the gate verdict. This is the deterministic,
network-free acceptance mechanism for the Phase GATE-OPT1 gate reduction:

    python -m harness.replay harness/reports/report_20260612T093002Z.raw.json

It reuses the exact production gate path (``run_comparison`` + the same
``all_passed_hard`` / ``all_passed_tolerant`` predicates as
``harness/validate.py``), so a PASS here means the live nightly gate would have
passed on that day's upstream conditions.

Exit code 0 on gate PASS, 1 on gate FAIL, 2 on a usage/IO error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tests.fixtures.loader import load_regression_30

from .comparator_runner import run_comparison


def replay(sidecar_path: Path) -> tuple[bool, "object"]:
    """Replay a sidecar through the comparator. Returns (gate_passed, comparison)."""
    sidecar = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    fixture = load_regression_30()
    fixture_rows = fixture["rows"]

    by_id = {row["row_id"]: row.get("actual", {}) for row in sidecar.get("rows", [])}
    missing = [fr["row_id"] for fr in fixture_rows if fr["row_id"] not in by_id]
    if missing:
        raise ValueError(f"sidecar missing actual responses for rows: {missing}")

    payload = {"results": [by_id[fr["row_id"]] for fr in fixture_rows]}
    comparison = run_comparison(fixture_rows, payload)
    gate_passed = comparison.all_passed_hard and comparison.all_passed_tolerant
    return gate_passed, comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.replay")
    parser.add_argument("sidecar", help="Path to a report_*.raw.json sidecar")
    args = parser.parse_args(argv)

    try:
        gate_passed, comparison = replay(Path(args.sidecar))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"replay failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    hard_fail = [r for r in comparison.rows if not r.passed_hard]
    tol_fail = [r for r in comparison.rows if not r.passed_tolerant]
    snap_diff = [r for r in comparison.rows if r.snapshot_verdict != "NONE"]

    print(f"Replay of {args.sidecar}")
    print(f"  gate: {'PASS' if gate_passed else 'FAIL'}")
    print(f"  hard failures:     {[r.row_id for r in hard_fail] or 'none'}")
    print(f"  tolerant failures: {[r.row_id for r in tol_fail] or 'none'}")
    print(
        f"  snapshot diffs (non-gating, informational): "
        f"{len(snap_diff)} rows -> {[r.row_id for r in snap_diff] or 'none'}"
    )
    return 0 if gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
