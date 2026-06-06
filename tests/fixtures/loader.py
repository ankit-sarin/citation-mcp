"""Loader + validator for tests/fixtures/regression_30.json.

`load_regression_30()` reads the fixture and runs the 12 Phase 2.A structural
validation rules. Any rule violation raises `FixtureValidationError`. The
fixture path is resolved relative to this file, so the loader works
regardless of pytest's working directory.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


FIXTURE_PATH = Path(__file__).parent / "regression_30.json"

VALID_DISCREPANCY_RULES = {
    "highest_authority_db",
    "earliest_non_null_year",
    "report_both_diff_gt_20pct",
}

EXPECTED_CATEGORY_COUNTS = {
    "identifier_decisive": 8,
    "bibliographic_only": 6,
    "title_only": 4,
    "known_discrepancy": 4,
    "pubmed_native_edge": 1,
    "retracted": 2,
    "corrigendum": 1,
    "arxiv_opt_in": 2,
    "adversarial": 2,
}

HAND_CURATED_IDS = {"row_024", "row_025", "row_027", "row_028", "row_029", "row_030"}
EE_SOURCE_RE = re.compile(r"^EE-\d+$")

ROW_029_ALLOWED_REJECTED_BY = {
    "year_off_by_more_than_one",
    "first_author_mismatch_low_title_sim",
    "title_similarity_below_floor",
}


class FixtureValidationError(Exception):
    """Raised when the regression fixture fails one or more validation rules."""


def _fail(rule_num: int, detail: str) -> None:
    raise FixtureValidationError(f"Rule {rule_num} failed: {detail}")


def _validate(fixture: dict[str, Any]) -> None:
    rows = fixture.get("rows")
    if not isinstance(rows, list):
        raise FixtureValidationError("fixture has no 'rows' list")

    # Rule 1: exactly 30 rows.
    if len(rows) != 30:
        _fail(1, f"expected 30 rows, got {len(rows)}")

    # Rule 2: row_001..row_030, unique, contiguous, in order.
    expected_ids = [f"row_{i:03d}" for i in range(1, 31)]
    actual_ids = [r.get("row_id") for r in rows]
    if actual_ids != expected_ids:
        _fail(2, f"row_id sequence wrong: {actual_ids}")

    # Rule 3: category counts.
    actual_counts = dict(Counter(r.get("category") for r in rows))
    if actual_counts != EXPECTED_CATEGORY_COUNTS:
        _fail(3, f"category counts {actual_counts} != expected {EXPECTED_CATEGORY_COUNTS}")

    # Rule 4: skipped (provenance-only, verified at Phase 2.C.2).

    # Rule 5: source value validity.
    for r in rows:
        rid = r["row_id"]
        src = r.get("source")
        if rid in HAND_CURATED_IDS:
            if src != "hand_curated":
                _fail(5, f"{rid}: source={src!r}, expected 'hand_curated'")
        else:
            if not isinstance(src, str) or not EE_SOURCE_RE.match(src):
                _fail(5, f"{rid}: source={src!r} does not match ^EE-\\d+$")

    # Rule 6: adversarial row constraints.
    r029 = next(r for r in rows if r["row_id"] == "row_029")
    r029_hard = r029["expected"]["hard"]
    if r029_hard.get("match_found") is not False:
        _fail(6, f"row_029: match_found={r029_hard.get('match_found')!r}, expected false")
    if r029_hard.get("rejected_by") not in ROW_029_ALLOWED_REJECTED_BY:
        _fail(6, f"row_029: rejected_by={r029_hard.get('rejected_by')!r} not in {sorted(ROW_029_ALLOWED_REJECTED_BY)}")

    r030 = next(r for r in rows if r["row_id"] == "row_030")
    r030_hard = r030["expected"]["hard"]
    mf = r030_hard.get("match_found")
    if mf is False:
        # Path A: must also have valid rejected_by.
        if r030_hard.get("rejected_by") not in ROW_029_ALLOWED_REJECTED_BY:
            _fail(6, f"row_030 Path A: rejected_by={r030_hard.get('rejected_by')!r} not in allowed set")
    elif mf is True:
        # Path B: match_quality must be 'low' or 'medium' (Phase 2.E.3 accepted medium).
        # Sourced from expected.snapshot (informational) rather than expected.hard:
        # match_quality was demoted out of row_030's gated hard set after Crossref
        # reshuffled the row onto a third Tukra-chapter edition (edition-drift
        # demotion). The structural low/medium invariant is preserved here.
        mq = r030["expected"]["snapshot"].get("match_quality")
        if mq not in {"low", "medium"}:
            _fail(6, f"row_030 Path B: match_quality={mq!r}, expected 'low' or 'medium'")
    else:
        _fail(6, f"row_030: match_found={mf!r}, expected true or false")

    for arow in (r029, r030):
        inp = arow.get("input", {})
        if not any(inp.get(k) for k in ("doi", "pmid", "title")):
            _fail(6, f"{arow['row_id']}: input missing all of doi/pmid/title")

    # Rule 7: non-adversarial rows have match_found=true + populated first_author_surname/year/match_quality.
    for r in rows:
        if r["category"] == "adversarial":
            continue
        rid = r["row_id"]
        h = r["expected"]["hard"]
        if h.get("match_found") is not True:
            _fail(7, f"{rid}: match_found={h.get('match_found')!r}, expected true")
        if not h.get("first_author_surname"):
            _fail(7, f"{rid}: first_author_surname empty")
        if h.get("year") is None:
            _fail(7, f"{rid}: year is null")
        if h.get("match_quality") is None:
            _fail(7, f"{rid}: match_quality is null")

    # Rule 8: arXiv rows.
    for rid in ("row_027", "row_028"):
        r = next(x for x in rows if x["row_id"] == rid)
        inp = r.get("input", {})
        if not inp.get("arxiv_id"):
            _fail(8, f"{rid}: missing input.arxiv_id")
        if not any(inp.get(k) for k in ("doi", "pmid", "title")):
            _fail(8, f"{rid}: missing all of doi/pmid/title")
        if "arxiv" not in r["expected"]["tolerant"].get("allowed_soft_failures", []):
            _fail(8, f"{rid}: allowed_soft_failures missing 'arxiv'")

    # Rule 9: title-only rows (015–018) match_quality == "medium".
    for rid in ("row_015", "row_016", "row_017", "row_018"):
        r = next(x for x in rows if x["row_id"] == rid)
        mq = r["expected"]["hard"].get("match_quality")
        if mq != "medium":
            _fail(9, f"{rid}: match_quality={mq!r}, expected 'medium'")

    # Rule 10: no row has expected.snapshot == null.
    for r in rows:
        if r["expected"].get("snapshot") is None:
            _fail(10, f"{r['row_id']}: expected.snapshot is null")

    # Rule 11: allowed_soft_failures ⊆ {"arxiv"}.
    for r in rows:
        vals = r["expected"]["tolerant"].get("allowed_soft_failures", [])
        for v in vals:
            if v not in {"arxiv"}:
                _fail(11, f"{r['row_id']}: allowed_soft_failures contains {v!r}")

    # Rule 12: discrepancy entries have valid rule + non-empty field.
    for r in rows:
        for kind in ("discrepancies_required", "discrepancies_forbidden"):
            for entry in r["expected"]["tolerant"].get(kind, []):
                if entry.get("rule") not in VALID_DISCREPANCY_RULES:
                    _fail(12, f"{r['row_id']}.{kind}: rule={entry.get('rule')!r}")
                f = entry.get("field")
                if not isinstance(f, str) or not f.strip():
                    _fail(12, f"{r['row_id']}.{kind}: field={entry.get('field')!r}")


def load_regression_30() -> dict[str, Any]:
    """Load and validate tests/fixtures/regression_30.json.

    Returns the parsed fixture dict on success.
    Raises FixtureValidationError with a descriptive message on any validation failure.
    """
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    _validate(fixture)
    return fixture
