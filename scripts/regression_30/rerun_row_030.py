"""Phase 2.E.2 Step 1 — re-run row_030 with the fallback title via stdio.

Preserves the original baseline as responses/row_030_original.json and
writes the new response to responses/row_030.json. CACHE_DB_PATH is
overridden to the regression baseline cache so the call shares the same
cache state as the rest of the baseline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = PROJECT_ROOT / "data" / "regression_30_baseline"
RESP_DIR = BASELINE_DIR / "responses"
CACHE_DB = BASELINE_DIR / "cache.db"

FALLBACK_INPUT = {
    "title": "Comparison of robotic and laparoscopic colectomy outcomes",
    "year": 2022,
}


async def _run() -> dict:
    required_keys = ["NCBI_API_KEY", "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"]
    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing required env vars: {missing}")

    sub_env = {**os.environ, "CACHE_DB_PATH": str(CACHE_DB)}
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "citation_mcp.server"],
        env=sub_env,
    )

    original_path = RESP_DIR / "row_030.json"
    preserved_path = RESP_DIR / "row_030_original.json"

    if not original_path.exists():
        raise SystemExit(f"missing original baseline: {original_path}")

    # Preserve original (idempotent — don't overwrite an existing preservation).
    if not preserved_path.exists():
        preserved_path.write_text(original_path.read_text())

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("verifyCitation", arguments=FALLBACK_INPUT)
            text_payload = None
            for block in result.content:
                if hasattr(block, "text") and block.text:
                    text_payload = block.text
                    break
            if text_payload is None:
                raise RuntimeError("no text content in tool result")
            payload = json.loads(text_payload)

    original_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


if __name__ == "__main__":
    payload = asyncio.run(_run())
    verified = payload.get("verified")
    rejected_by = payload.get("score_breakdown", {}).get("rejected_by")
    mq = payload.get("match_quality")
    conf = payload.get("confidence")
    path = "A" if verified is False else "B"
    print(f"row_030 re-run: verified={verified}  match_quality={mq}  "
          f"confidence={conf}  rejected_by={rejected_by}  path={path}")
