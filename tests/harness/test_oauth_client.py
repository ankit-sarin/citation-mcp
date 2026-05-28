"""Unit tests for harness.oauth_client.HarnessOAuthClient.

HTTP is mocked via httpx.MockTransport injected through the client's
http_client constructor parameter — matches the project's existing
mocking convention (see tests/conftest.py).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import stat
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from harness import oauth_client as oc
from harness.oauth_client import (
    CallbackTimeoutError,
    HarnessOAuthClient,
    RefreshTokenInvalid,
    StateMismatchError,
)

AS_METADATA = {
    "issuer": "https://test.example",
    "authorization_endpoint": "https://test.example/oauth/authorize",
    "token_endpoint": "https://test.example/oauth/token",
    "registration_endpoint": "https://test.example/oauth/register",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
}


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, str, callable]] = []

    def route(self, method: str, url: str, handler) -> None:
        self._routes.append((method, url, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, url, handler in self._routes:
            if request.method == method and str(request.url) == url:
                return handler(request)
        return httpx.Response(404, json={"error": "not_mocked", "url": str(request.url)})

    def count_path(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def _route_as_metadata(rec: _Recorder) -> None:
    rec.route(
        "GET",
        "https://test.example/.well-known/oauth-authorization-server",
        lambda r: httpx.Response(200, json=AS_METADATA),
    )


def _make_client(rec: _Recorder, config_dir: Path) -> HarnessOAuthClient:
    transport = httpx.MockTransport(rec)
    http_client = httpx.Client(transport=transport, timeout=10.0)
    return HarnessOAuthClient(
        base_url="https://test.example",
        config_dir=config_dir,
        http_client=http_client,
    )


def _patch_secrets(monkeypatch, verifier: str, state: str) -> None:
    def fake(nbytes=None):
        if nbytes == 32:
            return verifier
        if nbytes == 24:
            return state
        return "_other_"

    monkeypatch.setattr(oc.secrets, "token_urlsafe", fake)


def _fire_callback(query: str) -> None:
    """Open one GET against the harness's local callback server with retries."""
    last_err: Exception | None = None
    for _ in range(80):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=2)
            conn.request("GET", f"/callback?{query}")
            conn.getresponse().read()
            conn.close()
            return
        except (ConnectionRefusedError, OSError) as e:
            last_err = e
            time.sleep(0.05)
    raise RuntimeError(f"callback server never came up: {last_err}")


# --- Test 1 -----------------------------------------------------------------


def test_register_persists_client_json(tmp_path):
    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/register",
        lambda r: httpx.Response(
            201,
            json={
                "client_id": "abcd1234EFGH5678",
                "client_name": "citation-mcp-harness",
                "redirect_uris": ["http://127.0.0.1:8765/callback"],
                "token_endpoint_auth_method": "none",
            },
        ),
    )
    client = _make_client(rec, tmp_path / "cfg")
    client.register()
    p = client.client_json_path
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["client_id"] == "abcd1234EFGH5678"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


# --- Test 2 -----------------------------------------------------------------


def test_register_idempotent_when_client_json_exists(tmp_path):
    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/register",
        lambda r: httpx.Response(500, json={"error": "must_not_be_called"}),
    )
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    existing = {
        "client_id": "preexisting_cid_12345",
        "client_name": "citation-mcp-harness",
        "redirect_uris": ["http://127.0.0.1:8765/callback"],
        "token_endpoint_auth_method": "none",
        "registered_at": "2026-01-01T00:00:00+00:00",
    }
    p = config_dir / "client.json"
    p.write_text(json.dumps(existing, indent=2))
    p.chmod(0o600)

    client = _make_client(rec, config_dir)
    result = client.register()
    assert result == existing
    assert rec.count_path("/oauth/register") == 0


# --- Test 3 -----------------------------------------------------------------


