"""Tests for the CLI --transport flag and required-env validation."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


def _python_run(*args, env_extra=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "citation_mcp.cli", *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def test_transport_help_lists_both_choices() -> None:
    result = _python_run("--help")
    assert result.returncode == 0
    assert "stdio" in result.stdout
    assert "http" in result.stdout
    assert "OAUTH_SIGNING_KEY" not in result.stdout  # help text stays terse


def test_transport_http_without_signing_key_exits_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {k: v for k, v in os.environ.items() if k != "OAUTH_SIGNING_KEY"}
    result = subprocess.run(
        [sys.executable, "-m", "citation_mcp.cli", "--transport", "http"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "OAUTH_SIGNING_KEY" in combined
    assert "openssl rand -hex 32" in combined


def test_transport_http_with_short_key_exits_with_hint() -> None:
    env = os.environ.copy()
    env["OAUTH_SIGNING_KEY"] = "deadbeef"  # only 4 bytes when hex-decoded
    result = subprocess.run(
        [sys.executable, "-m", "citation_mcp.cli", "--transport", "http"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "openssl rand -hex 32" in combined


def test_build_http_app_returns_starlette_with_valid_env() -> None:
    """When OAUTH_SIGNING_KEY is set, the http_app factory builds cleanly."""
    from starlette.applications import Starlette

    from citation_mcp.auth.storage import OAuthStorage
    from citation_mcp.http_app import build_http_app

    storage = OAuthStorage(":memory:")
    app = build_http_app(
        signing_key="a" * 64,
        issuer="https://example.com",
        audience="https://example.com",
        storage=storage,
    )
    assert isinstance(app, Starlette)
