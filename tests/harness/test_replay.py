"""Acceptance replays for the Phase GATE-OPT1 gate reduction.

Replays saved nightly sidecars through the current reduced comparator and
asserts the gate verdict. The sidecars live under harness/reports/* which is
gitignored, so each test skips when its sidecar is absent (e.g. a clean CI
checkout) — on the DGX, where the captured sidecars exist, these lock in the
acceptance gates:

  * AG1 — the 06-12 FAIL day (OpenAlex 13x ReadTimeout + Crossref .c1 re-rank)
    now PASSes, with rows 010/013/014/015 surfacing as non-gating snapshot
    diffs rather than hard failures.
  * AG2 — a known-clean run still PASSes (regression-of-the-fix).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.replay import replay

REPORTS = Path(__file__).resolve().parents[2] / "harness" / "reports"
SIDECAR_0612 = REPORTS / "report_20260612T093002Z.raw.json"
SIDECAR_0609 = REPORTS / "report_20260609T065915Z.raw.json"


@pytest.mark.skipif(not SIDECAR_0612.exists(), reason="06-12 sidecar not present")
def test_ag1_replay_0612_passes_with_four_rows_as_snapshot_diffs():
    gate_passed, comparison = replay(SIDECAR_0612)
    assert gate_passed is True
    assert [r.row_id for r in comparison.rows if not r.passed_hard] == []
    assert [r.row_id for r in comparison.rows if not r.passed_tolerant] == []
    snap_diff_ids = {r.row_id for r in comparison.rows if r.snapshot_verdict != "NONE"}
    # The four ex-gating rows now surface as informational snapshot diffs.
    assert {"row_010", "row_013", "row_014", "row_015"} <= snap_diff_ids


@pytest.mark.skipif(not SIDECAR_0609.exists(), reason="06-09 sidecar not present")
def test_ag2_replay_clean_run_passes():
    gate_passed, _ = replay(SIDECAR_0609)
    assert gate_passed is True