def test_authorize_pkce_challenge_correct(tmp_path, monkeypatch):
    verifier = "verifier_for_pkce_conformance_check_xyz"
    state = "state_known_value"
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    _patch_secrets(monkeypatch, verifier, state)

    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/token",
        lambda r: httpx.Response(
            200,
            json={
                "access_token": "at_x",
                "refresh_token": "rt_x",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        ),
    )

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        oc.webbrowser, "open", lambda url: captured.setdefault("url", url)
    )

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({"client_id": "cid_pkce_test"})
    )

    client = _make_client(rec, config_dir)

    threading.Thread(
        target=_fire_callback,
        args=(f"code=AUTH_CODE&state={state}",),
        daemon=True,
    ).start()

    client.authorize_interactive(timeout=10)

    assert "url" in captured
    qs = parse_qs(urlparse(captured["url"]).query)
    assert qs["code_challenge"][0] == expected_challenge
    assert qs["code_challenge_method"][0] == "S256"


# --- Test 4 -----------------------------------------------------------------


def test_authorize_state_mismatch_rejected(tmp_path, monkeypatch):
    _patch_secrets(monkeypatch, "ver", "STATE_GOOD")
    monkeypatch.setattr(oc.webbrowser, "open", lambda url: None)

    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/token",
        lambda r: httpx.Response(500, json={"error": "must_not_be_called"}),
    )
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / "client.json").write_text(json.dumps({"client_id": "cid"}))

    client = _make_client(rec, config_dir)

    threading.Thread(
        target=_fire_callback,
        args=("code=AC&state=STATE_BAD",),
        daemon=True,
    ).start()

    with pytest.raises(StateMismatchError):
        client.authorize_interactive(timeout=10)
    assert rec.count_path("/oauth/token") == 0


# --- Test 5 -----------------------------------------------------------------


def test_authorize_callback_timeout(tmp_path, monkeypatch):
    _patch_secrets(monkeypatch, "ver", "st")
    monkeypatch.setattr(oc.webbrowser, "open", lambda url: None)

    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/token",
        lambda r: httpx.Response(500, json={"error": "must_not_be_called"}),
    )
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / "client.json").write_text(json.dumps({"client_id": "cid"}))

    client = _make_client(rec, config_dir)

    with pytest.raises(CallbackTimeoutError):
        client.authorize_interactive(timeout=1)
    assert rec.count_path("/oauth/token") == 0


# --- Test 6 -----------------------------------------------------------------


def test_authorize_persists_tokens_atomically(tmp_path, monkeypatch):
    _patch_secrets(monkeypatch, "ver", "st_atomic")
    monkeypatch.setattr(oc.webbrowser, "open", lambda url: None)

    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/token",
        lambda r: httpx.Response(
            200,
            json={
                "access_token": "AT_ATOMIC",
                "refresh_token": "RT_ATOMIC",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        ),
    )

    rename_calls: list[tuple[str, str]] = []
    orig_rename = os.rename

    def spy(src, dst):
        rename_calls.append((str(src), str(dst)))
        return orig_rename(src, dst)

    monkeypatch.setattr(oc.os, "rename", spy)

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / "client.json").write_text(json.dumps({"client_id": "cid"}))

    client = _make_client(rec, config_dir)
    threading.Thread(
        target=_fire_callback,
        args=("code=AC&state=st_atomic",),
        daemon=True,
    ).start()
    client.authorize_interactive(timeout=10)

    tokens_path = config_dir / "tokens.json"
    assert tokens_path.exists()
    body = json.loads(tokens_path.read_text())
    assert body["access_token"] == "AT_ATOMIC"
    assert body["refresh_token"] == "RT_ATOMIC"
    assert stat.S_IMODE(tokens_path.stat().st_mode) == 0o600

    matching = [
        (src, dst)
        for src, dst in rename_calls
        if dst == str(tokens_path) and src.endswith(".tmp")
    ]
    assert matching, f"expected tmp→tokens.json rename, saw {rename_calls}"


