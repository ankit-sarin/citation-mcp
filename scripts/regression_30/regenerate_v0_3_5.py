"""Phase v0.3.5.F.1 — regeneration dry-run.

Re-runs each of the 30 rows in tests/fixtures/regression_30.json through
verifyCitation via stdio with force_refresh=true. Diffs each new response
against the current expected.snapshot, categorises the row, and writes a
staging file for v0.3.5.F.2 consumption.

No fixture modifications. No commits. Pure regeneration + categorisation.

Categories:
  NONE                — no observable change vs current snapshot
  AUTHOR_ONLY         — changes restricted to author shape / surname fields
  AUTHOR_AND_SCORING  — author-related + match_quality / confidence / discrepancies
  UNEXPECTED          — bibliographic-fact changes OR unrelated changes
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "regression_30.json"
STAGING_PATH = PROJECT_ROOT / "tests" / "fixtures" / "regression_30_v0_3_5_responses.json"


# ---------------------------------------------------------------------------
# Diff machinery
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


def diff(old: dict, new: dict) -> list:
    out: list = []
    _walk(old, new, "", out)
    return out


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------


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


def categorise(diffs: list) -> tuple[str, dict]:
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
# Stdio harness
# ---------------------------------------------------------------------------


def _strip_nulls(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _load_env_file(env_path: Path) -> dict:
    """Parse a simple KEY=VALUE .env file. No quote handling beyond strip."""
    out = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'").strip('"')
    return out


async def _call_one(session: ClientSession, args: dict, *, retries: int = 1) -> dict:
    """Call verifyCitation once with retry-on-error."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            result = await session.call_tool("verifyCitation", arguments=args)
            text_payload = None
            for block in result.content:
                if hasattr(block, "text") and block.text:
                    text_payload = block.text
                    break
            if text_payload is None:
                raise RuntimeError("no text content in tool result")
            return json.loads(text_payload)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(5.0)
                continue
            raise last_exc


