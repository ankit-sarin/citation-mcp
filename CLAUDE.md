# citation-mcp — Architectural Notes

## Project purpose

citation-mcp is a self-hosted MCP server providing citation tools that gap-fill
against Anthropic's hosted PubMed connector and `web_fetch`. It is the Python
implementation on the DGX Spark, eventually exposed via Cloudflare Tunnel to
`claude.ai` as a custom connector. The repo is implementer-side only —
architectural decisions are made in the planning environment (`claude.ai`)
and arrive here as task specs.

## Architecture state

**Phase 1.B + 1.B.1 — complete.**

- stdio MCP transport via the official `mcp` Python SDK
- Three tools:
  - `verifyCitation` — fans out across all configured databases in parallel,
    applies per-DB candidate fall-through, then runs Layer 3 canonical merge
    with inter-DB discrepancy detection.
  - `bulkVerifyCitations` — batch verification with citation-level
    concurrency cap (10) and a 200-citation request cap.
  - `resolveIdentifier` — cross-converts DOI ↔ PMID ↔ arXiv ID ↔ OpenAlex
    Work ID ↔ Semantic Scholar paper ID, with 14-day caching.
- Five database backends, each gracefully degrading when its API key is unset:
  - **Crossref** — polite-pool, single-source canonical for DOI / title / authors / journal
  - **PubMed** — NCBI E-utilities (esearch + efetch), 10 req/sec with key (3 without)
  - **OpenAlex** — `api_key=` query param (no mailto; deprecated Feb 2026)
  - **Semantic Scholar** — `x-api-key` header, 20 req/sec authenticated
  - **arXiv** — Atom API, 3-second min-spacing lock, opt-in (queried only
    when input has explicit `arxiv_id`, or `resolveIdentifier` is called with
    `from_type='arxiv'`)
- SQLite cache (aiosqlite) with differential TTL: 14 days for confirmed matches,
  24 hours for no-match outcomes, 14 days for identifier-resolution results
- Three-layer match-quality scoring:
  - Layer 1 — identifier-decisive (DOI / PMID / arXiv / OpenAlex / Semantic Scholar)
  - Layer 2 — weighted-field score (title 0.40, first-author 0.20, year 0.20,
    journal 0.10, other-authors 0.10), adaptive weight redistribution over
    supplied input fields, short-title weight adjustment, three sanity guards,
    and a "title-only cap" that limits match_quality to "medium" when fewer
    than three input fields are populated.
  - Layer 3 — `merge_canonical_records()` with per-field authority order across
    DBs, special-case earliest-year and dual-source citation counts.

**Future phases (not yet built):**

- **1.C** — additional citation-integrity tools per the phasing schedule
  maintained in `claude.ai`
- **1.D** — HTTP/SSE transport + OAuth + DCR + Cloudflare Tunnel + Cloudflare
  Access (Google SSO) so the server can register as a `claude.ai` custom
  connector

## Database query policy

- **arXiv is opt-in.** For `verifyCitation`, arXiv is queried only when the
  input has an explicit `arxiv_id`. For DOI / PMID / title inputs, arXiv's
  coverage of biomedical work is near-zero and the rate-limit cost (3-second
  min spacing plus frequent 429s) is not worth the negligible hit rate.
  Similarly, `resolveIdentifier` queries arXiv only when `from_type='arxiv'`;
  for other `from_type` values, arXiv IDs are cross-referenced via Semantic
  Scholar's `externalIds` field.
- **arXiv 429s are soft warnings, not failures.** When arXiv exhausts its
  retries on 429, the client raises `ArxivRateLimited` and the tool result
  surfaces a `{"source": "arxiv", "level": "warning", "message": "arxiv
  rate-limited; result may be incomplete"}` entry. arXiv does NOT appear in
  `databases_failed` in this case — the other DBs typically cover the work
  and partial-but-correct is a better representation than "DB failed".

## Database configuration

| Variable | Default | Purpose | If unset |
|---|---|---|---|
| `CROSSREF_POLITE_EMAIL` | `asarin@ucdavis.edu` | Polite-pool mailto for Crossref + NCBI User-Agent | Crossref runs at default rate |
| `NCBI_API_KEY` | (none) | NCBI E-utilities key for PubMed | PubMed runs at 3 req/sec instead of 10 |
| `OPENALEX_API_KEY` | (none) | OpenAlex API key | OpenAlex client disabled; warning logged |
| `SEMANTIC_SCHOLAR_API_KEY` | (none) | Semantic Scholar Graph API key | Semantic Scholar runs at 1 req/sec |
| `CACHE_DB_PATH` | `~/projects/citation-mcp/data/cache.db` | SQLite cache location | n/a |
| `CITATION_MCP_LOG_LEVEL` | `INFO` | Python logging level | n/a |

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

