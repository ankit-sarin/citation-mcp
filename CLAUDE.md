# citation-mcp — Architectural Notes

## Project purpose

citation-mcp is a self-hosted MCP server providing citation tools that gap-fill
against Anthropic's hosted PubMed connector and `web_fetch`. It is the Python
implementation on the DGX Spark, eventually exposed via Cloudflare Tunnel to
`claude.ai` as a custom connector. The repo is implementer-side only —
architectural decisions are made in the planning environment (`claude.ai`)
and arrive here as task specs.

## Architecture state

**Phase 1.A — complete.**

- stdio MCP transport via the official `mcp` Python SDK
- Single tool: `verifyCitation`
- Single database backend: Crossref (polite pool)
- SQLite cache (aiosqlite) with TTL, scaffolded for future result types
- Match-quality scoring rubric:
  - Layer 1 — identifier-decisive (DOI / PMID / arXiv / OpenAlex / Semantic Scholar)
  - Layer 2 — weighted-field score (title 0.40, first-author 0.20, year 0.20,
    journal 0.10, other-authors 0.10) with short-title weight adjustment and
    three hard sanity guards (year-off, first-author-mismatch + low title sim,
    title sim below floor)

**Future phases (not yet built):**

- **1.B+** — PubMed, OpenAlex, Semantic Scholar, arXiv clients;
  `bulkVerifyCitations`, `resolveIdentifier`
- **2** — HTTP/SSE transport + OAuth + DCR + Cloudflare Tunnel + Cloudflare
  Access (Google SSO) so the server can register as a `claude.ai` custom
  connector
- **3+** — remaining citation-integrity tools per the phasing schedule
  maintained in `claude.ai`

## How to run locally

```bash
uv sync
uv run mcp dev src/citation_mcp/server.py   # mcp-inspector in the browser
uv run citation-mcp                          # stdio server directly
```

## How to run tests

```bash
uv run pytest -v
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CROSSREF_POLITE_EMAIL` | `asarin@ucdavis.edu` | Contact email injected into the Crossref `User-Agent` for polite-pool access |
| `CACHE_DB_PATH` | `~/projects/citation-mcp/data/cache.db` | SQLite cache location (parent dir is created on first run) |
| `CITATION_MCP_LOG_LEVEL` | `INFO` | Python logging level for the server |

## Architectural convention

Do not add new tools, transports, or database backends without an updated task
spec from the planning environment (`claude.ai`). This repo is implementer-side
only; architecture decisions live elsewhere. Likewise, do not introduce auth,
HTTP transport, or systemd/Cloudflare changes within this Phase 1.A scope.

## Phase 1.A known limitations

The following are deliberate scope cuts, slated for Phase 1.B unless otherwise noted:

- **Title-only input can reach high confidence.** Layer 2 renormalizes weights over supplied fields, so a citation with only a title and a single candidate match can score 1.0. In v1.1 this is capped at `match_quality: "medium"` for input with fewer than three populated fields among {title, first-author, year, journal}.
- **No metadata-search candidate fall-through.** If the top-ranked Crossref candidate fails sanity guards, lower-ranked candidates are not retried. This will incorrectly reject the real paper when Crossref surfaces "Reply to" / "Erratum" entries with similar titles above it. Phase 1.B iterates the candidate list and picks the highest-scoring candidate that passes guards.
- **`rejected_by` is currently surfaced both at the top level of the Layer 2 internal result and inside `score_breakdown`.** Phase 1.B consolidates to `score_breakdown.rejected_by` only.
- **Uniform 14-day cache TTL.** No-match outcomes are cached for the same 14 days as confirmed matches. Phase 1.B introduces a 24-hour TTL for no-match outcomes so newly-indexed papers are re-queried promptly.
- **Single-string author input + non-Western name order.** `parse_author_string` assumes the last whitespace-separated token is the surname. `"Şahin Uğur"` parses incorrectly. Crossref's structured family/given fields are used preferentially when available, which mitigates this for verification flows but not for input parsing. v1.1 hardening item.
- **Single-database discrepancy reporting.** The `discrepancies` array is intentionally empty in Phase 1.A; it is reserved for inter-database conflict reporting once Phase 1.B adds PubMed/OpenAlex/Semantic Scholar/arXiv. Input-vs-canonical mismatches within Crossref data are not surfaced as discrepancies (they would mostly be user typos, not source disagreement).
