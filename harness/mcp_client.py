"""Streamable-HTTP MCP client for the citation-mcp validation harness.

Single async entry point — `call_bulk_with_refresh_retry` — that opens a
transport, invokes `bulkVerifyCitations`, and retries once on a 401 after
forcing an OAuth refresh. No connection pooling across calls; each
invocation owns its own transport lifecycle.

Three failure modes are distinguished:

  * TransportError  — HTTP-layer failure not recovered by the single retry
  * ToolError       — tool returned isError=True or a 2xx payload carrying
                      {"error": ..., "message": ...}
  * HarnessMCPError — base class for the above
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .oauth_client import HarnessOAuthClient


class HarnessMCPError(Exception):
    """Base class for harness MCP-client errors."""


class ToolError(HarnessMCPError):
    """Tool returned isError=True or its payload carried an error key."""

    def __init__(self, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.payload = payload


class TransportError(HarnessMCPError):
    """Transport-layer failure not recovered by the single retry."""


@dataclass
class ToolCallOutcome:
    payload: dict
    wall_clock_seconds: float
    refreshed_token: bool


async def _attempt_call(
    server_url: str,
    access_token: str,
    citations: list[dict],
    force_refresh: bool,
    timeout_seconds: float,
) -> tuple[dict, float]:
    """One open → initialize → call_tool → close cycle.

    Returns (payload, wall_clock_seconds). Raises httpx.HTTPStatusError on
    transport failures; raises ToolError on tool-level / validation-level
    errors surfaced inside a 2xx response.
    """
    hc = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=httpx.Timeout(timeout_seconds),
    )
    start = time.monotonic()
    try:
        async with streamable_http_client(server_url, http_client=hc) as (
            read,
            write,
            _get_session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "bulkVerifyCitations",
                    arguments={
                        "citations": citations,
                        "force_refresh": force_refresh,
                    },
                )
        elapsed = time.monotonic() - start

        payload: dict | None = None
        for block in result.content:
            if hasattr(block, "text") and block.text:
                payload = json.loads(block.text)
                break
        if payload is None:
            raise ToolError("Tool returned no text content", payload=None)

        if result.isError:
            raise ToolError(
                f"Tool returned isError=True: {payload}", payload=payload
            )

        if (
            isinstance(payload, dict)
            and "error" in payload
            and "message" in payload
        ):
            raise ToolError(
                f"Tool validation error: {payload.get('message')}",
                payload=payload,
            )

        return payload, elapsed
    finally:
        await hc.aclose()


async def call_bulk_with_refresh_retry(
    oauth: HarnessOAuthClient,
    server_url: str,
    citations: list[dict],
    force_refresh: bool,
    timeout_seconds: float = 120.0,
) -> ToolCallOutcome:
    """Call bulkVerifyCitations, refreshing the OAuth token once on a 401."""
    access_token = oauth.get_access_token()
    refreshed = False
    try:
        payload, elapsed = await _attempt_call(
            server_url,
            access_token,
            citations,
            force_refresh,
            timeout_seconds,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 401:
            raise TransportError(
                f"Transport error {e.response.status_code}: {e}"
            ) from e
        oauth.refresh()
        refreshed = True
        new_token = oauth.get_access_token()
        try:
            payload, elapsed = await _attempt_call(
                server_url,
                new_token,
                citations,
                force_refresh,
                timeout_seconds,
            )
        except httpx.HTTPStatusError as e2:
            if e2.response.status_code == 401:
                raise TransportError(
                    "OAuth refresh did not recover from 401"
                ) from e2
            raise TransportError(
                f"Transport error after refresh {e2.response.status_code}: {e2}"
            ) from e2
    return ToolCallOutcome(
        payload=payload,
        wall_clock_seconds=elapsed,
        refreshed_token=refreshed,
    )
