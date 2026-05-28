"""OAuth 2.1 PKCE client for the citation-mcp harness."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import queue
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

REDIRECT_URI = "http://127.0.0.1:8765/callback"
CALLBACK_PORT = 8765
REFRESH_TTL_SECONDS = 30 * 86400
HTTP_TIMEOUT = 30.0


class HarnessOAuthError(Exception):
    """Base class for harness OAuth errors."""


class TokensNotFoundError(HarnessOAuthError):
    """tokens.json is missing — caller must run authorize_interactive first."""


class StateMismatchError(HarnessOAuthError):
    """OAuth state parameter returned by AS did not match the value we sent."""


class CallbackTimeoutError(HarnessOAuthError):
    """Browser callback did not arrive within the configured timeout."""


class RefreshTokenInvalid(HarnessOAuthError):
    """Refresh-grant rejected by AS (expired, revoked, or replayed)."""


class RegistrationFailed(HarnessOAuthError):
    """Dynamic Client Registration POST failed."""


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.chmod(str(tmp), 0o600)
    os.rename(str(tmp), str(path))


class _CallbackHandlerBase(http.server.BaseHTTPRequestHandler):
    callback_queue: "queue.Queue[dict[str, str]]" = None  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Authorization complete.</h2>"
            b"<p>You may close this window.</p></body></html>"
        )
        self.callback_queue.put(params)

    def log_message(self, format, *args):  # noqa: A002
        return


def _make_handler(q: "queue.Queue[dict[str, str]]") -> type:
    return type(
        "_CallbackHandler",
        (_CallbackHandlerBase,),
        {"callback_queue": q},
    )


def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


class HarnessOAuthClient:
    """OAuth 2.1 PKCE Authorization Code client for the harness."""

    def __init__(
        self,
        base_url: str = "https://citation-mcp.digitalsurgeon.dev",
        config_dir: Path | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if config_dir is None:
            self.config_dir = Path.home() / ".config" / "citation-mcp" / "harness"
        else:
            self.config_dir = Path(config_dir)
        self._http = http_client
        self._owns_http = http_client is None
        self._as_metadata: dict | None = None
        self._client_metadata: dict | None = None

    def __enter__(self) -> "HarnessOAuthClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    @property
    def client_json_path(self) -> Path:
        return self.config_dir / "client.json"

    @property
    def tokens_json_path(self) -> Path:
        return self.config_dir / "tokens.json"

    @property
    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT)
        return self._http

    def _get_as_metadata(self) -> dict:
        if self._as_metadata is not None:
            return self._as_metadata
        url = f"{self.base_url}/.well-known/oauth-authorization-server"
        r = self._client.get(url)
        r.raise_for_status()
        self._as_metadata = r.json()
        return self._as_metadata

    def _load_client_metadata(self) -> dict | None:
        if self._client_metadata is not None:
            return self._client_metadata
        if not self.client_json_path.exists():
            return None
        with open(self.client_json_path) as f:
            self._client_metadata = json.load(f)
        return self._client_metadata

    def _load_tokens(self) -> dict:
        if not self.tokens_json_path.exists():
            raise TokensNotFoundError(
                f"No tokens at {self.tokens_json_path}. Run 'auth' first."
            )
        with open(self.tokens_json_path) as f:
            return json.load(f)

    def register(self) -> dict:
        existing = self._load_client_metadata()
        if existing and existing.get("client_id"):
            return existing
        meta = self._get_as_metadata()
        body = {
            "client_name": "citation-mcp-harness",
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "mcp",
        }
        r = self._client.post(meta["registration_endpoint"], json=body)
        if r.status_code >= 400:
            raise RegistrationFailed(
                f"DCR failed: {r.status_code} {r.text}"
            )
        data = r.json()
        if "registered_at" not in data:
            data["registered_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(self.client_json_path, json.dumps(data, indent=2))
        self._client_metadata = data
        return data

    def authorize_interactive(self, timeout: int = 300) -> dict:
        client_meta = self._load_client_metadata()
        if not client_meta or not client_meta.get("client_id"):
            raise HarnessOAuthError(
                "Client not registered. Call register() first."
            )
        client_id = client_meta["client_id"]
        as_meta = self._get_as_metadata()

        verifier = secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(24)

        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "mcp",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = f"{as_meta['authorization_endpoint']}?{urlencode(params)}"

        q: "queue.Queue[dict[str, str]]" = queue.Queue()
        handler_cls = _make_handler(q)
        server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            print(f"Open this URL in your browser:\n{url}\n")
            try:
                webbrowser.open(url)
            except Exception:
                pass

            try:
                callback_params = q.get(timeout=timeout)
            except queue.Empty:
                raise CallbackTimeoutError(
                    f"No callback received within {timeout}s"
                )

            if callback_params.get("state") != state:
                raise StateMismatchError(
                    "State parameter mismatch (possible CSRF)"
                )
            code = callback_params.get("code")
            if not code:
                err = callback_params.get("error", "missing-code")
                raise HarnessOAuthError(
                    f"Callback did not deliver a code: {err}"
                )
        finally:
            server.shutdown()
            server.server_close()

        token_form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": client_id,
        }
        r = self._client.post(as_meta["token_endpoint"], data=token_form)
        if r.status_code >= 400:
            try:
                err_body = r.json()
            except Exception:
                err_body = {"error": "unknown", "error_description": r.text}
            raise HarnessOAuthError(
                f"Token exchange failed: {r.status_code} {err_body}"
            )
        token_resp = r.json()
        now = int(time.time())
        state_dict = {
            "access_token": token_resp["access_token"],
            "refresh_token": token_resp["refresh_token"],
            "access_expires_at": now + int(token_resp["expires_in"]),
            "refresh_expires_at": now + REFRESH_TTL_SECONDS,
            "token_type": token_resp.get("token_type", "Bearer"),
            "obtained_at": now,
        }
        _atomic_write(self.tokens_json_path, json.dumps(state_dict, indent=2))
        return state_dict

    def refresh(self) -> dict:
        tokens = self._load_tokens()
        client_meta = self._load_client_metadata()
        if not client_meta:
            raise HarnessOAuthError(
                "Client metadata missing. Re-register before refreshing."
            )
        as_meta = self._get_as_metadata()
        body = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_meta["client_id"],
        }
        r = self._client.post(as_meta["token_endpoint"], data=body)
        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                err = {"error": "invalid_grant", "error_description": r.text}
            raise RefreshTokenInvalid(
                f"{err.get('error', 'invalid_grant')}: "
                f"{err.get('error_description', '')}"
            )
        token_resp = r.json()
        now = int(time.time())
        state_dict = {
            "access_token": token_resp["access_token"],
            "refresh_token": token_resp["refresh_token"],
            "access_expires_at": now + int(token_resp["expires_in"]),
            "refresh_expires_at": now + REFRESH_TTL_SECONDS,
            "token_type": token_resp.get("token_type", "Bearer"),
            "obtained_at": now,
        }
        _atomic_write(self.tokens_json_path, json.dumps(state_dict, indent=2))
        return state_dict

    def get_access_token(self, skew_seconds: int = 60) -> str:
        tokens = self._load_tokens()
        if tokens["access_expires_at"] > int(time.time()) + skew_seconds:
            return tokens["access_token"]
        new_state = self.refresh()
        return new_state["access_token"]

    def status(self) -> dict:
        client_meta = self._load_client_metadata()
        if not client_meta:
            raise HarnessOAuthError(
                "Client not registered. Run 'register' first."
            )
        client_id = client_meta["client_id"]
        result: dict[str, Any] = {
            "client_id_suffix": client_id[-8:],
            "registered_at": client_meta.get("registered_at"),
        }
        if not self.tokens_json_path.exists():
            result["access_token_present"] = False
            result["access_expires_at_iso"] = None
            result["access_expires_in_seconds"] = None
            result["refresh_expires_at_iso"] = None
            result["refresh_expires_in_seconds"] = None
            return result
        tokens = self._load_tokens()
        now = int(time.time())
        result["access_token_present"] = True
        result["access_expires_at_iso"] = _iso(tokens["access_expires_at"])
        result["access_expires_in_seconds"] = max(
            0, tokens["access_expires_at"] - now
        )
        result["refresh_expires_at_iso"] = _iso(tokens["refresh_expires_at"])
        result["refresh_expires_in_seconds"] = max(
            0, tokens["refresh_expires_at"] - now
        )
        return result
