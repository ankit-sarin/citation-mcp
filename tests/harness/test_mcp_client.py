"""Unit tests for harness.mcp_client.

The MCP SDK's transport + session pair is too complex to mock at the HTTP
layer cleanly. Instead we monkeypatch the `streamable_http_client` factory
and `ClientSession` class at module scope with lightweight fakes that
mirror the SDK's async-context-manager protocol. Tests inject canned
behaviors per attempt (success, raise HTTPStatusError, return isError, etc).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import httpx
import pytest

from harness import mcp_client as mc
from harness.mcp_client import (
    ToolCallOutcome,
    ToolError,
    TransportError,
    call_bulk_with_refresh_retry,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _Result:
    def __init__(self, content: list, isError: bool = False) -> None:
        self.content = content
        self.isError = isError


def _make_result(payload: dict, isError: bool = False) -> _Result:
    return _Result(content=[_Block(text=json.dumps(payload))], isError=isError)


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://test.example/mcp")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"{code}", request=req, response=resp)


class FakeSession:
    """Drop-in for mcp.ClientSession with per-test behavior."""

    def __init__(self, read, write) -> None:
        self.read = read
        self.write = write
        # behavior is patched into the class by tests via _install_behaviour
        self.calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict | None = None):
        self.calls.append((name, arguments))
        return await type(self)._behaviour(self, name, arguments)

    # Default behaviour — tests override on the class.
    @staticmethod
    async def _behaviour(self, name, arguments):
        raise RuntimeError("FakeSession behaviour not configured for this test")


class FakeOAuth:
    """Minimal drop-in for HarnessOAuthClient with controllable token state."""

    def __init__(self, token: str = "TOKEN_INITIAL") -> None:
        self._token = token
        self.get_calls = 0
        self.refresh_calls = 0

    def get_access_token(self, **_kwargs) -> str:
        self.get_calls += 1
        return self._token

    def refresh(self) -> dict:
        self.refresh_calls += 1
        self._token = "TOKEN_REFRESHED"
        return {"access_token": self._token}


def _install_factory(monkeypatch, session_factory):
    """Patch streamable_http_client + ClientSession into mcp_client.

    session_factory(read, write) -> FakeSession instance. We keep a reference
    to the last constructed session so tests can introspect its `.calls`.
    """
    state: dict = {"last_session": None}

    @contextlib.asynccontextmanager
    async def fake_streamable(url: str, *, http_client=None):
        state["url"] = url
        state["http_client"] = http_client
        yield (object(), object(), lambda: None)

    def session_ctor(read, write, *args, **kwargs):
        s = session_factory(read, write)
        state["last_session"] = s
        return s

    monkeypatch.setattr(mc, "streamable_http_client", fake_streamable)
    monkeypatch.setattr(mc, "ClientSession", session_ctor)
    return state


def _set_behaviour(behaviour):
    """Assign the static behaviour callable used by FakeSession.call_tool."""
    FakeSession._behaviour = staticmethod(behaviour)


@pytest.fixture(autouse=True)
def _reset_behaviour():
    yield
    # Restore the no-behaviour default after each test.
    async def _default(self, name, arguments):
        raise RuntimeError("FakeSession behaviour not configured")

    FakeSession._behaviour = staticmethod(_default)


VALID_PAYLOAD = {
    "results": [
        {
            "verified": True,
            "match_quality": "high",
            "canonical": {"doi": "10.0/test"},
            "warnings": [],
            "discrepancies": [],
            "databases_failed": [],
        }
    ],
    "summary": {
        "total": 1,
        "verified": 1,
        "by_match_quality": {"high": 1, "medium": 0, "low": 0, "none": 0},
        "with_warnings": 0,
        "cache_hits": 0,
        "elapsed_seconds": 0.123,
    },
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_successful_call_returns_outcome(monkeypatch):
    state = _install_factory(monkeypatch, FakeSession)

    async def behaviour(self, name, arguments):
        return _make_result(VALID_PAYLOAD)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    outcome = _run(
        call_bulk_with_refresh_retry(
            oauth, "http://test.example/mcp",
            citations=[{"doi": "10.0/x"}], force_refresh=True,
        )
    )
    assert isinstance(outcome, ToolCallOutcome)
    assert outcome.payload == VALID_PAYLOAD
    assert outcome.wall_clock_seconds > 0
    assert outcome.refreshed_token is False
    assert oauth.refresh_calls == 0


def test_401_triggers_refresh_and_retry(monkeypatch):
    state = _install_factory(monkeypatch, FakeSession)
    attempts = {"n": 0}

    async def behaviour(self, name, arguments):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_status_error(401)
        return _make_result(VALID_PAYLOAD)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    outcome = _run(
        call_bulk_with_refresh_retry(
            oauth, "http://test.example/mcp",
            citations=[{"doi": "10.0/x"}], force_refresh=False,
        )
    )
    assert outcome.refreshed_token is True
    assert oauth.refresh_calls == 1
    assert attempts["n"] == 2


def test_second_401_raises_transport_error(monkeypatch):
    _install_factory(monkeypatch, FakeSession)

    async def behaviour(self, name, arguments):
        raise _http_status_error(401)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    with pytest.raises(TransportError) as exc_info:
        _run(
            call_bulk_with_refresh_retry(
                oauth, "http://test.example/mcp",
                citations=[{"doi": "10.0/x"}], force_refresh=False,
            )
        )
    assert "OAuth refresh did not recover from 401" in str(exc_info.value)
    assert oauth.refresh_calls == 1


def test_non_401_transport_error_raised(monkeypatch):
    _install_factory(monkeypatch, FakeSession)

    async def behaviour(self, name, arguments):
        raise _http_status_error(500)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    with pytest.raises(TransportError):
        _run(
            call_bulk_with_refresh_retry(
                oauth, "http://test.example/mcp",
                citations=[{"doi": "10.0/x"}], force_refresh=False,
            )
        )
    assert oauth.refresh_calls == 0


def test_tool_is_error_raises_tool_error(monkeypatch):
    _install_factory(monkeypatch, FakeSession)

    async def behaviour(self, name, arguments):
        return _make_result({"explanation": "something went wrong"}, isError=True)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    with pytest.raises(ToolError) as exc_info:
        _run(
            call_bulk_with_refresh_retry(
                oauth, "http://test.example/mcp",
                citations=[{"doi": "10.0/x"}], force_refresh=False,
            )
        )
    assert exc_info.value.payload == {"explanation": "something went wrong"}


def test_validation_error_in_payload_raises_tool_error(monkeypatch):
    _install_factory(monkeypatch, FakeSession)

    async def behaviour(self, name, arguments):
        return _make_result(
            {"error": "invalid_input", "message": "citations cannot be empty"}
        )

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    with pytest.raises(ToolError) as exc_info:
        _run(
            call_bulk_with_refresh_retry(
                oauth, "http://test.example/mcp",
                citations=[], force_refresh=False,
            )
        )
    assert "citations cannot be empty" in str(exc_info.value)


def test_force_refresh_passed_to_call_tool(monkeypatch):
    state = _install_factory(monkeypatch, FakeSession)

    async def behaviour(self, name, arguments):
        return _make_result(VALID_PAYLOAD)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    _run(
        call_bulk_with_refresh_retry(
            oauth, "http://test.example/mcp",
            citations=[{"doi": "10.0/x"}], force_refresh=True,
        )
    )
    last_session = state["last_session"]
    assert last_session.calls == [
        ("bulkVerifyCitations", {"citations": [{"doi": "10.0/x"}], "force_refresh": True}),
    ]


def test_no_text_content_raises_tool_error(monkeypatch):
    _install_factory(monkeypatch, FakeSession)

    async def behaviour(self, name, arguments):
        return _Result(content=[], isError=False)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    with pytest.raises(ToolError) as exc_info:
        _run(
            call_bulk_with_refresh_retry(
                oauth, "http://test.example/mcp",
                citations=[{"doi": "10.0/x"}], force_refresh=False,
            )
        )
    assert "no text content" in str(exc_info.value)


def test_wall_clock_excludes_failed_attempt(monkeypatch):
    _install_factory(monkeypatch, FakeSession)
    attempts = {"n": 0}

    async def behaviour(self, name, arguments):
        attempts["n"] += 1
        if attempts["n"] == 1:
            await asyncio.sleep(0.1)
            raise _http_status_error(401)
        return _make_result(VALID_PAYLOAD)

    _set_behaviour(behaviour)

    oauth = FakeOAuth()
    outcome = _run(
        call_bulk_with_refresh_retry(
            oauth, "http://test.example/mcp",
            citations=[{"doi": "10.0/x"}], force_refresh=False,
        )
    )
    # The 100ms sleep happens inside the failed first attempt. The reported
    # wall-clock must only cover the successful second attempt.
    assert outcome.wall_clock_seconds < 0.05
    assert outcome.refreshed_token is True
