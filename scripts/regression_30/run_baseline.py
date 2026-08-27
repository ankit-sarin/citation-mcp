"""Phase 2.D ground-truth harness — run 30 primary + 4 backup inputs through
citation-mcp via stdio, capture raw verifyCitation responses to disk for
Phase 2.E review.

No assertions made here. Each row's full response is preserved verbatim
(pretty-printed for human review). Errors are caught per-row so one bad row
doesn't abort the run.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = PROJECT_ROOT / "data" / "regression_30_baseline"
INPUTS_FILE = BASELINE_DIR / "inputs.json"
BACKUP_FILE = BASELINE_DIR / "backup_inputs.json"
RESP_DIR = BASELINE_DIR / "responses"
LOG_DIR = BASELINE_DIR / "logs"
CACHE_DB = BASELINE_DIR / "cache.db"


def _strip_nulls(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


async def _run() -> dict:
    if not INPUTS_FILE.exists():
        raise SystemExit(f"missing inputs file: {INPUTS_FILE}")
    if not BACKUP_FILE.exists():
        raise SystemExit(f"missing backup inputs file: {BACKUP_FILE}")

    primaries = json.loads(INPUTS_FILE.read_text())
    backups = json.loads(BACKUP_FILE.read_text())
    all_rows = primaries + backups

    # Required keys. Fail-fast if any is unset in the parent env.
    required_keys = ["NCBI_API_KEY", "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"]
    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing required env vars: {missing}")

    # Subprocess env: inherit current env (including the three API keys),
    # override CACHE_DB_PATH to isolate from production cache.
    sub_env = {**os.environ, "CACHE_DB_PATH": str(CACHE_DB)}

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "citation_mcp.server"],
        env=sub_env,
    )

    timings: list[tuple[str, float, bool]] = []
    n_errors = 0
    wall_start = time.monotonic()

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for i, row in enumerate(all_rows, 1):
                rid = row["row_id"]
                args = _strip_nulls(row["input"])
                t0 = time.monotonic()
                errored = False
                try:
                    result = await session.call_tool("verifyCitation", arguments=args)
                    # result.content is a list of TextContent / etc. Take the
                    # first text block — verifyCitation returns one JSON string.
                    text_payload = None
                    for block in result.content:
                        if hasattr(block, "text") and block.text:
                            text_payload = block.text
                            break
                    if text_payload is None:
                        raise RuntimeError("no text content in tool result")
                    payload = json.loads(text_payload)
                    out_path = RESP_DIR / f"{rid}.json"
                    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
                except Exception as e:
                    errored = True
                    n_errors += 1
                    err_payload = {
                        "row_id": rid,
                        "error": type(e).__name__,
                        "message": str(e),
                    }
                    (RESP_DIR / f"{rid}.json").write_text(
                        json.dumps(err_payload, indent=2, ensure_ascii=False)
                    )
                    (LOG_DIR / f"{rid}.stderr.log").write_text(traceback.format_exc())
                elapsed = time.monotonic() - t0
                timings.append((rid, elapsed, errored))
                status = "ERR" if errored else "ok"
                print(f"  [{i:2d}/{len(all_rows)}] {rid:20s} {status:3s} {elapsed:6.2f}s", flush=True)
                await asyncio.sleep(0.5)

    wall_elapsed = time.monotonic() - wall_start
    return {
        "timings": timings,
        "wall_elapsed": wall_elapsed,
        "n_errors": n_errors,
        "total_rows": len(all_rows),
    }


if __name__ == "__main__":
    summary = asyncio.run(_run())
    print()
    print(f"wall: {summary['wall_elapsed']:.1f}s   errors: {summary['n_errors']}/{summary['total_rows']}")
    # Persist timings for the report
    (BASELINE_DIR / "_timings.json").write_text(
        json.dumps({
            "wall_elapsed": summary["wall_elapsed"],
            "n_errors": summary["n_errors"],
            "total_rows": summary["total_rows"],
            "per_row": [{"row_id": r, "elapsed_s": e, "errored": x} for r, e, x in summary["timings"]],
        }, indent=2)
    )
