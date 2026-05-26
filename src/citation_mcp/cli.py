"""citation-mcp CLI — dispatches to stdio or HTTP transport.

Default transport is stdio (preserves all existing local-Claude-Code harness
behavior). --transport http boots a Starlette + uvicorn app exposing OAuth
2.1 endpoints and the MCP server at /mcp.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _stdio_main() -> None:
    # Imported lazily so the HTTP path doesn't drag in the stdio session
    # plumbing — and so an HTTP-only environment without a TTY won't trip
    # initialization warnings.
    from .server import main as stdio_main

    stdio_main()


def _http_main(host: str, port: int) -> None:
    import uvicorn

    from .http_app import build_http_app_from_env

    log_level = os.environ.get("CITATION_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = build_http_app_from_env()
    uvicorn.run(app, host=host, port=port, log_level=log_level.lower())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="citation-mcp",
        description="citation-mcp server: stdio MCP (default) or HTTP transport.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport to use (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"),
        help="Bind host for HTTP transport (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_HTTP_PORT", "8080")),
        help="Bind port for HTTP transport (default: 8080).",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        _stdio_main()
        return

    # HTTP transport: validate required env up front so we fail fast with a
    # clear remediation hint instead of a stack trace deep in startup.
    signing_key = os.environ.get("OAUTH_SIGNING_KEY")
    if not signing_key:
        print(
            "error: OAUTH_SIGNING_KEY is required for --transport http\n"
            "       generate one with: openssl rand -hex 32",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        decoded = bytes.fromhex(signing_key)
    except ValueError:
        print(
            "error: OAUTH_SIGNING_KEY must be hex-encoded\n"
            "       generate one with: openssl rand -hex 32",
            file=sys.stderr,
        )
        sys.exit(2)
    if len(decoded) < 32:
        print(
            "error: OAUTH_SIGNING_KEY must decode to at least 32 bytes\n"
            "       generate one with: openssl rand -hex 32",
            file=sys.stderr,
        )
        sys.exit(2)

    _http_main(args.host, args.port)


if __name__ == "__main__":
    main()
