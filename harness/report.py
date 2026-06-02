"""Markdown report builder for the citation-mcp validation harness."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from tests.fixtures.comparator import _MISSING

from .comparator_runner import BulkComparisonResult, RowComparisonResult


PASS = "PASS"
FAIL = "FAIL"
CHECK = "✓"
CROSS = "✗"


@dataclass
class ValidationRunInputs:
    fixture_version: str
    server_url: str
    client_id_suffix: str
    timestamp_utc_iso: str

    cold_wall_clock_seconds: float
    cold_server_elapsed_seconds: float
    cold_cache_hits: int
    cold_refreshed_token: bool

    warm_wall_clock_seconds: float
    warm_server_elapsed_seconds: float
    warm_cache_hits: int
    warm_refreshed_token: bool

    comparison: BulkComparisonResult
    n_citations: int

    # Retained for backward compatibility with callers that still pass it,
    # but no longer consulted by gate logic (Phase 1.E.2.F.3 recalibration:
    # latency is informational only, not a gate criterion).
    cold_latency_gate_seconds: float = 20.0


def _ps(b: bool) -> str:
    return PASS if b else FAIL


def _yn(b: bool) -> str:
    return "Yes" if b else "No"


def _fmt_val(v: object) -> str:
    if v is _MISSING:
        return "<missing>"
    if isinstance(v, str):
        return repr(v) if v == "" else v
    return repr(v) if isinstance(v, (dict, list, tuple)) else str(v)


def _hard_fail_lines(row: RowComparisonResult) -> list[str]:
    lines: list[str] = []
    for f in row.hard.fields:
        if not f.passed:
            lines.append(
                f"- Field `{f.field}`: actual={_fmt_val(f.actual)}, "
                f"expected={_fmt_val(f.expected)}"
            )
    return lines


def _tolerant_fail_lines(row: RowComparisonResult) -> list[str]:
    lines: list[str] = []
    cc = row.tolerant.citation_count
    if cc is not None and not cc.passed:
        anomaly = cc.anomaly or "unknown"
        lines.append(
            f"- citation_count anomaly `{anomaly}`: "
            f"actual={cc.actual}, baseline={cc.expected_value}"
        )
    dr = row.tolerant.discrepancies_required
    if dr is not None and not dr.passed:
        lines.append(
            f"- discrepancies_required missing: {dr.missing}"
        )
    df = row.tolerant.discrepancies_forbidden
    if df is not None and not df.passed:
        lines.append(
            f"- discrepancies_forbidden present: {df.present}"
        )
    sf = row.tolerant.soft_failures
    if sf is not None and not sf.passed:
        lines.append(
            f"- allowed_soft_failures violated: disallowed={sf.disallowed}"
        )
    return lines


def _snapshot_paths(row: RowComparisonResult, limit: int = 6) -> str:
    paths = [p for p, _, _ in row.snapshot.diffs]
    if len(paths) > limit:
        shown = paths[:limit] + [f"... (+{len(paths) - limit} more)"]
    else:
        shown = paths
    return ", ".join(f"`{p}`" for p in shown) if shown else "(none)"


def _gate_passed(inputs: ValidationRunInputs) -> bool:
    # v1.0 gate measures connector-controlled correctness only — hard tier
    # plus the gate-relevant tolerant sub-checks (citation_count band +
    # discrepancies). Cold-cache latency and upstream-DB availability are
    # rendered as informational but do NOT gate, since transient upstream
    # behavior is outside the connector's control. See Phase 1.E.2.F.3.
    return (
        inputs.comparison.all_passed_hard
        and inputs.comparison.all_passed_tolerant
    )


def build_report(inputs: ValidationRunInputs) -> str:
    cmp = inputs.comparison
    n = len(cmp.rows)
    hard_pass_count = sum(1 for r in cmp.rows if r.passed_hard)
    tol_pass_count = sum(1 for r in cmp.rows if r.passed_tolerant)
    gate_pass = _gate_passed(inputs)
    verdicts = cmp.snapshot_verdict_counts

    out: list[str] = []
    a = out.append

    a("# citation-mcp validation report")
    a("")
    a(f"**Timestamp:** {inputs.timestamp_utc_iso}")
    a(f"**Connector:** {inputs.server_url}")
    a(f"**Fixture version:** {inputs.fixture_version}")
    a(f"**OAuth client:** ...{inputs.client_id_suffix}")
    a(f"**Citations:** {inputs.n_citations}")
    a("")

    a("## Overall result")
    a("")
    a(
        f"**Hard tier:** {_ps(cmp.all_passed_hard)} "
        f"({hard_pass_count}/{n} rows)"
    )
    a(
        f"**Tolerant tier:** {_ps(cmp.all_passed_tolerant)} "
        f"({tol_pass_count}/{n} rows)"
    )
    a(f"**v1.0 gate:** {_ps(gate_pass)}")
    a("")
    a(
        f"_Cold-cache latency: {inputs.cold_wall_clock_seconds:.2f}s "
        f"(informational — not a gate criterion)_"
    )
    a("")

    a("## Latency")
    a("")
    a("| Pass | Client wall-clock | Server elapsed | Cache hits | Token refresh |")
    a("|------|-------------------|----------------|------------|---------------|")
    a(
        f"| Cold | {inputs.cold_wall_clock_seconds:.2f}s | "
        f"{inputs.cold_server_elapsed_seconds:.2f}s | "
        f"{inputs.cold_cache_hits} | {_yn(inputs.cold_refreshed_token)} |"
    )
    a(
        f"| Warm | {inputs.warm_wall_clock_seconds:.2f}s | "
        f"{inputs.warm_server_elapsed_seconds:.2f}s | "
        f"{inputs.warm_cache_hits} | {_yn(inputs.warm_refreshed_token)} |"
    )
    a("")

    a("## Snapshot verdict distribution")
    a("")
    a("| Verdict | Count |")
    a("|---------|-------|")
    for v in ("NONE", "AUTHOR_ONLY", "AUTHOR_AND_SCORING", "UNEXPECTED"):
        a(f"| {v} | {verdicts.get(v, 0)} |")
    a("")

    a("## Per-row results")
    a("")
    a("| row_id | category | hard | tolerant | snapshot verdict |")
    a("|--------|----------|------|----------|------------------|")
    for r in cmp.rows:
        a(
            f"| {r.row_id} | {r.category} | "
            f"{CHECK if r.passed_hard else CROSS} | "
            f"{CHECK if r.passed_tolerant else CROSS} | "
            f"{r.snapshot_verdict} |"
        )
    a("")

    a("## Needs attention")
    a("")

    items: list[str] = []

    # (a) Hard failures
    hard_failures = [r for r in cmp.rows if not r.passed_hard]
    for r in hard_failures:
        block = [f"### {r.row_id} — Hard tier failure"]
        block.extend(_hard_fail_lines(r))
        if not r.passed_tolerant:
            block.append(
                "- (Note: tolerant tier also failed for this row; see below for detail.)"
            )
            block.extend(_tolerant_fail_lines(r))
        items.append("\n".join(block))

    # (b) Tolerant-only failures (rows where hard passed but tolerant failed)
    tolerant_only_failures = [
        r for r in cmp.rows if r.passed_hard and not r.passed_tolerant
    ]
    for r in tolerant_only_failures:
        block = [f"### {r.row_id} — Tolerant tier failure"]
        block.extend(_tolerant_fail_lines(r))
        items.append("\n".join(block))

    # (c) Upstream database availability (informational; does not affect gate).
    # soft_failures sub-results are populated by compare_tolerant but excluded
    # from the tolerant-tier gate verdict per Phase 1.E.2.F.3 recalibration.
    db_to_rows: dict[str, list[str]] = {}
    for r in cmp.rows:
        sf = r.tolerant.soft_failures
        if sf is None or sf.passed:
            continue
        for db in sf.disallowed:
            db_to_rows.setdefault(db, []).append(r.row_id)
    if db_to_rows:
        block = [
            "### Upstream database availability (informational — does not affect gate)"
        ]
        for db in sorted(db_to_rows):
            block.append(
                f"- {db} failed on rows: {', '.join(db_to_rows[db])}"
            )
        block.append(
            "- (these are transient upstream failures; the connector degraded "
            "gracefully and hard/tolerant gate checks passed using the "
            "remaining databases)"
        )
        items.append("\n".join(block))

    # (d) Snapshot verdicts other than NONE (informational)
    interesting_snapshots = [
        r for r in cmp.rows if r.snapshot_verdict != "NONE"
    ]
    if interesting_snapshots:
        block = ["### Snapshot verdicts requiring review"]
        for r in interesting_snapshots:
            block.append(
                f"- {r.row_id}: {r.snapshot_verdict} "
                f"— diffs at: {_snapshot_paths(r)}"
            )
        items.append("\n".join(block))

    # (d) Cache-hit anomaly on warm pass
    if inputs.warm_cache_hits != inputs.n_citations:
        block = [
            "### Cache hit anomaly",
            (
                f"Expected warm_cache_hits = {inputs.n_citations}, "
                f"got {inputs.warm_cache_hits}. Soft warning — does not "
                f"affect gate. May indicate external cache disturbance between "
                f"cold and warm passes."
            ),
        ]
        items.append("\n".join(block))

    if not items:
        a("(no items requiring attention)")
    else:
        for i, item in enumerate(items):
            a(item)
            if i < len(items) - 1:
                a("")
    a("")

    return "\n".join(out)


def _timestamp_compact(ts_iso: str) -> str:
    """Convert an ISO-8601 timestamp to compact YYYYMMDDTHHMMSSZ form.

    Falls back to the current UTC time if parsing fails.
    """
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(tz=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _json_default(obj):
    if obj is _MISSING:
        return "<missing>"
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, tuple):
        return list(obj)
    return repr(obj)


def _row_to_raw_dict(row: RowComparisonResult) -> dict:
    return {
        "row_id": row.row_id,
        "category": row.category,
        "passed_hard": row.passed_hard,
        "passed_tolerant": row.passed_tolerant,
        "snapshot_verdict": row.snapshot_verdict,
        "actual": row.actual,
        "hard": asdict(row.hard),
        "tolerant": asdict(row.tolerant),
        "snapshot": asdict(row.snapshot),
    }


def write_raw_responses(
    inputs: ValidationRunInputs, output_dir: Path
) -> Path:
    """Persist per-row actual responses + diff details alongside the markdown.

    The markdown report only lists diff paths, not values — a failing-row
    triage previously required re-running the harness to read the actual
    response. This sidecar carries the full per-row payload so any future
    failure can be diagnosed from disk alone. Gitignored via
    `harness/reports/*`.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"report_{_timestamp_compact(inputs.timestamp_utc_iso)}.raw.json"
    path = output_dir / name
    payload = {
        "timestamp_utc_iso": inputs.timestamp_utc_iso,
        "fixture_version": inputs.fixture_version,
        "server_url": inputs.server_url,
        "n_citations": inputs.n_citations,
        "rows": [_row_to_raw_dict(r) for r in inputs.comparison.rows],
    }
    path.write_text(
        json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_report(
    inputs: ValidationRunInputs, output_dir: Path
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    body = build_report(inputs)
    name = f"report_{_timestamp_compact(inputs.timestamp_utc_iso)}.md"
    path = output_dir / name
    path.write_text(body, encoding="utf-8")
    write_raw_responses(inputs, output_dir)
    return path