Live integration tests are gated behind `CITATION_MCP_LIVE=1`:

```bash
CITATION_MCP_LIVE=1 uv run pytest -v -k integration
```

## Speed targets

- `bulkVerifyCitations` cold cache, 30 typical citations: <15 seconds.
- Same call, warm cache: <2 seconds.

## Architectural convention

Do not add new tools, transports, or database backends without an updated task
spec from the planning environment (`claude.ai`). This repo is implementer-side
only; architecture decisions live elsewhere. Likewise, do not introduce auth,
HTTP transport, or systemd/Cloudflare changes within Phase 1.B / 1.B.1 scope —
those land in Phase 1.D.

## Phase 1.B refinements completed (vs. Phase 1.A)

- **Title-only inputs are now capped at "medium" match quality** when fewer
  than three populated fields among {title, first-author, year, journal} are
  supplied. The cap surfaces as a `capped_at_medium_insufficient_input_fields`
  warning on the result.
- **Candidate fall-through.** When the top-scoring candidate from any DB fails
  a sanity guard, the next-highest-scoring candidate is tried. Applied
  uniformly across Crossref, PubMed, OpenAlex, and Semantic Scholar
  metadata-search paths (and arXiv when its opt-in condition is met).
- **Differential cache TTL.** Confirmed matches cache for 14 days; no-match
  outcomes cache for 24 hours so newly-indexed papers re-query promptly.
- **`rejected_by` consolidated.** Now lives only in `score_breakdown.rejected_by`;
  the top-level duplicate has been removed.

## Phase 1.B.1 refinements (vs. Phase 1.B)

- **arXiv opt-in.** Phase 1.B queried arXiv on every input regardless of
  type; cold-cache bulk-10 took ~6 minutes due to arXiv's 3-second spacing
  lock and frequent 429s. 1.B.1 makes arXiv opt-in (see "Database query
  policy" above), cutting cold-cache single-citation from "unbounded" to
  ~0.5 s and cold-cache bulk-10 from ~6 min to ~3 s.
- **arXiv 429 soft-warning path.** Exhausted arXiv retries (2 attempts, 5 s
  base backoff) raise `ArxivRateLimited`, which surfaces as a warning on
  the result rather than placing arXiv in `databases_failed`.
- **OpenAlex `host_venue` fully removed.** OpenAlex deprecated the field in
  late 2025. The client reads journal only from
  `primary_location.source.display_name`.
- **`citation_count` shape verified consistent** (always dict-or-null
  across all paths; never a bare int).

## Phase 1.B / 1.B.1 known limitations

- **Single-string author input + non-Western name order.** `parse_author_string`
  still assumes the last whitespace-separated token is the surname. Structured
  family/given fields are used preferentially when available.
- **arXiv Atom XML edge cases.** Certain malformed `<entry>` elements (e.g.
  partial submissions) may parse with empty fields. The client filters out
  obviously broken entries but does not log them.
- **PubMed batched efetch for bulkVerifyCitations.** Currently each citation
  in a bulk call issues its own esearch + efetch sequence. For DOI-keyed
  bulk, a single efetch with comma-joined PMIDs would cut PubMed traffic by
  ~50%. Deferred to Phase 1.B.2 if profiling shows it matters at scale.
- **arXiv as a dedicated `searchPreprints` tool (Phase 1.C).** The current
  opt-in policy means arXiv is invisible for users searching by title for a
  potentially-unpublished preprint. A separate tool would make preprint
  coverage explicit.
- **Cross-DB 429 soft-warning inconsistency.** arXiv 429s are soft warnings;
  PubMed and OpenAlex 429s still enter `databases_failed`. Revisit if real
  workloads start hitting authenticated-tier 429s on the others.
- **API keys appear in httpx URL logs at DEBUG/INFO.** OpenAlex passes its
  key as `?api_key=...`, NCBI as `?api_key=...`; both are visible in the
  default httpx log. Rotate keys before exposing the server publicly and
  add httpx log redaction in Phase 1.D before production deploy.
