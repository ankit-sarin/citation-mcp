"""End-to-end orchestration for the citation-mcp validation harness.

run_validation() composes:
  * OAuth client (harness/oauth_client.py)
  * MCP transport + bulkVerifyCitations call (harness/mcp_client.py)
  * Three-tier comparator (tests/fixtures/comparator.py via comparator_runner)
  * Markdown report builder (harness/report.py)

cold_only / warm_only flags are debugging modes — they produce a partial
report but never pass the v1.0 gate.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from tests.fixtures.loader import load_regression_30

from .comparator_runner import run_comparison
from .mcp_client import call_bulk_with_refresh_retry
from .oauth_client import (
    HarnessOAuthClient,
    RefreshTokenInvalid,
    TokensNotFoundError,
)
from .report import ValidationRunInputs, write_report


DEFAULT_SERVER_URL = "https://citation-mcp.digitalsurgeon.dev/mcp"
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _strip_nulls(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


async def run_validation(
    cold_only: bool = False,
    warm_only: bool = False,
    server_url: str = DEFAULT_SERVER_URL,
    output_dir: Path | None = None,
    config_dir: Path | None = None,
) -> tuple[Path, bool]:
    """Run the validation harness end-to-end.

    Returns (report_path, gate_passed). When cold_only or warm_only is set
    the function still writes a report but the gate is always False (debug
    mode is not a gate run).
    """
    if cold_only and warm_only:
        raise ValueError("cold_only and warm_only are mutually exclusive")

    fixture = load_regression_30()
    fixture_version = fixture["fixture_version"]
    fixture_rows = fixture["rows"]
    citations = [_strip_nulls(r["input"]) for r in fixture_rows]
    n_citations = len(citations)

    oauth = HarnessOAuthClient(config_dir=config_dir)
    try:
        oauth.get_access_token()
    except (TokensNotFoundError, RefreshTokenInvalid) as e:
        print(
            f"No valid tokens. Run `python -m harness.cli auth` first. ({e})",
            file=sys.stderr,
        )
        sys.exit(2)

    timestamp = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    client_id_suffix = oauth.status()["client_id_suffix"]

    cold_outcome = None
    warm_outcome = None

    if not warm_only:
        cold_outcome = await call_bulk_with_refresh_retry(
            oauth, server_url, citations, force_refresh=True
        )
    if not cold_only:
        warm_outcome = await call_bulk_with_refresh_retry(
            oauth, server_url, citations, force_refresh=False
        )

    chosen_payload = (
        warm_outcome.payload if warm_outcome is not None else cold_outcome.payload
    )
    comparison = run_comparison(fixture_rows, chosen_payload)

    def _summary(outcome) -> tuple[float, int]:
        if outcome is None:
            return 0.0, 0
        summary = outcome.payload.get("summary") or {}
        return (
            float(summary.get("elapsed_seconds", 0.0)),
            int(summary.get("cache_hits", 0)),
        )

    cold_server_elapsed, cold_cache_hits = _summary(cold_outcome)
    warm_server_elapsed, warm_cache_hits = _summary(warm_outcome)

    inputs = ValidationRunInputs(
        fixture_version=fixture_version,
        server_url=server_url,
        client_id_suffix=client_id_suffix,
        timestamp_utc_iso=timestamp,
        cold_wall_clock_seconds=(
            cold_outcome.wall_clock_seconds if cold_outcome else 0.0
        ),
        cold_server_elapsed_seconds=cold_server_elapsed,
        cold_cache_hits=cold_cache_hits,
        cold_refreshed_token=bool(
            cold_outcome.refreshed_token if cold_outcome else False
        ),
        warm_wall_clock_seconds=(
            warm_outcome.wall_clock_seconds if warm_outcome else 0.0
        ),
        warm_server_elapsed_seconds=warm_server_elapsed,
        warm_cache_hits=warm_cache_hits,
        warm_refreshed_token=bool(
            warm_outcome.refreshed_token if warm_outcome else False
        ),
        comparison=comparison,
        n_citations=n_citations,
    )

    output_dir = Path(output_dir) if output_dir is not None else DEFAULT_REPORTS_DIR
    report_path = write_report(inputs, output_dir)

    both_passes_ran = cold_outcome is not None and warm_outcome is not None
    # v1.0 gate measures connector-controlled correctness only. Cold-cache
    # latency and upstream-DB availability are reported but do NOT gate —
    # they're sensitive to transient upstream-DB conditions outside the
    # connector's control. See Phase 1.E.2.F.3 recalibration.
    gate_passed = (
        both_passes_ran
        and comparison.all_passed_hard
        and comparison.all_passed_tolerant
    )
    return report_path, gate_passed


def run_validation_sync(
    cold_only: bool = False, warm_only: bool = False
) -> tuple[Path, bool]:
    """Sync wrapper for CLI entry. Wraps asyncio.run."""
    return asyncio.run(
        run_validation(cold_only=cold_only, warm_only=warm_only)
    )
