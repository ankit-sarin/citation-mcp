"""MCP server (stdio) for citation-mcp.

Exposes a single tool — verifyCitation — backed by Crossref + SQLite cache.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from mcp.server.fastmcp import Context, FastMCP

from .cache import Cache, make_cache_key
from .databases.crossref import CrossrefClient
from .scoring import normalize_author_surname, score_match

logger = logging.getLogger("citation_mcp")

# ---------------------------------------------------------------------------
# Shared dependencies (instantiated in the server lifespan)
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    crossref: CrossrefClient
    cache: Cache


@asynccontextmanager
async def _app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    crossref = CrossrefClient()
    cache = Cache()
    await cache.init()
    try:
        yield AppContext(crossref=crossref, cache=cache)
    finally:
        await crossref.aclose()
        await cache.close()


mcp = FastMCP("citation-mcp", lifespan=_app_lifespan)


# ---------------------------------------------------------------------------
# Core verifyCitation logic (separated for testability)
# ---------------------------------------------------------------------------


def _empty_canonical_from_input(input_citation: dict) -> dict:
    return {
        "doi": input_citation.get("doi"),
        "pmid": input_citation.get("pmid"),
        "title": input_citation.get("title"),
        "authors": [{"family": a, "given": ""} if isinstance(a, str) else a
                    for a in (input_citation.get("authors") or [])] or None,
        "year": input_citation.get("year"),
        "journal": input_citation.get("journal"),
        "volume": input_citation.get("volume"),
        "issue": input_citation.get("issue"),
        "pages": input_citation.get("pages"),
        "abstract": None,
    }


def _canonical_from_record(record: dict) -> dict:
    return {
        "doi": record.get("doi"),
        "pmid": record.get("pmid"),
        "title": record.get("title"),
        "authors": record.get("authors") or None,
        "year": record.get("year"),
        "journal": record.get("journal"),
        "volume": record.get("volume"),
        "issue": record.get("issue"),
        "pages": record.get("pages"),
        "abstract": None,
    }


def _no_match_response(input_citation: dict, databases_failed: list[str], warnings: list[dict]) -> dict:
    return {
        "verified": False,
        "confidence": 0.0,
        "match_quality": "none",
        "databases_confirmed": [],
        "databases_queried": ["crossref"],
        "databases_failed": databases_failed,
        "canonical": _empty_canonical_from_input(input_citation),
        "discrepancies": [],
        "requires_review": False,
        "warnings": warnings,
        "score_breakdown": None,
    }


async def verify_citation(
    input_citation: dict,
    crossref: CrossrefClient,
    cache: Cache,
) -> dict:
    """Run the verifyCitation flow. Returns the response dict (see spec)."""
    # --- Input validation: at least one of (doi, pmid, title) ---
    if not (input_citation.get("doi") or input_citation.get("pmid") or input_citation.get("title")):
        return {
            "error": "at_least_one_of_doi_pmid_title_required",
            "message": "verifyCitation requires at least one of: doi, pmid, title.",
        }

    # --- Cache lookup ---
    cache_key = make_cache_key(input_citation)
    cached = await cache.get(cache_key)
    if cached is not None:
        warnings = list(cached.get("warnings") or [])
        warnings.append({"source": "cache"})
        cached["warnings"] = warnings
        return cached

    warnings: list[dict] = []

    # --- DOI-direct path ---
    doi = input_citation.get("doi")
    record: dict | None = None
    candidates: list[dict] = []
    try:
        if doi:
            record = await crossref.search_by_doi(doi)
            if record is not None:
                candidates = [record]
        else:
            title = input_citation.get("title") or ""
            authors = input_citation.get("authors") or []
            first_author = ""
            if authors:
                first_author = normalize_author_surname(
                    authors[0] if isinstance(authors[0], str) else (
                        f"{authors[0].get('family','')}, {authors[0].get('given','')}"
                    )
                )
            year = input_citation.get("year")
            journal = input_citation.get("journal")
            candidates = await crossref.search_by_metadata(
                title=title,
                author=first_author or None,
                year=year,
                journal=journal,
                rows=5,
            )
    except (httpx.HTTPError, RuntimeError) as e:
        logger.exception("Crossref request failed")
        warnings.append({
            "source": "crossref",
            "level": "error",
            "message": f"Crossref request failed: {type(e).__name__}: {e}",
        })
        return _no_match_response(input_citation, databases_failed=["crossref"], warnings=warnings)

    if not candidates:
        result = _no_match_response(input_citation, databases_failed=[], warnings=warnings)
        await cache.set(cache_key, result, type_="verify_citation_result")
        return result

    # --- Score candidates, pick best ---
    best: tuple[dict, dict] | None = None  # (score_result, candidate_record)
    for cand in candidates:
        # Build the input citation as seen by the scorer (authors as strings is fine —
        # scoring handles both str and dict).
        scoring_input = dict(input_citation)
        scored = score_match(scoring_input, cand)
        if best is None or scored["confidence"] > best[0]["confidence"]:
            best = (scored, cand)

    assert best is not None
    scored, matched_record = best

    if scored["confidence"] <= 0.0 or scored["match_quality"] == "none":
        result = _no_match_response(input_citation, databases_failed=[], warnings=warnings)
        # Still report score breakdown of best candidate for transparency.
        result["score_breakdown"] = scored.get("score_breakdown")
        await cache.set(cache_key, result, type_="verify_citation_result")
        return result

    result = {
        "verified": scored["match_quality"] in ("high", "medium", "low"),
        "confidence": scored["confidence"],
        "match_quality": scored["match_quality"],
        "databases_confirmed": ["crossref"],
        "databases_queried": ["crossref"],
        "databases_failed": [],
        "canonical": _canonical_from_record(matched_record),
        "discrepancies": [],
        "requires_review": scored.get("requires_review", False),
        "warnings": warnings,
        "score_breakdown": scored.get("score_breakdown"),
    }
    await cache.set(cache_key, result, type_="verify_citation_result")
    return result


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


@mcp.tool(
    name="verifyCitation",
    description=(
        "Verify a citation against the Crossref database. Provide at least one of "
        "doi, pmid, or title. Returns a structured verification result including "
        "match quality, confidence score, canonical record, and score breakdown."
    ),
)
async def verify_citation_tool(
    ctx: Context,
    doi: str | None = None,
    pmid: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    journal: str | None = None,
    volume: str | None = None,
    issue: str | None = None,
    pages: str | None = None,
) -> str:
    """Verify a citation. Returns JSON-serialized result dict."""
    input_citation = {
        "doi": doi,
        "pmid": pmid,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
    }
    # Drop None values for cleaner downstream handling.
    input_citation = {k: v for k, v in input_citation.items() if v is not None}

    app_ctx: AppContext = ctx.request_context.lifespan_context
    result = await verify_citation(input_citation, app_ctx.crossref, app_ctx.cache)
    return json.dumps(result, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the stdio MCP server."""
    log_level = os.environ.get("CITATION_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