async def _run() -> dict:
    if not FIXTURE_PATH.exists():
        raise SystemExit(f"missing fixture: {FIXTURE_PATH}")

    fixture = json.loads(FIXTURE_PATH.read_text())
    rows = fixture["rows"]
    assert len(rows) == 30, f"expected 30 rows, got {len(rows)}"

    # Pull API keys from .env if not already in os.environ.
    env_file = _load_env_file(PROJECT_ROOT / ".env")
    required = ["NCBI_API_KEY", "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"]
    merged_env = {**env_file, **{k: v for k, v in os.environ.items()}}
    # Prefer process env, fall back to .env file.
    for k in required:
        if not os.environ.get(k) and env_file.get(k):
            merged_env[k] = env_file[k]
    missing = [k for k in required if not merged_env.get(k)]
    if missing:
        raise SystemExit(f"missing required env vars: {missing}")

    # Isolated cache DB so forced refreshes don't contaminate production cache.
    tmpdir = Path(tempfile.mkdtemp(prefix="regen_v0_3_5_"))
    cache_db = tmpdir / "cache.db"
    sub_env = {**merged_env, "CACHE_DB_PATH": str(cache_db)}
    # Make sure CROSSREF_POLITE_EMAIL is set if available.
    if env_file.get("CROSSREF_POLITE_EMAIL") and not os.environ.get("CROSSREF_POLITE_EMAIL"):
        sub_env["CROSSREF_POLITE_EMAIL"] = env_file["CROSSREF_POLITE_EMAIL"]

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "citation_mcp.server"],
        env=sub_env,
    )

    new_responses: dict[str, dict] = {}
    retries_used: list[str] = []
    n_errors = 0
    wall_start = time.monotonic()

    print(f"Regenerating 30 rows via stdio (force_refresh=True, isolated cache)...", flush=True)
    print(f"Cache DB: {cache_db}", flush=True)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for i, row in enumerate(rows, 1):
                rid = row["row_id"]
                args = _strip_nulls(row["input"])
                args["force_refresh"] = True
                t0 = time.monotonic()
                errored = False
                first_attempt_failed = False
                try:
                    try:
                        payload = await _call_one(session, args, retries=0)
                    except Exception:
                        first_attempt_failed = True
                        retries_used.append(rid)
                        await asyncio.sleep(5.0)
                        payload = await _call_one(session, args, retries=0)
                    new_responses[rid] = payload
                except Exception as e:
                    errored = True
                    n_errors += 1
                    print(f"  [{i:2d}/30] {rid:20s} ERR {type(e).__name__}: {e}", flush=True)
                    traceback.print_exc()
                    # Abort per spec: don't silently skip.
                    raise SystemExit(f"row {rid} failed after retry — aborting per spec")
                elapsed = time.monotonic() - t0
                tag = "retry" if first_attempt_failed else "ok"
                print(f"  [{i:2d}/30] {rid:20s} {tag:5s} {elapsed:6.2f}s", flush=True)
                await asyncio.sleep(0.5)

    wall_elapsed = time.monotonic() - wall_start
    return {
        "responses": new_responses,
        "retries_used": retries_used,
        "n_errors": n_errors,
        "wall_elapsed": wall_elapsed,
        "rows": rows,
        "tmpdir": str(tmpdir),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt_value(v) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    if len(s) > 140:
        s = s[:137] + "..."
    return s


def _print_report(rows: list, responses: dict, wall: float, retries: list) -> None:
    print()
    print("=" * 72)
    print("=== Phase v0.3.5.F.1 — regeneration dry-run ===")
    print("=" * 72)
    print()
    print(f"Wall-clock: {wall:.1f}s (regeneration of 30 rows via stdio with force_refresh=true)")
    if retries:
        print(f"Retries triggered: {len(retries)} rows — {retries}")
    else:
        print("Retries triggered: none")
    print()

    cat_counts = {"NONE": 0, "AUTHOR_ONLY": 0, "AUTHOR_AND_SCORING": 0, "UNEXPECTED": 0}
    per_row_summary = []

    for row in rows:
        rid = row["row_id"]
        snap = row["expected"]["snapshot"]
        new = responses[rid]
        diffs = diff(snap, new)
        cat, buckets = categorise(diffs)
        cat_counts[cat] += 1
        per_row_summary.append((rid, cat, diffs, buckets))

    print("Category breakdown:")
    print(f"  NONE:               {cat_counts['NONE']:2d} rows")
    print(f"  AUTHOR_ONLY:        {cat_counts['AUTHOR_ONLY']:2d} rows")
    print(f"  AUTHOR_AND_SCORING: {cat_counts['AUTHOR_AND_SCORING']:2d} rows")
    print(f"  UNEXPECTED:         {cat_counts['UNEXPECTED']:2d} rows")
    print()
    print("Per-row detail:")
    print()

    for rid, cat, diffs, buckets in per_row_summary:
        print(f"{rid} — [{cat}]")
        if not diffs:
            print("  (no changes)")
        else:
            print(f"  Bucket counts: author={buckets['author']} scoring={buckets['scoring']} "
                  f"bibliographic={buckets['bibliographic']} other={buckets['other']}")
            print(f"  Changed fields ({len(diffs)}):")
            for path, oldv, newv in diffs:
                print(f"    {path}:")
                print(f"      old: {_fmt_value(oldv)}")
                print(f"      new: {_fmt_value(newv)}")
        print()

    needs_update = cat_counts["AUTHOR_ONLY"] + cat_counts["AUTHOR_AND_SCORING"]
    print("Summary:")
    print(f"  Total rows expected to need fixture update (AUTHOR_ONLY + AUTHOR_AND_SCORING): {needs_update}")
    print(f"  Total rows expected to need NO change (NONE): {cat_counts['NONE']}"
          f"  [should equal 13 Western-only + 6 identifier-only = 19]")
    print(f"  Total UNEXPECTED rows requiring architect intervention: {cat_counts['UNEXPECTED']}"
          f"  [should ideally be 0]")
    print()
    print(f"Staging file written: {STAGING_PATH}")


def _write_staging(responses: dict, wall: float, retries: list) -> None:
    payload = {
        "phase": "v0.3.5.F.1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_elapsed_seconds": round(wall, 2),
        "retries_used": retries,
        "force_refresh": True,
        "n_rows": len(responses),
        "responses": responses,
    }
    STAGING_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    summary = asyncio.run(_run())
    _write_staging(summary["responses"], summary["wall_elapsed"], summary["retries_used"])
    _print_report(summary["rows"], summary["responses"], summary["wall_elapsed"], summary["retries_used"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
