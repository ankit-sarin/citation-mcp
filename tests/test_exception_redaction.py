"""Tests for the exception-string redaction follow-up to Phase 1.D.0."""

from __future__ import annotations

import httpx
import pytest

from citation_mcp.log_redaction import (
    redact_query_string_in_text,
    reraise_redacted,
)


def test_redact_query_string_basic() -> None:
    text = "https://example.com/path?api_key=secret&other=keep"
    assert (
        redact_query_string_in_text(text)
        == "https://example.com/path?api_key=***&other=keep"
    )


def test_redact_query_string_idempotent() -> None:
    text = "https://example.com/x?api_key=ABCDEF"
    once = redact_query_string_in_text(text)
    twice = redact_query_string_in_text(once)
    assert once == "https://example.com/x?api_key=***"
    assert twice == once


def test_redact_handles_multiple_params() -> None:
    text = "https://x/y?token=abc&api-key=def&refresh_token=ghi"
    out = redact_query_string_in_text(text)
    assert "token=***" in out
    assert "api-key=***" in out
    assert "refresh_token=***" in out
    assert "abc" not in out
    assert "def" not in out
    assert "ghi" not in out


def test_redact_passes_through_text_without_url() -> None:
    text = "no urls here"
    assert redact_query_string_in_text(text) == text


def test_reraise_redacted_preserves_type_and_redacts_message() -> None:
    request = httpx.Request("GET", "https://example.com/?api_key=SECRET123")
    response = httpx.Response(500, request=request)
    err = httpx.HTTPStatusError(
        "Server error '500 Internal Server Error' for url 'https://example.com/?api_key=SECRET123'",
        request=request,
        response=response,
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        reraise_redacted(err)
    assert "api_key=***" in str(exc_info.value)
    assert "SECRET123" not in str(exc_info.value)
    # Cause chain preserved.
    assert exc_info.value.__cause__ is err
