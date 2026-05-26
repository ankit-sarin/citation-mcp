"""MCP server (stdio) for citation-mcp.

Phase 1.B tools:
  - verifyCitation        — multi-DB verification with Layer 3 canonical merge
  - bulkVerifyCitations   — batch wrapper around verifyCitation
  - resolveIdentifier     — cross-DB identifier mapping
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from mcp.server.fastmcp import Context, FastMCP

from .cache import Cache, make_cache_key
from .databases.arxiv import ArxivClient, ArxivRateLimited
from .databases.crossref import CrossrefClient
from .databases.openalex import OpenAlexClient, _extract_openalex_id
from .databases.pubmed import PubMedClient
from .databases.semantic_scholar import SemanticScholarClient
from .scoring import (
    merge_canonical_records,
    normalize_author_surname,
    normalize_doi,
    score_match,
)

logger = logging.getLogger("citation_mcp")

# 14 days for confirmed matches, 24 hours for no-match outcomes (Phase 1.B refinement).
_TTL_MATCH = 14 * 24 * 60 * 60
_TTL_NO_MATCH = 24 * 60 * 60
_TTL_RESOLVE = 14 * 24 * 60 * 60

_ALL_DB_SOURCES = ("crossref", "pubmed", "openalex", "semantic_scholar", "arxiv")
_BULK_MAX_CITATIONS = 200
_BULK_CONCURRENCY = 10


# ---------------------------------------------------------------------------
# Shared dependencies (instantiated in the server lifespan)
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    crossref: CrossrefClient
    pubmed: PubMedClient
    openalex: OpenAlexClient
    semantic_scholar: SemanticScholarClient
    arxiv: ArxivClient
    cache: Cache


@asynccontextmanager
async def _app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    crossref = CrossrefClient()
    pubmed = PubMedClient()
    openalex = OpenAlexClient()
    semantic_scholar = SemanticScholarClient()
    arxiv = ArxivClient()
    cache = Cache()
    await cache.init()
    try:
        yield AppContext(
            crossref=crossref,
            pubmed=pubmed,
            openalex=openalex,
            semantic_scholar=semantic_scholar,
            arxiv=arxiv,
            cache=cache,
        )
    finally:
        await asyncio.gather(
            crossref.aclose(),
            pubmed.aclose(),
            openalex.aclose(),
            semantic_scholar.aclose(),
            arxiv.aclose(),
            return_exceptions=True,
        )
        await cache.close()


def build_lifespan() -> Callable[[FastMCP], AsyncIterator[AppContext]]:
    """Return the shared lifespan factory so HTTP and stdio use one definition."""
    return _app_lifespan


# TODO(v1.1): consolidate per-tool entry logging via a wrapper adapter when tool count grows past 5–6.
def register_tools(target: FastMCP) -> None:
    """Register the three Phase 1.B tools on the given FastMCP instance.

    Called for the module-global stdio mcp below; called again from the HTTP
    transport on its own FastMCP wired with token_verifier + auth.
    """
    target.tool(
        name="verifyCitation",
        description=(
            "Verify a citation against five databases (Crossref, PubMed, OpenAlex, "
            "Semantic Scholar, arXiv) in parallel. Provide at least one of doi, pmid, "
            "or title. Returns match quality, canonical record, inter-DB discrepancies, "
            "and per-DB confirmation status."
        ),
    )(verify_citation_tool)
    target.tool(
        name="bulkVerifyCitations",
        description=(
            "Verify up to 200 citations in parallel against all configured databases. "
            "Returns a list of per-citation results in input order plus a summary "
            "(counts by match quality, cache hits, elapsed seconds)."
        ),
    )(bulk_verify_citations_tool)
    target.tool(
        name="resolveIdentifier",
        description=(
            "Cross-convert paper identifiers across DOI, PMID, arXiv ID, OpenAlex Work ID, "
            "and Semantic Scholar paper ID. Specify the source identifier and its type; "
            "returns the corresponding identifiers in each requested target system."
        ),
    )(resolve_identifier_tool)


mcp = FastMCP("citation-mcp", lifespan=_app_lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_canonical_from_input(input_citation: dict) -> dict:
    return {
        "doi": input_citation.get("doi"),
        "pmid": input_citation.get("pmid"),
        "arxiv_id": input_citation.get("arxiv_id"),
        "openalex_id": None,
        "paper_id": None,
        "title": input_citation.get("title"),
        "authors": [
            {"family": a, "given": ""} if isinstance(a, str) else a
            for a in (input_citation.get("authors") or [])
        ] or None,
        "year": input_citation.get("year"),
        "journal": input_citation.get("journal"),
        "journal_iso_abbrev": None,
        "volume": input_citation.get("volume"),
        "issue": input_citation.get("issue"),
        "pages": input_citation.get("pages"),
        "abstract": None,
        "citation_count": None,
        "type": None,
        "sources": [],
    }


def _first_author_str(input_citation: dict) -> str | None:
    authors = input_citation.get("authors") or []
    if not authors:
        return None
    a0 = authors[0]
    if isinstance(a0, str):
        return normalize_author_surname(a0) or None
    if isinstance(a0, dict):
        s = f"{a0.get('family','')}, {a0.get('given','')}"
        return normalize_author_surname(s) or None
    return None


# ---------------------------------------------------------------------------
# Per-DB fetch + candidate fall-through
# ---------------------------------------------------------------------------


async def _safe_call(coro: Awaitable[Any]) -> tuple[Any | None, Exception | None]:
    try:
        return await coro, None
    except Exception as e:  # noqa: BLE001 — boundary catch for one DB
        return None, e


def _select_best_candidate(
    input_citation: dict,
    candidates: list[dict],
) -> dict | None:
    """Phase 1.B candidate fall-through: walk by score, pick first that passes guards.

    Returns a dict with either passed=True (a record matched) or passed=False
    plus the most-informative rejected score, so callers can surface the
    rejection reason in the no-match response.
    """
    if not candidates:
        return None
    scored: list[tuple[dict, dict]] = []
    for cand in candidates:
        result = score_match(input_citation, cand)
        scored.append((result, cand))
    scored.sort(key=lambda x: x[0].get("confidence", 0.0), reverse=True)
    for result, cand in scored:
        if result.get("match_quality") != "none":
            return {"passed": True, "score": result, "record": cand}
    # All rejected — return the top-scoring rejected entry for context.
    best = scored[0] if scored else None
    if best is None:
        return None
    return {"passed": False, "score": best[0], "record": best[1]}


async def _fetch_from_db(
    db_name: str,
    client: Any,
    input_citation: dict,
    first_author: str | None,
) -> tuple[str, str, list[dict] | None, Exception | None]:
    """Return (db_name, strategy, candidates, error). strategy ∈ {doi, pmid, arxiv_id, metadata}."""
    doi = input_citation.get("doi")
    pmid = input_citation.get("pmid")
    arxiv_id = input_citation.get("arxiv_id")
    title = input_citation.get("title")
    year = input_citation.get("year")
    journal = input_citation.get("journal")

    try:
        if doi:
            if hasattr(client, "search_by_doi"):
                rec = await client.search_by_doi(doi)
                return db_name, "doi", ([rec] if rec else []), None
            return db_name, "doi", [], None
        if pmid:
            if db_name == "pubmed":
                rec = await client.search_by_pmid(pmid)
                return db_name, "pmid", ([rec] if rec else []), None
            if db_name == "semantic_scholar":
                rec = await client.search_by_pmid(pmid)
                return db_name, "pmid", ([rec] if rec else []), None
            # Fall through to metadata search for DBs without PMID lookup.
        if arxiv_id:
            if db_name == "arxiv":
                rec = await client.search_by_arxiv_id(arxiv_id)
                return db_name, "arxiv_id", ([rec] if rec else []), None
        # Metadata fall-back path.
        if not title:
            return db_name, "metadata", [], None
        if db_name == "semantic_scholar":
            cands = await client.search_by_metadata(
                title=title,
                author=first_author,
                year=year,
                rows=5,
            )
        elif db_name == "arxiv":
            cands = await client.search_by_metadata(
                title=title,
                author=first_author,
                year=year,
                rows=5,
            )
        else:
            cands = await client.search_by_metadata(
                title=title,
                author=first_author,
                year=year,
                journal=journal,
                rows=5,
            )
        return db_name, "metadata", cands or [], None
    except ArxivRateLimited as e:
        # Soft signal for the caller — arXiv being rate-limited is routine and
        # not a "DB failed" condition.
        return db_name, "rate_limited", None, e
    except (httpx.HTTPError, RuntimeError) as e:
        return db_name, "error", None, e


# ---------------------------------------------------------------------------
# verifyCitation core
# ---------------------------------------------------------------------------


async def verify_citation(
    input_citation: dict,
    crossref: CrossrefClient,
    cache: Cache,
    pubmed: PubMedClient | None = None,
    openalex: OpenAlexClient | None = None,
    semantic_scholar: SemanticScholarClient | None = None,
    arxiv: ArxivClient | None = None,
) -> dict:
    """Phase 1.B verifyCitation: dispatch to all configured DBs in parallel,
    apply candidate fall-through per DB, then merge with Layer 3.
    """
    # Input validation
    if not (input_citation.get("doi") or input_citation.get("pmid") or input_citation.get("title")):
        return {
            "error": "at_least_one_of_doi_pmid_title_required",
            "message": "verifyCitation requires at least one of: doi, pmid, title.",
        }

    cache_key = make_cache_key(input_citation)
    cached = await cache.get(cache_key)
    if cached is not None:
        warnings = list(cached.get("warnings") or [])
        warnings.append({"source": "cache"})
        cached["warnings"] = warnings
        return cached

    first_author = _first_author_str(input_citation)
    warnings: list[dict] = []

    clients: dict[str, Any] = {"crossref": crossref}
    if pubmed is not None:
        clients["pubmed"] = pubmed
    if openalex is not None and getattr(openalex, "enabled", True):
        clients["openalex"] = openalex
    if semantic_scholar is not None:
        clients["semantic_scholar"] = semantic_scholar
    # Phase 1.B.1: arXiv is opt-in. Only consult arXiv when the input carries
    # an explicit arxiv_id — for DOI/PMID/title inputs arXiv's coverage of
    # biomedical work is near-zero and the rate-limit cost is not worth it.
    if arxiv is not None and input_citation.get("arxiv_id"):
        clients["arxiv"] = arxiv

    # Fan out across configured DBs.
    tasks = [
        _fetch_from_db(name, client, input_citation, first_author)
        for name, client in clients.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    databases_queried: list[str] = []
    databases_failed: list[str] = []
    matched_records: list[dict] = []
    per_db_score: dict[str, dict] = {}
    rejected_scores: list[dict] = []

    for db_name, strategy, candidates, error in results:
        databases_queried.append(db_name)
        if strategy == "rate_limited":
            # Soft warning — does NOT enter databases_failed.
            warnings.append({
                "source": db_name,
                "level": "warning",
                "message": f"{db_name} rate-limited; result may be incomplete",
            })
            continue
        if error is not None:
            databases_failed.append(db_name)
            warnings.append({
                "source": db_name,
                "level": "error",
                "message": f"{db_name} request failed: {type(error).__name__}: {error}",
            })
            continue
        if not candidates:
            continue
        best = _select_best_candidate(input_citation, candidates)
        if best is None:
            continue
        if best.get("passed"):
            matched_records.append(best["record"])
            per_db_score[db_name] = best["score"]
        else:
            rejected_scores.append(best["score"])

    if not matched_records:
        # No DB confirmed. Pick the most-informative score_breakdown from the
        # rejected pool (highest confidence; ties broken by presence of rejected_by).
        score_breakdown = None
        if rejected_scores:
            rejected_scores.sort(
                key=lambda s: s.get("confidence", 0.0),
                reverse=True,
            )
            score_breakdown = rejected_scores[0].get("score_breakdown")
        result = {
            "verified": False,
            "confidence": 0.0,
            "match_quality": "none",
            "databases_confirmed": [],
            "databases_queried": sorted(databases_queried),
            "databases_failed": sorted(databases_failed),
            "canonical": _empty_canonical_from_input(input_citation),
            "discrepancies": [],
            "requires_review": False,
            "warnings": warnings,
            "score_breakdown": score_breakdown,
        }
        await cache.set(cache_key, result, type_="verify_citation_result", ttl_seconds=_TTL_NO_MATCH)
        return result

    # Layer 3 merge.
    merge = merge_canonical_records(matched_records)
    canonical = merge["canonical"]
    discrepancies = merge["discrepancies"]

    # Choose a representative score from the matched DBs. Prefer the highest-confidence one.
    best_db = max(per_db_score.items(), key=lambda kv: kv[1].get("confidence", 0.0))
    best_score = best_db[1]

    # Aggregate cap flag: if any DB's scorer capped the result, surface a warning.
    capped = any(
        s.get("capped_at_medium_insufficient_input_fields", False)
        for s in per_db_score.values()
    )
    if capped and best_score.get("match_quality") == "medium":
        warnings.append({"source": "scorer", "message": "capped_at_medium_insufficient_input_fields"})

    confirmed_dbs = sorted(per_db_score.keys())

    result = {
        "verified": best_score.get("match_quality") in ("high", "medium", "low"),
        "confidence": best_score.get("confidence", 0.0),
        "match_quality": best_score.get("match_quality", "none"),
        "databases_confirmed": confirmed_dbs,
        "databases_queried": sorted(databases_queried),
        "databases_failed": sorted(databases_failed),
        "canonical": canonical,
        "discrepancies": discrepancies,
        "requires_review": best_score.get("requires_review", False),
        "warnings": warnings,
        "score_breakdown": best_score.get("score_breakdown"),
    }

    ttl = _TTL_MATCH if result["match_quality"] != "none" else _TTL_NO_MATCH
    await cache.set(cache_key, result, type_="verify_citation_result", ttl_seconds=ttl)
    return result


# ---------------------------------------------------------------------------
# bulkVerifyCitations
# ---------------------------------------------------------------------------


async def bulk_verify_citations(
    citations: list[dict],
    crossref: CrossrefClient,
    cache: Cache,
    pubmed: PubMedClient | None = None,
    openalex: OpenAlexClient | None = None,
    semantic_scholar: SemanticScholarClient | None = None,
    arxiv: ArxivClient | None = None,
) -> dict:
    if not isinstance(citations, list) or not citations:
        return {
            "error": "citations_must_be_non_empty_list",
            "message": "bulkVerifyCitations requires a non-empty `citations` array.",
        }
    if len(citations) > _BULK_MAX_CITATIONS:
        return {
            "error": "too_many_citations",
            "message": f"bulkVerifyCitations accepts at most {_BULK_MAX_CITATIONS} citations per call (got {len(citations)}).",
        }

    sem = asyncio.Semaphore(_BULK_CONCURRENCY)
    start = time.monotonic()

    async def _one(c: dict) -> dict:
        async with sem:
            return await verify_citation(
                c, crossref, cache,
                pubmed=pubmed,
                openalex=openalex,
                semantic_scholar=semantic_scholar,
                arxiv=arxiv,
            )

    results = await asyncio.gather(*[_one(c) for c in citations])
    elapsed = time.monotonic() - start

    by_quality = {"high": 0, "medium": 0, "low": 0, "none": 0}
    verified = 0
    with_warnings = 0
    cache_hits = 0
    for r in results:
        mq = r.get("match_quality", "none")
        if mq in by_quality:
            by_quality[mq] += 1
        if r.get("verified"):
            verified += 1
        ws = r.get("warnings") or []
        if ws:
            with_warnings += 1
        if any(w.get("source") == "cache" for w in ws):
            cache_hits += 1

    return {
        "results": results,
        "summary": {
            "total": len(citations),
            "verified": verified,
            "by_match_quality": by_quality,
            "with_warnings": with_warnings,
            "cache_hits": cache_hits,
            "elapsed_seconds": round(elapsed, 4),
        },
    }


# ---------------------------------------------------------------------------
# resolveIdentifier
# ---------------------------------------------------------------------------


_RESOLVE_TYPES = ("doi", "pmid", "arxiv", "openalex", "semantic_scholar")


def _resolve_cache_key(from_type: str, identifier: str) -> str:
    norm = identifier.strip()
    if from_type == "doi":
        norm = normalize_doi(norm)
    elif from_type == "pmid":
        norm = norm.lstrip("0") or "0"
    elif from_type == "openalex":
        norm = (_extract_openalex_id(norm) or norm).upper()
    else:
        norm = norm.lower()
    digest = hashlib.sha256(f"{from_type}|{norm}".encode("utf-8")).hexdigest()
    return f"resolve:{from_type}:{digest}"


def _ids_from_record(rec: dict | None) -> dict:
    if not rec:
        return {}
    return {
        "doi": rec.get("doi"),
        "pmid": rec.get("pmid"),
        "arxiv": rec.get("arxiv_id"),
        "openalex": rec.get("openalex_id"),
        "semantic_scholar": rec.get("paper_id"),
    }


def _merge_ids(base: dict, more: dict) -> None:
    for k, v in more.items():
        if v and not base.get(k):
            base[k] = v


async def resolve_identifier(
    identifier: str,
    from_type: str,
    to_types: list[str] | None,
    cache: Cache,
    crossref: CrossrefClient,
    pubmed: PubMedClient | None = None,
    openalex: OpenAlexClient | None = None,
    semantic_scholar: SemanticScholarClient | None = None,
    arxiv: ArxivClient | None = None,
) -> dict:
    if from_type not in _RESOLVE_TYPES:
        return {
            "error": "invalid_from_type",
            "message": f"from_type must be one of {list(_RESOLVE_TYPES)}.",
        }
    if not identifier or not isinstance(identifier, str):
        return {"error": "identifier_required", "message": "Provide a non-empty `identifier`."}
    if to_types is None:
        to_types = list(_RESOLVE_TYPES)
    for t in to_types:
        if t not in _RESOLVE_TYPES:
            return {"error": "invalid_to_type", "message": f"to_type '{t}' invalid."}

    cache_key = _resolve_cache_key(from_type, identifier)
    cached = await cache.get(cache_key)
    if cached is not None:
        cached_filtered = dict(cached)
        cached_filtered["resolved"] = {
            k: cached["resolved"].get(k) for k in to_types
        }
        return cached_filtered

    resolved: dict[str, str | None] = {k: None for k in _RESOLVE_TYPES}
    resolved[from_type] = identifier
    if from_type == "doi":
        resolved["doi"] = normalize_doi(identifier)

    databases_queried: list[str] = []
    databases_failed: list[str] = []

    warnings: list[dict] = []

    async def _query(db_name: str, coro: Awaitable[dict | None]) -> None:
        databases_queried.append(db_name)
        try:
            rec = await coro
        except ArxivRateLimited:
            warnings.append({
                "source": db_name,
                "level": "warning",
                "message": f"{db_name} rate-limited; result may be incomplete",
            })
            logger.warning("resolveIdentifier: %s rate-limited", db_name)
            return
        except (httpx.HTTPError, RuntimeError) as e:
            databases_failed.append(db_name)
            logger.warning("resolveIdentifier: %s failed: %s", db_name, e)
            return
        _merge_ids(resolved, _ids_from_record(rec))

    tasks: list[Awaitable[None]] = []
    # Phase 1.B.1: arXiv is queried for from_type='arxiv' only. For all other
    # from_types, arXiv ID cross-refs come from Semantic Scholar's externalIds.
    if from_type == "doi":
        tasks.append(_query("crossref", crossref.search_by_doi(identifier)))
        if pubmed is not None:
            tasks.append(_query("pubmed", pubmed.search_by_doi(identifier)))
        if openalex is not None and openalex.enabled:
            tasks.append(_query("openalex", openalex.search_by_doi(identifier)))
        if semantic_scholar is not None:
            tasks.append(_query("semantic_scholar", semantic_scholar.search_by_doi(identifier)))
    elif from_type == "pmid":
        if pubmed is not None:
            tasks.append(_query("pubmed", pubmed.search_by_pmid(identifier)))
        if semantic_scholar is not None:
            tasks.append(_query("semantic_scholar", semantic_scholar.search_by_pmid(identifier)))
    elif from_type == "arxiv":
        if arxiv is not None:
            tasks.append(_query("arxiv", arxiv.search_by_arxiv_id(identifier)))
    elif from_type == "openalex":
        if openalex is not None and openalex.enabled:
            tasks.append(_query("openalex", openalex.search_by_work_id(identifier)))
    elif from_type == "semantic_scholar":
        if semantic_scholar is not None:
            tasks.append(_query("semantic_scholar", semantic_scholar.search_by_paper_id(identifier)))

    await asyncio.gather(*tasks)

    # If from_type wasn't doi but we resolved a DOI mid-way, query OpenAlex by DOI to
    # fill the openalex_id, and PubMed by DOI to fill PMID. One extra round.
    if from_type != "doi" and resolved.get("doi"):
        followup: list[Awaitable[None]] = []
        if openalex is not None and openalex.enabled and not resolved.get("openalex"):
            followup.append(_query("openalex", openalex.search_by_doi(resolved["doi"])))
        if pubmed is not None and not resolved.get("pmid"):
            followup.append(_query("pubmed", pubmed.search_by_doi(resolved["doi"])))
        if semantic_scholar is not None and not resolved.get("semantic_scholar"):
            followup.append(_query("semantic_scholar", semantic_scholar.search_by_doi(resolved["doi"])))
        if followup:
            await asyncio.gather(*followup)

    result = {
        "identifier": identifier,
        "from_type": from_type,
        "resolved": {k: resolved.get(k) for k in _RESOLVE_TYPES},
        "databases_queried": sorted(set(databases_queried)),
        "databases_failed": sorted(set(databases_failed)),
        "warnings": warnings,
    }
    await cache.set(cache_key, result, type_="resolve_identifier_result", ttl_seconds=_TTL_RESOLVE)
    result["resolved"] = {k: result["resolved"].get(k) for k in to_types}
    return result


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


async def verify_citation_tool(
    ctx: Context,
    doi: str | None = None,
    pmid: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    journal: str | None = None,
    volume: str | None = None,
    issue: str | None = None,
    pages: str | None = None,
) -> str:
    logger.info("tool_call name=%s", "verifyCitation")
    input_citation = {
        "doi": doi,
        "pmid": pmid,
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
    }
    input_citation = {k: v for k, v in input_citation.items() if v is not None}

    app_ctx: AppContext = ctx.request_context.lifespan_context
    result = await verify_citation(
        input_citation,
        app_ctx.crossref,
        app_ctx.cache,
        pubmed=app_ctx.pubmed,
        openalex=app_ctx.openalex,
        semantic_scholar=app_ctx.semantic_scholar,
        arxiv=app_ctx.arxiv,
    )
    return json.dumps(result, ensure_ascii=False, default=str)


async def bulk_verify_citations_tool(
    ctx: Context,
    citations: list[dict],
) -> str:
    logger.info("tool_call name=%s", "bulkVerifyCitations")
    app_ctx: AppContext = ctx.request_context.lifespan_context
    result = await bulk_verify_citations(
        citations,
        app_ctx.crossref,
        app_ctx.cache,
        pubmed=app_ctx.pubmed,
        openalex=app_ctx.openalex,
        semantic_scholar=app_ctx.semantic_scholar,
        arxiv=app_ctx.arxiv,
    )
    return json.dumps(result, ensure_ascii=False, default=str)


async def resolve_identifier_tool(
    ctx: Context,
    identifier: str,
    from_type: str,
    to_types: list[str] | None = None,
) -> str:
    logger.info("tool_call name=%s", "resolveIdentifier")
    app_ctx: AppContext = ctx.request_context.lifespan_context
    result = await resolve_identifier(
        identifier=identifier,
        from_type=from_type,
        to_types=to_types,
        cache=app_ctx.cache,
        crossref=app_ctx.crossref,
        pubmed=app_ctx.pubmed,
        openalex=app_ctx.openalex,
        semantic_scholar=app_ctx.semantic_scholar,
        arxiv=app_ctx.arxiv,
    )
    return json.dumps(result, ensure_ascii=False, default=str)


# Register the three tools on the stdio mcp instance. HTTP transport
# builds its own FastMCP and calls register_tools(target) on that.
register_tools(mcp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log_level = os.environ.get("CITATION_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
