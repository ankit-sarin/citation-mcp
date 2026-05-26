"""HTTP URL query-string redaction for httpx logging.

httpx emits request URLs at INFO. Two of our backends authenticate via
query parameters — NCBI E-utilities (`api_key=`) and OpenAlex (`api_key=`) —
which would otherwise leak the key into log files and terminal scrollback.

`QueryParamRedactionFilter` rewrites the value of any sensitive query
parameter to `***` before the log record is emitted. `install_redaction_filter()`
attaches it idempotently to `logging.getLogger("httpx")` and is invoked from
the package `__init__`. Header-based auth (e.g. Semantic Scholar's
`x-api-key`) is unaffected because httpx does not log request headers at
INFO; see Phase 1.D.2 for body / header redaction.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# The leading [?&;] anchor is what prevents `?keyword=foo` from matching:
# the regex requires one of the listed names to start immediately after a
# separator, not just to appear as a substring of a longer name.
_REDACTION_RE = re.compile(
    r"([?&;](?:api[-_]?key|apikey|key|token|access_token|refresh_token|client_secret)=)([^&\s]+)",
    flags=re.IGNORECASE,
)
_REPLACEMENT = r"\1***"


def _redact(value: str) -> str:
    return _REDACTION_RE.sub(_REPLACEMENT, value)


def _maybe_redact(value: Any) -> Any:
    """Apply redaction; preserve the original object when nothing changes.

    Accepts str directly. For other objects (e.g. ``httpx.URL``), stringifies
    and redacts only if the regex matches — otherwise the original object is
    returned so ``%s`` formatting preserves its native repr.
    """
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return value
    redacted = _REDACTION_RE.sub(_REPLACEMENT, s)
    if redacted == s:
        return value
    return redacted


class QueryParamRedactionFilter(logging.Filter):
    """logging.Filter that scrubs sensitive query-string values in URLs.

    Operates on both ``record.msg`` (when it is a pre-formatted str) and
    ``record.args`` (when the message is a format string with positional or
    keyword args — httpx's common case). Never raises: any internal error
    leaves the record unmodified.
    """

    # In-place mutation of record.msg / record.args is intentional: once a
    # record is redacted here, any downstream handler that re-emits it stays
    # redacted, which is the safe direction.
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str):
                record.msg = _redact(record.msg)
            args = record.args
            if args:
                if isinstance(args, dict):
                    record.args = {k: _maybe_redact(v) for k, v in args.items()}
                elif isinstance(args, tuple):
                    record.args = tuple(_maybe_redact(v) for v in args)
        except Exception:
            return True
        return True


def install_redaction_filter(logger_name: str = "httpx") -> QueryParamRedactionFilter | None:
    """Idempotently install the redaction filter on the given logger.

    Returns the filter instance (newly installed or pre-existing). A second
    call on the same logger reuses the already-attached filter rather than
    stacking a second one.
    """
    logger = logging.getLogger(logger_name)
    for existing in logger.filters:
        if isinstance(existing, QueryParamRedactionFilter):
            return existing
    f = QueryParamRedactionFilter()
    logger.addFilter(f)
    return f
