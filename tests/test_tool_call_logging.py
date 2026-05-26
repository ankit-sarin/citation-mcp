"""Regression tests for per-tool entry INFO logging (v0.3.3 Gap 2)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from citation_mcp.server import (
    AppContext,
    bulk_verify_citations_tool,
    resolve_identifier_tool,
    verify_citation_tool,
)

from .conftest import POLACK_DOI

SENTINEL_DOI = "10.9999/sentinel-do-not-log-arguments"


def _make_ctx(clients: dict, cache_obj) -> SimpleNamespace:
    app_ctx = AppContext(
        crossref=clients["crossref"],
        pubmed=clients["pubmed"],
        openalex=clients["openalex"],
        semantic_scholar=clients["semantic_scholar"],
        arxiv=clients["arxiv"],
        cache=cache_obj,
    )
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=app_ctx)
    )


async def test_tool_call_entry_logs_name_only(polack_multi_db, cache, caplog) -> None:
    ctx = _make_ctx(polack_multi_db, cache)
    caplog.set_level(logging.INFO, logger="citation_mcp")

    await verify_citation_tool(ctx, doi=SENTINEL_DOI)
    await bulk_verify_citations_tool(ctx, citations=[{"doi": POLACK_DOI}])
    await resolve_identifier_tool(ctx, identifier=POLACK_DOI, from_type="doi")

    assert "tool_call name=verifyCitation" in caplog.text
    assert "tool_call name=bulkVerifyCitations" in caplog.text
    assert "tool_call name=resolveIdentifier" in caplog.text

    verify_records = [
        r for r in caplog.records
        if r.name == "citation_mcp"
        and r.getMessage().startswith("tool_call name=verifyCitation")
    ]
    assert len(verify_records) == 1
    assert SENTINEL_DOI not in verify_records[0].getMessage()
