"""Three-tier comparator for regression_30 fixture rows.

Public API:
    compare_hard(actual, expected_hard)        -> HardTierResult
    compare_tolerant(actual, expected_tolerant) -> TolerantTierResult
    classify_snapshot(actual, expected_snapshot) -> SnapshotTierResult
    compare_three_tier(actual, expected_block)   -> ThreeTierResult

The snapshot classifier (_walk, _diff, _classify_path, _categorise plus the
three _*_PATTERNS regex groups) is copied verbatim from
scripts/regression_30/regenerate_v0_3_5.py to preserve the Phase v0.3.5.F.1
four-way verdict semantics (NONE / AUTHOR_ONLY / AUTHOR_AND_SCORING /
UNEXPECTED). Decision 2A: discrepancies-path diffs continue to route as
scoring, not as their own tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# Sentinel for hard-tier fields absent from the actual response. Exposed so
# tests can identify the "field missing" case via identity check.
_MISSING = object()


# ---------------------------------------------------------------------------
# Snapshot classifier — copied verbatim from
# scripts/regression_30/regenerate_v0_3_5.py (Phase v0.3.5.F.1). Renamed
# `diff` -> `_diff` and `categorise` -> `_categorise` to make the public
# module surface clean; bodies unchanged.
# ---------------------------------------------------------------------------


def _walk(old, new, path: str, out: list) -> None:
    """Recursive structural diff; emits (path, old, new) tuples for leaf diffs."""
    if type(old) is not type(new) and not (old is None or new is None):
        # Treat type mismatch as a single change at this path.
        out.append((path, old, new))
        return
    if isinstance(old, dict) and isinstance(new, dict):
        keys = sorted(set(old.keys()) | set(new.keys()))
        for k in keys:
            child_path = f"{path}.{k}" if path else k
            if k not in old:
                out.append((child_path, "<missing>", new[k]))
            elif k not in new:
                out.append((child_path, old[k], "<missing>"))
            else:
                _walk(old[k], new[k], child_path, out)
        return
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            out.append((path, old, new))
            return
        for i, (a, b) in enumerate(zip(old, new)):
            _walk(a, b, f"{path}[{i}]", out)
        return
    if old != new:
        out.append((path, old, new))


def _diff(old: dict, new: dict) -> list:
    out: list = []
    _walk(old, new, "", out)
    return out


# Path patterns. Match against the json-path string built by _walk above.
_AUTHOR_PATTERNS = [
    re.compile(r"^canonical\.authors(\[\d+\](\..*)?)?$"),
    re.compile(r"^score_breakdown\.first_author_match$"),
    re.compile(r"^score_breakdown\.other_authors_match$"),
    re.compile(r".*first_author_surname.*"),
]

_SCORING_PATTERNS = [
    re.compile(r"^match_quality$"),
    re.compile(r"^confidence$"),
    re.compile(r"^verified$"),
    re.compile(r"^requires_review$"),
    re.compile(r"^discrepancies(\[\d+\](\..*)?)?$"),
    re.compile(r"^score_breakdown\.(title_sim|year_match|journal_match|confidence|weighted_score|rejected_by|guard|capped.*|adjusted_weights.*|input_field_count)$"),
    re.compile(r"^score_breakdown$"),  # whole replacement
]

_BIBLIOGRAPHIC_PATTERNS = [
    re.compile(r"^canonical\.(citation_count|year|journal|journal_iso_abbrev|volume|issue|pages|title|abstract|type|doi|pmid|arxiv_id|openalex_id|paper_id)(\..*)?$"),
]


def _classify_path(path: str) -> str:
    """Return one of: 'author', 'scoring', 'bibliographic', 'other'."""
    for pat in _AUTHOR_PATTERNS:
        if pat.match(path):
            return "author"
    for pat in _BIBLIOGRAPHIC_PATTERNS:
        if pat.match(path):
            return "bibliographic"
    for pat in _SCORING_PATTERNS:
        if pat.match(path):
            return "scoring"
    return "other"


def _categorise(diffs: list) -> tuple[str, dict]:
    """Map list of (path, old, new) tuples to one of four category labels.

    Returns (category, bucket_counts) for the report.
    """
    if not diffs:
        return "NONE", {"author": 0, "scoring": 0, "bibliographic": 0, "other": 0}
    buckets = {"author": 0, "scoring": 0, "bibliographic": 0, "other": 0}
    for path, _, _ in diffs:
        buckets[_classify_path(path)] += 1
    if buckets["bibliographic"] > 0 or buckets["other"] > 0:
        return "UNEXPECTED", buckets
    if buckets["scoring"] > 0:
        return "AUTHOR_AND_SCORING", buckets
    if buckets["author"] > 0:
        return "AUTHOR_ONLY", buckets
    # All zeros despite non-empty diffs — shouldn't happen, but flag conservatively.
    return "UNEXPECTED", buckets


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class HardFieldResult:
    field: str
    actual: Any
    expected: Any
    passed: bool


@dataclass
class HardTierResult:
    fields: list[HardFieldResult]

    @property
    def passed(self) -> bool:
        return all(f.passed for f in self.fields)


@dataclass
class CitationCountResult:
    actual: Optional[int]
    expected_value: int
    tolerance_pct: int
    delta_pct: Optional[float]
    passed: bool


@dataclass
class DiscrepancyRequiredResult:
    expected: list[tuple[str, str]]
    actual: list[tuple[str, str]]
    missing: list[tuple[str, str]]
    passed: bool


@dataclass
class DiscrepancyForbiddenResult:
    forbidden: list[tuple[str, str]]
    actual: list[tuple[str, str]]
    present: list[tuple[str, str]]
    passed: bool


@dataclass
class SoftFailuresResult:
    allowed: list[str]
    actual_failures: list[str]
    disallowed: list[str]
    passed: bool


@dataclass
class TolerantTierResult:
    citation_count: Optional[CitationCountResult] = None
    discrepancies_required: Optional[DiscrepancyRequiredResult] = None
    discrepancies_forbidden: Optional[DiscrepancyForbiddenResult] = None
    soft_failures: Optional[SoftFailuresResult] = None

    @property
    def passed(self) -> bool:
        results = [
            r
            for r in (
                self.citation_count,
                self.discrepancies_required,
                self.discrepancies_forbidden,
                self.soft_failures,
            )
            if r is not None
        ]
        return all(r.passed for r in results)


@dataclass
class SnapshotTierResult:
    verdict: str
    diffs: list[tuple[str, Any, Any]]
    buckets: dict[str, int]
    # Intentionally no .passed property — snapshot tier is informational.


@dataclass
class ThreeTierResult:
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


# ---------------------------------------------------------------------------
# Comparators
# ---------------------------------------------------------------------------


def _extract_first_author_surname(response: dict) -> str | None:
    """Return canonical.authors[0].family, or None if authors absent/empty."""
    canonical = response.get("canonical") or {}
    authors = canonical.get("authors") or []
    if not authors:
        return None
    return (authors[0] or {}).get("family")


# Hard-tier field extractors map abstract fixture keys to extraction logic
# against the live response shape. Fixture-builder logic at
# scripts/regression_30/apply_protocol.py:105+ has the inverse direction
# (response → fixture hard block). Duplicated here intentionally rather than
# importing from a script that isn't structured as a library; v0.4 hygiene
# pass may consolidate into a shared module.
HARD_FIELD_EXTRACTORS = {
    "match_found":          lambda r: bool(r.get("verified")),
    "doi_resolved":         lambda r: (r.get("canonical") or {}).get("doi"),
    "pmid_resolved":        lambda r: (r.get("canonical") or {}).get("pmid"),
    "arxiv_id_resolved":    lambda r: (r.get("canonical") or {}).get("arxiv_id"),
    "first_author_surname": _extract_first_author_surname,
    "year":                 lambda r: (r.get("canonical") or {}).get("year"),
    "match_quality":        lambda r: r.get("match_quality"),
    "confidence":           lambda r: r.get("confidence"),
    "rejected_by":          lambda r: (r.get("score_breakdown") or {}).get("rejected_by"),
}


def compare_hard(actual: dict, expected_hard: dict) -> HardTierResult:
    fields: list[HardFieldResult] = []
    for key, exp in expected_hard.items():
        if key in HARD_FIELD_EXTRACTORS:
            actual_value = HARD_FIELD_EXTRACTORS[key](actual)
            passed = (actual_value == exp)
        elif key in actual:
            actual_value = actual[key]
            passed = (actual_value == exp)
        else:
            actual_value = _MISSING
            passed = False
        fields.append(
            HardFieldResult(
                field=key, actual=actual_value, expected=exp, passed=passed
            )
        )
    return HardTierResult(fields=fields)


def _pair_list(items: list[dict]) -> list[tuple[str, str]]:
    return [(d["rule"], d["field"]) for d in items]


def compare_tolerant(
    actual: dict, expected_tolerant: dict
) -> TolerantTierResult:
    result = TolerantTierResult()

    if "citation_count" in expected_tolerant:
        spec = expected_tolerant["citation_count"]
        exp_value = spec["value"]
        tol_pct = spec["tolerance_pct"]
        raw_actual = actual.get("canonical", {}).get("citation_count")
        # canonical.citation_count in live bulk responses is a per-source
        # dict (e.g., {"openalex": 20, "semantic_scholar": 21}), not a
        # single int. Aggregate to one int via max() for the tolerant-tier
        # band comparison. max() is the empirically-aligned aggregation:
        # robust to missing sources, deterministic across dict orderings,
        # and matches the fixture-builder's value on spot-checked rows.
        # Defer per-source comparison to v0.4 if cross-source drift
        # detection becomes valuable.
        if isinstance(raw_actual, dict):
            valid_values = [v for v in raw_actual.values() if v is not None]
            actual_value = max(valid_values) if valid_values else None
        else:
            actual_value = raw_actual
        if actual_value is None:
            result.citation_count = CitationCountResult(
                actual=None,
                expected_value=exp_value,
                tolerance_pct=tol_pct,
                delta_pct=None,
                passed=False,
            )
        elif exp_value == 0:
            result.citation_count = CitationCountResult(
                actual=actual_value,
                expected_value=0,
                tolerance_pct=tol_pct,
                delta_pct=None,
                passed=(actual_value == 0),
            )
        else:
            delta_pct = abs(actual_value - exp_value) / exp_value * 100
            result.citation_count = CitationCountResult(
                actual=actual_value,
                expected_value=exp_value,
                tolerance_pct=tol_pct,
                delta_pct=delta_pct,
                passed=(delta_pct <= tol_pct),
            )

    if "discrepancies_required" in expected_tolerant:
        required = expected_tolerant["discrepancies_required"]
        required_tuples = _pair_list(required)
        actual_disc = actual.get("discrepancies", [])
        actual_tuples = _pair_list(actual_disc)
        actual_set = set(actual_tuples)
        missing = [t for t in required_tuples if t not in actual_set]
        result.discrepancies_required = DiscrepancyRequiredResult(
            expected=required_tuples,
            actual=actual_tuples,
            missing=missing,
            passed=(len(missing) == 0),
        )

    if "discrepancies_forbidden" in expected_tolerant:
        forbidden = expected_tolerant["discrepancies_forbidden"]
        forbidden_tuples = _pair_list(forbidden)
        actual_disc = actual.get("discrepancies", [])
        actual_tuples = _pair_list(actual_disc)
        actual_set = set(actual_tuples)
        present = [t for t in forbidden_tuples if t in actual_set]
        result.discrepancies_forbidden = DiscrepancyForbiddenResult(
            forbidden=forbidden_tuples,
            actual=actual_tuples,
            present=present,
            passed=(len(present) == 0),
        )

    if "allowed_soft_failures" in expected_tolerant:
        allowed = list(expected_tolerant["allowed_soft_failures"])
        actual_failures = list(actual.get("databases_failed", []))
        disallowed = [f for f in actual_failures if f not in allowed]
        result.soft_failures = SoftFailuresResult(
            allowed=allowed,
            actual_failures=actual_failures,
            disallowed=disallowed,
            passed=(len(disallowed) == 0),
        )

    return result


def classify_snapshot(
    actual: dict, expected_snapshot: dict
) -> SnapshotTierResult:
    # warnings is operational metadata (cache markers, soft-failure messages)
    # that legitimately varies between fixture capture and live runs. Strip
    # before snapshot diff. Cache-hit signal is captured separately in
    # summary.cache_hits; soft failures are captured in
    # tolerant.allowed_soft_failures.
    actual_for_diff = {k: v for k, v in actual.items() if k != "warnings"}
    expected_for_diff = {
        k: v for k, v in expected_snapshot.items() if k != "warnings"
    }
    diffs = _diff(expected_for_diff, actual_for_diff)
    verdict, buckets = _categorise(diffs)
    return SnapshotTierResult(verdict=verdict, diffs=diffs, buckets=buckets)


def compare_three_tier(actual: dict, expected: dict) -> ThreeTierResult:
    return ThreeTierResult(
        hard=compare_hard(actual, expected.get("hard", {})),
        tolerant=compare_tolerant(actual, expected.get("tolerant", {})),
        snapshot=classify_snapshot(actual, expected.get("snapshot", {})),
    )