# --- Test 7 -----------------------------------------------------------------


def test_refresh_rotates_tokens(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / "client.json").write_text(json.dumps({"client_id": "cid"}))
    now = int(time.time())
    (config_dir / "tokens.json").write_text(
        json.dumps(
            {
                "access_token": "OLD_A",
                "refresh_token": "OLD",
                "access_expires_at": now + 100,
                "refresh_expires_at": now + 100000,
                "token_type": "Bearer",
                "obtained_at": now - 100,
            }
        )
    )

    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/token",
        lambda r: httpx.Response(
            200,
            json={
                "access_token": "NEW_A",
                "refresh_token": "NEW_R",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        ),
    )
    client = _make_client(rec, config_dir)
    client.refresh()

    file_text = (config_dir / "tokens.json").read_text()
    assert "NEW_A" in file_text
    assert "NEW_R" in file_text
    assert "OLD" not in file_text


# --- Test 8 -----------------------------------------------------------------


def test_refresh_invalid_does_not_modify_tokens_file(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / "client.json").write_text(json.dumps({"client_id": "cid"}))
    now = int(time.time())
    body = json.dumps(
        {
            "access_token": "A",
            "refresh_token": "R",
            "access_expires_at": now + 100,
            "refresh_expires_at": now + 100000,
            "token_type": "Bearer",
            "obtained_at": now - 100,
        }
    )
    (config_dir / "tokens.json").write_text(body)
    original_bytes = (config_dir / "tokens.json").read_bytes()

    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/token",
        lambda r: httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "refresh token revoked",
            },
        ),
    )
    client = _make_client(rec, config_dir)
    with pytest.raises(RefreshTokenInvalid):
        client.refresh()
    assert (config_dir / "tokens.json").read_bytes() == original_bytes


# --- Test 9 -----------------------------------------------------------------


def test_get_access_token_uses_cache_when_valid(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / "client.json").write_text(json.dumps({"client_id": "cid"}))
    now = int(time.time())
    (config_dir / "tokens.json").write_text(
        json.dumps(
            {
                "access_token": "VALID_CACHED_TOKEN",
                "refresh_token": "R",
                "access_expires_at": now + 3600,
                "refresh_expires_at": now + 100000,
                "token_type": "Bearer",
                "obtained_at": now,
            }
        )
    )

    rec = _Recorder()
    _route_as_metadata(rec)
    rec.route(
        "POST",
        "https://test.example/oauth/token",
        lambda r: httpx.Response(
            500, json={"error": "must_not_be_called"}
        ),
    )
    client = _make_client(rec, config_dir)
    token = client.get_access_token()
    assert token == "VALID_CACHED_TOKEN"
    assert rec.count_path("/oauth/token") == 0


# --- Test 10 ----------------------------------------------------------------


def test_status_redacts_secrets(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700, parents=True)
    client_id = "client_id_abcdef_XYZ12345"  # last 8 = "XYZ12345"
    (config_dir / "client.json").write_text(
        json.dumps(
            {
                "client_id": client_id,
                "registered_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    now = int(time.time())
    access_secret = "DO_NOT_LEAK_ACCESS_SECRET"
    refresh_secret = "DO_NOT_LEAK_REFRESH_SECRET"
    (config_dir / "tokens.json").write_text(
        json.dumps(
            {
                "access_token": access_secret,
                "refresh_token": refresh_secret,
                "access_expires_at": now + 100,
                "refresh_expires_at": now + 100000,
                "token_type": "Bearer",
                "obtained_at": now,
            }
        )
    )

    rec = _Recorder()
    _route_as_metadata(rec)
    client = _make_client(rec, config_dir)
    result = client.status()
    blob = json.dumps(result)
    assert access_secret not in blob
    assert refresh_secret not in blob
    assert result["client_id_suffix"] == "XYZ12345"
    assert len(result["client_id_suffix"]) == 8
