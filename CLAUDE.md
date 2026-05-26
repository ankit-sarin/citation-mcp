# citation-mcp — Architectural Notes

## Project purpose

citation-mcp is a self-hosted MCP server providing citation tools that gap-fill
against Anthropic's hosted PubMed connector and `web_fetch`. It is the Python
implementation on the DGX Spark, eventually exposed via Cloudflare Tunnel to
`claude.ai` as a custom connector. The repo is implementer-side only —
architectural decisions are made in the planning environment (`claude.ai`)
and arrive here as task specs.

## Architecture state

**Phase 1.B + 1.B.1 + 1.D.0 + 1.D + 1.D.1 — complete.**

- Dual transport: **stdio** (default) and **Streamable HTTP** (`--transport http`),
  both backed by the official `mcp` Python SDK
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

**Production deployment (Phase 1.D.1).**

- systemd unit at `/etc/systemd/system/citation-mcp.service`, env in drop-in
  `.service.d/override.conf` (640 root:root) — secrets never inline in the
  main unit. Process runs as `ankitsarin`, bound to `127.0.0.1:8080`.
- Fronted by Cloudflare Tunnel + Cloudflare Access at
  `https://citation-mcp.digitalsurgeon.dev`. Access OTP gates
  `/oauth/authorize` only; all other paths bypass.
- Hardening: `NoNewPrivileges`, `ProtectSystem=strict`,
  `ReadWritePaths=data logs`, `ProtectHome=read-only`, `PrivateTmp=true`.
- Restart policy: `on-failure`, `RestartSec=5s` — deliberately fail-fast
  on config errors (e.g. malformed `OAUTH_SIGNING_KEY`) so misconfiguration
  surfaces in `systemctl status` rather than masquerading as a running
  service.

**Future phases (not yet built):**

- **1.C** — additional citation-integrity tools per the phasing schedule
  maintained in `claude.ai`
- **1.D.4** — register the server with `claude.ai` as a custom connector
  and verify the end-to-end OAuth flow from a connected conversation

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
| `OAUTH_SIGNING_KEY` | (none) | HS256 signing key (hex, ≥32 bytes) — **required** in HTTP mode | HTTP server refuses to start |
| `OAUTH_ISSUER` | `https://citation-mcp.digitalsurgeon.dev` | OAuth 2.1 issuer URL surfaced in AS metadata | falls back to issuer default |
| `OAUTH_AUDIENCE` | same as issuer | JWT `aud` claim | n/a |
| `OAUTH_DB_PATH` | `~/projects/citation-mcp/data/oauth.db` | aiosqlite store for clients, codes, refresh tokens | n/a |
| `OAUTH_ALLOW_MISSING_CF_EMAIL` | `false` | Dev escape hatch — bypass `Cf-Access-Authenticated-User-Email` requirement | n/a |
| `MCP_ORIGIN_ALLOWLIST` | `https://claude.ai` | CSV of allowed Origins for `POST /mcp` (CVE-2026-33252) | n/a |
| `MCP_HOST_ALLOWLIST` | `citation-mcp.digitalsurgeon.dev,localhost,127.0.0.1` | CSV of allowed Host headers (CVE-2026-35568) | n/a |

## How to run locally

```bash
uv sync
uv run mcp dev src/citation_mcp/server.py        # mcp-inspector (stdio)
uv run citation-mcp                              # stdio server
OAUTH_SIGNING_KEY=$(openssl rand -hex 32) \
  uv run citation-mcp --transport http --port 8090   # HTTP server (local dev)
```

For the deployed service, use `systemctl {status,restart,stop} citation-mcp`
on the DGX; logs via `journalctl -u citation-mcp -f`.

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
only; architecture decisions live elsewhere. The systemd unit and Cloudflare
Tunnel/Access config are similarly out-of-band — edit those only against an
explicit deployment spec, not as part of a code change.

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

## Phase 1.D / 1.D.0 / 1.D.1 refinements (vs. 1.B.1)

- **Streamable HTTP transport** alongside stdio. Outer Starlette app holds
  our OAuth routes + CVE middlewares; SDK's `streamable_http_app()` is
  mounted at `/` so our routes match first. Outer lifespan chains into
  `sdk_app.router.lifespan_context` so the SDK's session manager actually
  runs (Starlette doesn't propagate lifespan into mounts).
- **Hand-rolled OAuth 2.1 AS** (`/oauth/register`, `/oauth/authorize`,
  `/oauth/token`) per RFC 6749 / 7591 / 7636. Public clients only
  (`token_endpoint_auth_method=none`). Returns `401 invalid_client` (not
  the SDK's `400 unauthorized_client`) for unknown clients per RFC 6749
  §5.2. DCR is open by design — Cloudflare Access at `/oauth/authorize`
  is the gate, not registration.
- **Identity bridge** via `Cf-Access-Authenticated-User-Email` header.
  The accompanying `Cf-Access-Jwt-Assertion` is *not* verified against
  Cloudflare's JWKS — this is intentional (the Tunnel is the trust
  boundary). Set `OAUTH_ALLOW_MISSING_CF_EMAIL=true` only for local dev.
- **CVE defenses** in three middlewares (outer→inner): `HostHeader`
  (CVE-2026-35568, 421 on bad Host), `McpOrigin` (CVE-2026-33252, 403 on
  cross-site `POST /mcp`), `McpContentType` (CVE-2026-33252, 415 on
  non-JSON `POST /mcp`).
- **Refresh-token chain rotation** with reuse detection — rotated tokens
  inherit `chain_id`; presenting a previously-rotated token revokes the
  whole chain. All tokens stored as sha256 hex; plaintext leaves the
  server only at issuance.
- **Token-endpoint error semantics aligned with RFC 6749 §5.2.** PyJWT
  decode uses *explicit* `issuer=` and `audience=` kwargs (the SDK's
  default path lacks both — upstream issues #1443 / #1445).
- **httpx log + exception redaction** (1.D.0): the URL-query-param
  filter scrubs `api_key=` and seven other sensitive names; every
  `raise_for_status()` site is wrapped by `reraise_redacted()` so URLs
  in propagated exception strings are also scrubbed.
- **Production deployment** (1.D.1): see "Production deployment" block
  in the architecture-state section above. v0.3.1 is deployment-only;
  no code changes from v0.3.0.

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
- **~~API keys appear in httpx URL logs~~** — mitigated in 1.D.0 via the
  query-param redaction filter that scrubs `api_key=` and seven other
  sensitive names to `***` in `httpx` INFO logs. Plus 1.D's
  `reraise_redacted(httpx.HTTPStatusError)` wrap at every
  `raise_for_status()` site so URLs in exception strings are also scrubbed.
