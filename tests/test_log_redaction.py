"""Tests for the httpx URL query-param redaction filter.

Covers:
  - Regex / filter unit tests: param positions, case-insensitivity, false-
    positive guard, no-query records, type preservation, exception safety,
    idempotent install.
  - End-to-end integration: a real httpx.AsyncClient call through MockTransport
    against a stubbed NCBI E-utilities endpoint, with httpx's INFO-level log
    record captured and asserted to contain `api_key=***` rather than the
    plaintext value.
"""

from __future__ import annotations

import logging
import os

import httpx
import pytest

from citation_mcp.log_redaction import (
    QueryParamRedactionFilter,
    _redact,
    install_redaction_filter,
)


# ---------------------------------------------------------------------------
# Pure regex unit tests
# ---------------------------------------------------------------------------


def test_single_param_scrubbed():
    assert (
        _redact("https://api.example.com/v1?api_key=SECRET_XYZ")
        == "https://api.example.com/v1?api_key=***"
    )


def test_param_first_position():
    assert (
        _redact("https://api.example.com/v1?api_key=SECRET&foo=bar")
        == "https://api.example.com/v1?api_key=***&foo=bar"
    )


def test_param_last_position():
    assert (
        _redact("https://api.example.com/v1?foo=bar&api_key=SECRET")
        == "https://api.example.com/v1?foo=bar&api_key=***"
    )


def test_param_middle_position():
    assert (
        _redact("https://api.example.com/v1?foo=bar&api_key=SECRET&baz=qux")
        == "https://api.example.com/v1?foo=bar&api_key=***&baz=qux"
    )


def test_case_insensitive_match():
    # Both the param name and the variant separator chars are matched ci.
    assert _redact("https://x/y?Api_Key=SECRET") == "https://x/y?Api_Key=***"
    assert _redact("https://x/y?APIKEY=SECRET") == "https://x/y?APIKEY=***"
    assert _redact("https://x/y?api-key=SECRET") == "https://x/y?api-key=***"


def test_multiple_sensitive_params_all_scrubbed():
    out = _redact("https://x/y?api_key=A&token=B&client_secret=C")
    assert "api_key=***" in out
    assert "token=***" in out
    assert "client_secret=***" in out
    assert "A" not in out and "B" not in out and "C" not in out


def test_non_matching_params_untouched():
    s = "https://x/y?foo=bar&baz=qux&page=2"
    assert _redact(s) == s


def test_record_without_query_untouched():
    s = "Plain log message with no URL whatsoever"
    assert _redact(s) == s
    assert _redact("https://x/y/no/query") == "https://x/y/no/query"


def test_false_positive_keyword_does_not_match():
    """`?keyword=foo` must NOT scrub — `key` is a substring of `keyword`,
    but the regex anchors to the param name *immediately following* a
    separator, so the longer name is left intact."""
    s = "https://x/y?keyword=foo&tokenize=bar"
    # `key` would otherwise prefix-match `keyword`, and `token` would prefix-
    # match `tokenize` — both must be rejected because the regex requires
    # `=` immediately after the listed name.
    assert _redact(s) == s


def test_semicolon_separator_supported():
    # Some URL schemes use `;` as a query separator.
    assert (
        _redact("https://x/y;api_key=SECRET")
        == "https://x/y;api_key=***"
    )


# ---------------------------------------------------------------------------
# Filter-level tests (LogRecord shape)
# ---------------------------------------------------------------------------


def _make_record(msg, *args) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args or None, exc_info=None,
    )


def test_filter_scrubs_msg_string():
    f = QueryParamRedactionFilter()
    record = _make_record("GET https://x/y?api_key=SECRET")
    assert f.filter(record) is True
    assert "SECRET" not in record.getMessage()
    assert "api_key=***" in record.getMessage()


def test_filter_scrubs_url_in_args():
    """httpx logs URLs as one of the positional args, not pre-formatted into msg."""
    f = QueryParamRedactionFilter()
    record = _make_record(
        'HTTP Request: %s %s "HTTP/%s %d %s"',
        "GET", "https://x/y?api_key=SECRET", "1.1", 200, "OK",
    )
    assert f.filter(record) is True
    formatted = record.getMessage()
    assert "SECRET" not in formatted
    assert "api_key=***" in formatted


def test_filter_scrubs_httpx_url_object_in_args():
    """The URL arg may be an httpx.URL instance — str(URL) yields the full URL."""
    f = QueryParamRedactionFilter()
    url = httpx.URL("https://x/y?api_key=SECRET&foo=bar")
    record = _make_record("URL: %s", url)
    assert f.filter(record) is True
    formatted = record.getMessage()
    assert "SECRET" not in formatted
    assert "api_key=***" in formatted


def test_filter_never_raises_on_weird_msg():
    f = QueryParamRedactionFilter()

    class Boom:
        def __str__(self):
            raise RuntimeError("nope")

    record = _make_record("hello %s", Boom())
    # Must not raise even though str(arg) blows up.
    assert f.filter(record) is True


def test_install_is_idempotent():
    logger_name = "citation_mcp_test_idempotent"
    logger = logging.getLogger(logger_name)
    # Clean slate.
    for existing in list(logger.filters):
        if isinstance(existing, QueryParamRedactionFilter):
            logger.removeFilter(existing)

    install_redaction_filter(logger_name)
    install_redaction_filter(logger_name)
    install_redaction_filter(logger_name)

    matching = [f for f in logger.filters if isinstance(f, QueryParamRedactionFilter)]
    assert len(matching) == 1


def test_install_attaches_to_httpx_logger_by_default():
    """Auto-install at package import should have wired the filter to `httpx`."""
    logger = logging.getLogger("httpx")
    matching = [f for f in logger.filters if isinstance(f, QueryParamRedactionFilter)]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Integration test — real httpx client + MockTransport + log capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_httpx_emits_redacted_url(caplog):
    """Drive a real httpx call against a stubbed NCBI endpoint and confirm
    the captured INFO log record contains api_key=*** rather than the
    plaintext key."""
    plaintext_key = "PLAINTEXT_NCBI_KEY_DO_NOT_LEAK"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})

    # Ensure the filter is installed (package-import side effect should have
    # done this, but make the test self-contained).
    install_redaction_filter("httpx")

    httpx_logger = logging.getLogger("httpx")
    prior_level = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)
    caplog.set_level(logging.INFO, logger="httpx")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "pubmed", "term": "CRISPR", "api_key": plaintext_key},
            )
    finally:
        httpx_logger.setLevel(prior_level)

    httpx_records = [r for r in caplog.records if r.name == "httpx"]
    assert httpx_records, "no httpx log records captured"

    # Across every captured httpx record, the plaintext key must not appear,
    # and at least one record must show the redacted form.
    formatted_msgs = [r.getMessage() for r in httpx_records]
    joined = "\n".join(formatted_msgs)
    assert plaintext_key not in joined, f"plaintext key leaked into logs: {joined!r}"
    assert "api_key=***" in joined, f"redaction marker missing from logs: {joined!r}"
