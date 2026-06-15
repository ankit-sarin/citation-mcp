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

# Phase GATE-OPT1 hard-tier denylist (Rule 13 — the un-reintroducibility
# lever). These are positive upstream-echoed values/decisions that must never
# gate the live nightly harness; they live in expected.snapshot (non-gating)
# instead. Re-arming any of them in expected.hard fails this loader, which runs
# in tests/test_regression_30_fixture.py — so the flaky gate fails in CI, not at
# 2am in production.
#
#   * HARD_DENYLIST_ALWAYS — no carrier-invariant form exists; any presence in
#     expected.hard is illegal.
#   * match_found — only the positive (True) form is denylisted; match_found
#     False is the carrier-invariant negative for no-match/fabrication rows and
#     stays (see Rule 6 / row_029).
#   * HARD_DENYLIST_IDENTIFIERS — only a non-null (positive) value is denylisted;
#     a null identifier is the carrier-invariant negative on no-match rows and
#     stays (a real DB surfacing an identifier for a fabrication is a connector
#     bug, never an outage artifact).
HARD_DENYLIST_ALWAYS = frozenset(
    {
        "year",
        "first_author_surname",
        "journal",
        "confidence",
        "match_quality",
        "rejected_by",
    }
)
HARD_DENYLIST_IDENTIFIERS = frozenset(
    {"doi_resolved", "pmid_resolved", "arxiv_id_resolved"}
)


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

    # Rule 6: adversarial row constraints. Under Phase GATE-OPT1 the decision
    # fields (match_quality, rejected_by, and match_found=True) are demoted out
    # of expected.hard, so these structural invariants read the baseline from
    # expected.snapshot. The ONLY hard-tier assertion retained on an adversarial
    # row is the carrier-invariant negative match_found=False (row_029).
    r029 = next(r for r in rows if r["row_id"] == "row_029")
    s029 = r029["expected"]["snapshot"]
    if s029.get("verified") is not False:
        _fail(6, f"row_029: snapshot.verified={s029.get('verified')!r}, expected false")
    rb029 = (s029.get("score_breakdown") or {}).get("rejected_by")
    if rb029 not in ROW_029_ALLOWED_REJECTED_BY:
        _fail(6, f"row_029: snapshot rejected_by={rb029!r} not in {sorted(ROW_029_ALLOWED_REJECTED_BY)}")
    # row_029 retains the carrier-invariant negative in its gated hard block.
    if r029["expected"]["hard"].get("match_found") is not False:
        _fail(6, "row_029: hard.match_found must remain False (carrier-invariant negative)")

    r030 = next(r for r in rows if r["row_id"] == "row_030")
    s030 = r030["expected"]["snapshot"]
    v030 = s030.get("verified")
    if v030 is True:
        # Path B: noisy match -> match_quality must be 'low' or 'medium'
        # (Phase 2.E.3 accepted medium). Read from snapshot (demoted from hard).
        mq = s030.get("match_quality")
        if mq not in {"low", "medium"}:
            _fail(6, f"row_030 Path B: snapshot.match_quality={mq!r}, expected 'low' or 'medium'")
    elif v030 is False:
        # Path A: guard rejection -> valid rejected_by (from snapshot).
        rb030 = (s030.get("score_breakdown") or {}).get("rejected_by")
        if rb030 not in ROW_029_ALLOWED_REJECTED_BY:
            _fail(6, f"row_030 Path A: snapshot rejected_by={rb030!r} not in allowed set")
    else:
        _fail(6, f"row_030: snapshot.verified={v030!r}, expected true or false")

    for arow in (r029, r030):
        inp = arow.get("input", {})
        if not any(inp.get(k) for k in ("doi", "pmid", "title")):
            _fail(6, f"{arow['row_id']}: input missing all of doi/pmid/title")

    # Rule 7: non-adversarial rows are complete matches in the baseline —
    # match_found=true with populated first_author_surname / year /
    # match_quality. Under Phase GATE-OPT1 these are all demoted out of
    # expected.hard, so the completeness assertion reads them UNIFORMLY from
    # expected.snapshot for every row (this collapses the v12
    # YEAR_FROM_SNAPSHOT_ROWS named-set stopgap into one uniform rule).
    for r in rows:
        if r["category"] == "adversarial":
            continue
        rid = r["row_id"]
        s = r["expected"]["snapshot"]
        if s.get("verified") is not True:
            _fail(7, f"{rid}: snapshot.verified={s.get('verified')!r}, expected true")
        canonical = s.get("canonical") or {}
        authors = canonical.get("authors") or []
        first_family = (authors[0] or {}).get("family") if authors else None
        if not first_family:
            _fail(7, f"{rid}: snapshot canonical first-author family empty")
        if canonical.get("year") is None:
            _fail(7, f"{rid}: snapshot canonical.year is null")
        if s.get("match_quality") is None:
            _fail(7, f"{rid}: snapshot.match_quality is null")

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

    # Rule 9: title-only rows (015–018) match_quality == "medium" (title-only
    # cap). Read from snapshot — match_quality demoted from hard (GATE-OPT1).
    for rid in ("row_015", "row_016", "row_017", "row_018"):
        r = next(x for x in rows if x["row_id"] == rid)
        mq = r["expected"]["snapshot"].get("match_quality")
        if mq != "medium":
            _fail(9, f"{rid}: snapshot.match_quality={mq!r}, expected 'medium'")

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

    # Rule 13 (Phase GATE-OPT1 un-reintroducibility lever): expected.hard must
    # NOT re-arm any positive denylisted field. This is the guard that makes a
    # future fixture edit re-introducing the flaky gate fail in the test suite
    # rather than in production. See HARD_DENYLIST_* above.
    for r in rows:
        rid = r["row_id"]
        h = r["expected"]["hard"]
        for f in sorted(HARD_DENYLIST_ALWAYS):
            if f in h:
                _fail(13, f"{rid}: expected.hard re-arms denylisted field {f!r}")
        if h.get("match_found") is True:
            _fail(13, f"{rid}: expected.hard re-arms positive match_found=True")
        for f in sorted(HARD_DENYLIST_IDENTIFIERS):
            if f in h and h[f] is not None:
                _fail(
                    13,
                    f"{rid}: expected.hard re-arms positive {f}={h[f]!r} "
                    f"(only a null identifier is permitted, on no-match rows)",
                )

    # Rule 14 (Phase CMCP-GATE-0615 un-reintroducibility lever — sibling to
    # Rule 13, but guarding the comparator's TOLERANT gating set rather than the
    # fixture's hard block). The citation_count surface (value AND stub state) is
    # entirely upstream-availability-controlled — the 06-15 row_018 FAIL was a
    # matched row whose count carriers were both down. Gating on it is a category
    # error. If any denylisted field re-enters comparator.TOLERANT_GATE_FIELDS,
    # fail here (test_regression_30_fixture.py) — in CI, not at 2am. Read via the
    # module object (not a bound import) so a monkeypatched gating set is honored.
    from tests.fixtures import comparator as _comparator

    gating = set(_comparator.TOLERANT_GATE_FIELDS)
    rearmed = sorted(gating & set(_comparator.TOLERANT_GATE_DENYLIST))
    if rearmed:
        _fail(
            14,
            f"tolerant gating set re-arms denylisted citation_count field(s): "
            f"{rearmed} (citation_count is upstream-availability-controlled and "
            f"must stay non-gating snapshot)",
        )


def load_regression_30() -> dict[str, Any]:
    """Load and validate tests/fixtures/regression_30.json.

    Returns the parsed fixture dict on success.
    Raises FixtureValidationError with a descriptive message on any validation failure.
    """
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    _validate(fixture)
    return fixture
