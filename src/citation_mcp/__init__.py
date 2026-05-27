"""citation-mcp: self-hosted MCP server for citation verification, integrity, and discovery."""

from .log_redaction import install_redaction_filter

__version__ = "0.3.5"

# Install the httpx URL query-param redaction filter at import time so that
# NCBI / OpenAlex `api_key=` values never reach log handlers in plaintext.
install_redaction_filter("httpx")
