"""Command-line entry point for the harness OAuth client."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .oauth_client import (
    CallbackTimeoutError,
    HarnessOAuthClient,
    HarnessOAuthError,
    RefreshTokenInvalid,
    RegistrationFailed,
    StateMismatchError,
    TokensNotFoundError,
)


def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def cmd_register(_args: argparse.Namespace) -> int:
    with HarnessOAuthClient() as client:
        try:
            meta = client.register()
        except RegistrationFailed as e:
            print(f"Registration failed: {e}", file=sys.stderr)
            return 1
        client_id = meta["client_id"]
        registered_at = meta.get("registered_at", "(unknown)")
        print(
            f"Registered. client_id ends in {client_id[-8:]}. "
            f"registered_at {registered_at}."
        )
        return 0


def cmd_auth(_args: argparse.Namespace) -> int:
    with HarnessOAuthClient() as client:
        try:
            state = client.authorize_interactive()
        except StateMismatchError as e:
            print(f"State mismatch: {e}", file=sys.stderr)
            return 1
        except CallbackTimeoutError as e:
            print(f"Callback timeout: {e}", file=sys.stderr)
            return 1
        except HarnessOAuthError as e:
            print(f"Authorization failed: {e}", file=sys.stderr)
            return 1
        print(
            f"Authorization complete. "
            f"access expires {_iso(state['access_expires_at'])}. "
            f"refresh expires {_iso(state['refresh_expires_at'])}."
        )
        return 0


def cmd_refresh(_args: argparse.Namespace) -> int:
    with HarnessOAuthClient() as client:
        try:
            state = client.refresh()
        except RefreshTokenInvalid:
            print(
                "Refresh token invalid; run 'auth' to re-authenticate.",
                file=sys.stderr,
            )
            return 2
        except TokensNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 2
        except HarnessOAuthError as e:
            print(f"Refresh failed: {e}", file=sys.stderr)
            return 2
        print(
            f"Refresh complete. "
            f"access expires {_iso(state['access_expires_at'])}. "
            f"refresh expires {_iso(state['refresh_expires_at'])}."
        )
        return 0


def cmd_status(_args: argparse.Namespace) -> int:
    with HarnessOAuthClient() as client:
        try:
            s = client.status()
        except HarnessOAuthError as e:
            print(str(e), file=sys.stderr)
            return 3
        print(json.dumps(s, indent=2))
        if not s.get("access_token_present"):
            return 3
        if (s.get("access_expires_in_seconds") or 0) <= 0:
            return 3
        return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .validate import run_validation_sync

    try:
        report_path, gate_passed = run_validation_sync(
            cold_only=args.cold_only,
            warm_only=args.warm_only,
        )
    except SystemExit:
        raise
    except HarnessOAuthError as e:
        print(f"OAuth error: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — orchestration boundary
        print(f"Validation harness failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    print(f"Report written: {report_path}")
    if gate_passed:
        print("v1.0 gate: PASS")
        return 0
    print("v1.0 gate: FAIL (see report)", file=sys.stderr)
    return 1


def cmd_token(_args: argparse.Namespace) -> int:
    with HarnessOAuthClient() as client:
        try:
            token = client.get_access_token()
        except TokensNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 2
        except RefreshTokenInvalid:
            print(
                "Refresh token invalid; run 'auth' to re-authenticate.",
                file=sys.stderr,
            )
            return 2
        except HarnessOAuthError as e:
            print(f"Token retrieval failed: {e}", file=sys.stderr)
            return 2
        print(token, end="")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("register", help="Dynamic Client Registration (idempotent)")
    sub.add_parser("auth", help="Interactive PKCE authorization flow")
    sub.add_parser("refresh", help="Force-refresh access + refresh tokens")
    sub.add_parser("status", help="Show token status (JSON, no raw secrets)")
    sub.add_parser("token", help="Print a currently valid access token")

    validate_parser = sub.add_parser(
        "validate", help="Run citation-mcp validation harness"
    )
    validate_parser.add_argument(
        "--cold-only",
        action="store_true",
        help="Run only the cold-cache pass (debug mode; gate fails)",
    )
    validate_parser.add_argument(
        "--warm-only",
        action="store_true",
        help="Run only the warm-cache pass (debug mode; gate fails)",
    )

    args = parser.parse_args(argv)
    handlers = {
        "register": cmd_register,
        "auth": cmd_auth,
        "refresh": cmd_refresh,
        "status": cmd_status,
        "token": cmd_token,
        "validate": cmd_validate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
