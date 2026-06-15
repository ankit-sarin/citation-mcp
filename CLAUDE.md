# citation-mcp — Architectural Notes

## Project purpose

citation-mcp is a self-hosted MCP server providing citation tools that gap-fill
against Anthropic's hosted PubMed connector and `web_fetch`. It is the Python
implementation on the DGX Spark, eventually exposed via Cloudflare Tunnel to
`claude.ai` as a custom connector. The repo is implementer-side only —
architectural decisions are made in the planning environment (`claude.ai`)
and arrive here as task specs.

## Architecture state

**Phase 1.B + 1.B.1 + 1.D.0 + 1.D + 1.D.1 + 1.D.2 + 1.D.4 — complete.**
**Phase 2.A → 2.F (regression-baseline fixture) — complete.**
**Phase v0.3.5 (parser rewrite + force_refresh) — complete.**

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
- **`OAUTH_AUDIENCE` MUST include a trailing slash** in HTTP mode:
  `https://citation-mcp.digitalsurgeon.dev/`. claude.ai's connector
  constructs the RFC 8707 `resource` parameter as `<server-base>/` and
  performs a *strict* client-side `aud == resource` string comparison.
  Without the slash, claude.ai rejects every access token before
  presenting it and the connector loops forever in
  refresh-grant → 401 → refresh-grant. `OAUTH_ISSUER` is *not* affected
  (it's never compared as a client-side resource).
- Connector is **live at `https://citation-mcp.digitalsurgeon.dev`** as a
  claude.ai custom connector. The OFID is registered to
  `dr.ankitsarin@gmail.com`'s account; first connect was 09:35:24 UTC,
  first successful `tools/call` was 09:41:25 UTC (cache hit, ~14 ms
  server-side).

**Future phases (not yet built):**

- **1.C** — additional citation-integrity tools per the phasing schedule
  maintained in `claude.ai`
- **1.D.2 backlog** — observability gaps surfaced during 1.D.4 bring-up:
  log verifier-failure reason (currently swallowed in `TokenVerifier`);
  log tool-name on `CallToolRequest`; orphan-DCR-client cleanup endpoint
- **Phase 3** — live-connector regression harness that exercises the
  30-row fixture (see "Regression baseline fixture" below) against
  `https://citation-mcp.digitalsurgeon.dev` via the OAuth-gated `/mcp`
  endpoint, not just the stdio transport used at fixture-capture time

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
| `OAUTH_AUDIENCE` | same as issuer | JWT `aud` claim — **must end in `/`** when fronting claude.ai (see "Production deployment") | n/a |
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

Current count: **345 passing + 2 skipped**. The passing set is
unit/integration + fixture-validation in `tests/test_regression_30_fixture.py`
+ the 7 deterministic `canonical.arxiv_id` authority/redundancy invariants in
`tests/test_arxiv_id_authority.py` + the Phase GATE-OPT1 mocked correctness
suite (`tests/test_gate_opt1_correctness.py`, T1–T6) + the Phase CMCP-GATE-0615
citation_count non-gating suite (`tests/test_gate_opt1_citation_count.py`, 6
tests) and offline-replay acceptance checks (`tests/harness/test_replay.py`,
skip when their gitignored sidecars are absent). The 2 skipped are live upstream-contract checks in
`tests/test_arxiv_id_authority_live.py` that run only under
`CITATION_MCP_LIVE=1` (and `pytest.skip` — never fail — when Semantic Scholar
is rate-limited/unavailable).

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

## Phase 1.D.2 / 1.D.4 refinements (vs. 1.D.1)

- **SDK transport_security host check disabled** (v0.3.2, `b9c50aa`).
  `FastMCP.__init__` auto-enables DNS rebinding protection with a
  localhost-only allowlist (`127.0.0.1:*`, `localhost:*`, `[::1]:*`)
  when its `host` arg defaults to `127.0.0.1` — which 421s any traffic
  forwarded from a reverse proxy with the public Host header. Our outer
  `HostHeaderMiddleware` already validates against `MCP_HOST_ALLOWLIST`,
  so the SDK's redundant copy is explicitly disabled by passing
  `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)`
  to `FastMCP()` in `http_app.py`.
- **Regression test** (`test_authenticated_mcp_with_public_host_is_not_421`)
  exercises this code path with a valid bearer + an allowlisted non-
  localhost Host. Crucial because the SDK's check runs *after* our auth
  middleware — so unauthenticated tests would never exercise it.
- **Trailing-slash audience requirement** documented in the Production
  deployment block above. Discovered during 1.D.4 bring-up by tracing a
  refresh-grant loop that the test suite couldn't reproduce — claude.ai's
  client-side aud-vs-resource check is the missing third party.
- **Refresh tokens survive `OAUTH_SIGNING_KEY` rotation.** Refresh tokens
  are opaque random strings stored as sha256 hex; the signing key only
  signs JWT *access* tokens. After rotation, the next `/oauth/token`
  refresh-grant succeeds normally and issues a new access token signed
  with the new key. Useful for incident response (rotate key without
  invalidating user sessions).
- **claude.ai connector flow verified end-to-end.** `/mcp` 200 from a
  real connected conversation at 09:35:24 UTC; `tools/call` cache-hit
  in ~14 ms server-side at 09:41:25 UTC. The connector pipeline
  exchanges through five distinct DCR clients during bring-up retries
  (no client-side reuse) — the orphan DCR-client cleanup is deferred to
  1.D.2.

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

## Regression baseline fixture (Phase 2.F)

- **Fixture lives at `tests/fixtures/regression_30.json`** — 30 verified
  baseline rows captured against `v0.3.3` stdio transport, stratified
  across nine categories: identifier-decisive (8), bibliographic-only
  (6), title-only (4), known-discrepancy (4), pubmed-native-edge (1),
  retracted (2), corrigendum (1), arxiv-opt-in (2), adversarial (2).
- **Three-tier expected values per row** (Phase 2.A schema):
  - `expected.hard` — structural assertions that must match exactly
    (`match_found`, `doi_resolved`, `pmid_resolved`, `arxiv_id_resolved`,
    `first_author_surname`, `year`, `match_quality`, `confidence` on
    matched rows; `match_found` + `rejected_by` on adversarial rows).
    **The hard gating set is per-row and data-driven: a field gates iff
    it is a key in that row's `expected.hard` block.** A volatile field is
    de-gated simply by removing its key from `hard` and leaving the
    baseline in `expected.snapshot` (where drift surfaces as a non-gating
    snapshot diff). See "Per-row hard-tier gate scoping" below for the
    rows where edition- or availability-volatile fields have been removed.
  - `expected.tolerant` — citation_count baseline value. **As of Phase
    CMCP-GATE-0615 the ENTIRE citation_count surface (value AND stub state:
    `stub_null` / `stub_zero` / `not_per_source_dict` / the opt-in 10×
    tripwire) is NON-gating.** The anomalies are still computed and emitted
    as informational telemetry; organic drift surfaces via the snapshot
    UNEXPECTED mechanism on `canonical.citation_count.*` paths. `tolerance_pct`
    is retained in the fixture for back-compat but no longer enforced. Plus
    required and forbidden discrepancy entries keyed on `(rule, field)`, and
    `allowed_soft_failures` (e.g. `["arxiv"]` for arXiv-opt-in rows) — both
    computed/reported but NON-gating (only discrepancy *structural*
    well-formedness gates). See "Phase CMCP-GATE-0615" below.
  - `expected.snapshot` — full raw response object for delta-style
    regression review when a future code change moves the baseline.
- **Loader at `tests/fixtures/loader.py`** — `load_regression_30()` reads
  the fixture and raises `FixtureValidationError` on any of 12
  structural rule violations (count, row_id sequence, category
  distribution, source provenance, adversarial constraints,
  non-adversarial completeness, arXiv-row shape, title-only quality,
  snapshot presence, allowed_soft_failures subset, discrepancy entry
  shape). Path is resolved relative to the loader file so pytest cwd
  doesn't matter.
- **`tests/test_regression_30_fixture.py`** runs the loader on every
  test run — fixture cannot drift structurally without the test
  failing fast.
- **Capture provenance**: `data/regression_30_baseline/` (gitignored)
  holds the raw stdio responses, the proto-fixture
  (`fixture_populated.json`), and the protocol-application report
  (`protocol_report.md`). Edits applied via `scripts/regression_30/`
  helper scripts (also gitignored). Architect-side decisions for the
  13 edits applied to the proto-fixture live in `claude.ai` Phase 2.E.3.
- **Adversarial row 030** ended up on "Path B" — the fallback title
  ("Comparison of robotic and laparoscopic colectomy outcomes") also
  cleared all guards and returned a medium-quality match with 9
  inter-DB discrepancies (Crossref and PubMed resolved to different
  physical papers). The row now exercises noisy-match-with-multiple-
  discrepancies regression coverage rather than the originally-intended
  guard-rejection path. Category retained as `adversarial`; row notes
  document the deviation.

  **v0.3.5 update:** row_030's match shifted from Araujo et al.
  (`10.1055/s-0044-1780788`) to Tukra et al. book chapter
  (`10.1007/978-3-030-58080-3_323-1`) between Phase 2.D baseline
  (May 26) and v0.3.5 regeneration (May 27). Cause is persistent
  Crossref index reshuffle, not run-to-run flakiness — back-to-back
  F.1 and F.1.b regen runs both selected Tukra. The row's
  `expected.tolerant.discrepancies_required` is and has always been
  empty `[]`; Phase 2.E deliberately abstained from discrepancy
  requirements on adversarial-noisy rows because asserting specific
  tuples on them would be brittle. Snapshot-tier drift on this row is
  acceptable by design.
- **Adversarial row 029**'s actual `rejected_by` is
  `title_similarity_below_floor`, not the originally-intended
  `year_off_by_more_than_one` — title-only inputs with null-year DB
  candidates skip the year guard and fall through to title-sim
  (see Phase 2.D Finding 2 in `data/regression_30_baseline/summary.md`).

### Per-row hard-tier gate scoping (edition / availability invariance)

Some baseline values are not connector-correctness properties but artifacts
of upstream edition reshuffles or transient single-DB availability. Gating on
them produces false failures, so those specific keys were removed from the
affected rows' `expected.hard` (baseline preserved in `expected.snapshot` so
the drift still surfaces non-gating):

- **row_030 (adversarial)** — gate scoped to edition-invariant fields only
  (`match_found`, `first_author_surname`, `pmid_resolved: null`,
  `arxiv_id_resolved: null`); `match_quality` reads from `expected.snapshot`
  (Crossref re-ranks the Tukra book-chapter editions). Commit `063ab14`.
- **row_016 / row_028 (`year`)** — both carry the earliest-year
  preprint/publication duality trap (preprint year vs later conference-
  proceedings DOI year; Layer 3 selects earliest). `year` de-gated from
  `hard`; the loader's **Rule 7** mandatory-non-null-year assertion reads
  `expected.snapshot.canonical.year` for the named set
  `YEAR_FROM_SNAPSHOT_ROWS = {"row_016", "row_028"}` instead. Commit `d4b766c`.
- **row_016 / row_017 / row_028 (`arxiv_id_resolved`)** — de-gated where the
  field is upstream-availability-dependent. row_016 was observed dropping to
  `None` on 2026-06-08 when Semantic Scholar fell; row_016/017 are title-only,
  so arXiv is not queried and **SS is the sole carrier** of `arxiv_id`. row_028
  has arXiv+SS redundancy but its baseline year/edition is volatile.
  **row_027 keeps `arxiv_id_resolved` gated** — it is echo-backed (explicit
  arxiv input surfaces via `_empty_canonical_from_input` even on total match
  loss) and never flips. Commit `d7b787e`. The authority/redundancy logic
  behind these decisions is locked in by `tests/test_arxiv_id_authority.py`
  (deterministic) + `tests/test_arxiv_id_authority_live.py` (opt-in live
  contract): `canonical.arxiv_id` is fed only by the `[arxiv,
  semantic_scholar]` authority list (scoring.py `_AUTHORITY`); arXiv is queried
  only on explicit `arxiv_id` input (server.py).

### Phase GATE-OPT1 — nightly gate reduced to connector-invariant correctness

Three consecutive nightly hard-gate FAILs (06-05 row_030 Crossref edition
re-rank; 06-08 row_016/028 earliest-year duality + arxiv_id carrier; 06-12
OpenAlex 13× ReadTimeout → row_010 confidence, row_013 sole-carrier
match_found collapse, row_014 carrier-displaced author/year, plus row_015
Crossref `.c1` correction re-rank) all occurred with `src/` byte-identical to
`bae3ac9`. Root cause: the hard gate asserted values/decisions that are a
function of *which DBs answered and how each currently indexes the work*.

**GATE-OPT1 removes every upstream-varying assertion from the live gate** and
moves correctness to deterministic mocked tests. This **supersedes the per-row
hard-tier gate scoping above** — that per-field, per-row de-gating is now
subsumed by a uniform rule: every matched row's `expected.hard` is **empty**.

The reduced hard-tier gate enforces ONLY:
- **Plumbing** — OAuth PKCE + Cloudflare Tunnel + Streamable-HTTP bulk path.
- ~~**Structural validity** — `canonical.citation_count` is a per-source dict,
  never a bare scalar (tolerant anomaly `not_per_source_dict`).~~
  **SUPERSEDED by Phase CMCP-GATE-0615** — the whole citation_count surface is
  now non-gating; this shape check moved to the mocked suite.
- ~~**citation_count stub guards** — `stub_null` / `stub_zero`, conditioned on
  `verified=True`.~~ **SUPERSEDED by Phase CMCP-GATE-0615** — the
  `verified=True` conditioning still failed to cover a row matched via
  non-carrier DBs while BOTH count carriers were down (the 06-15 row_018
  FAIL). The entire citation_count surface is now non-gating. See below.
- **Discrepancy structural shape** — each emitted `discrepancies` entry carries
  `rule`+`field`+`resolved_to`. The old frozen-baseline
  `discrepancies_required`/`forbidden` set-membership is **demoted to
  non-gating** (it shrank/grew with the live DB set); membership drift now
  surfaces in the snapshot tier.
- **Carrier-invariant NEGATIVES** — for no-match/fabrication rows, gate
  `match_found=False` + null `doi`/`pmid`/`arxiv` (a real DB matching a
  fabrication, or surfacing an identifier for one, is always a connector bug).
  Only **row_029** is such a row; its negative was strengthened (it now gates
  the null identifiers it previously omitted). row_029's `rejected_by` was
  demoted (carrier-variant per Phase 2.D).
- **120 s hang backstop**.

Everything else DEMOTES to the non-gating snapshot tier: every POSITIVE
value/decision — `year`, `doi`/`pmid`/`arxiv_id` resolved, `first_author_surname`,
`journal`, `confidence`, `match_quality`, `match_found=True`, `citation_count`
value.

- **Loader** (`tests/fixtures/loader.py`): Rule 7/9 read completeness/quality
  from `expected.snapshot` **uniformly** (the v12 `YEAR_FROM_SNAPSHOT_ROWS`
  named-set stopgap is removed); Rule 6 reads adversarial baselines from
  snapshot. **Rule 13** (the un-reintroducibility lever) RAISES if any
  `expected.hard` re-arms a positive denylisted field — so re-introducing the
  flaky gate fails in the test suite (`tests/test_regression_30_fixture.py`),
  not at 2am. Denylist sets: `HARD_DENYLIST_ALWAYS`,
  `HARD_DENYLIST_IDENTIFIERS` (non-null only) + `match_found=True`.
- **Comparator** (`tests/fixtures/comparator.py`): `TolerantTierResult.passed`
  gated on `citation_count` + `discrepancies_structural`. **Phase CMCP-GATE-0615
  reduces this to `discrepancies_structural` only** (see below).
- **Correctness** now lives in `tests/test_gate_opt1_correctness.py` (T1–T6,
  mocked per-DB payloads incl. the row_013 OpenAlex-sole-carrier Cursi 2022
  case the live gate gave up).
- **Acceptance**: `python -m harness.replay <report_*.raw.json>` feeds a saved
  nightly sidecar through the current comparator offline and prints the gate
  verdict — the deterministic check for the reduction (06-12 FAIL-day → PASS,
  06-09 clean → PASS). Commits `014df9d`, `b04c92d`, `4a05761`.
- **Out of scope / follow-up**: OpenAlex was sole carrier for row_013 and timed
  out 13× on 06-12 — read-timeout/retry tuning + prioritizing the pending
  academic-access request is a coverage-quality (not gate-stability) follow-up.
  (See "Phase CMCP-OAX-DISCO" below for the read-only retry-path diagnosis.)

## Phase CMCP-GATE-0615 — citation_count surface fully non-gating

The 2026-06-15 nightly gate FAILed (tolerant, row_018 only) under the reduced
GATE-OPT1 gate with `src/` byte-identical to `bae3ac9`. Root cause
(CMCP-DIAG-0615): row_018 matched via Crossref+PubMed (`verified=True`, DOI
`10.1007/s11548-024-03178-z`) while BOTH citation_count carriers were down at
once — OpenAlex `ReadTimeout`, Semantic Scholar `429` — so
`canonical.citation_count=None` on a matched row. GATE-OPT1's `stub_null` guard
was conditioned on `verified=True` (to absorb the *no-match* sole-carrier case,
e.g. row_013 on 06-12), but that conditioning does NOT cover a row matched via
non-carrier DBs while the carriers are down. `citation_count` presence is
entirely upstream-availability-controlled (carriers = `[openalex,
semantic_scholar]` only — see `_citation_counts` in `scoring.py`), so gating on
it (value OR stub state) is the same category error GATE-OPT1 removed elsewhere.

**This change removes the entire citation_count surface from the tolerant
gating set.** It supersedes the GATE-OPT1 `not_per_source_dict` /
`stub_null` / `stub_zero` gating bullets above.

The tolerant gate now enforces **ONLY discrepancy structural well-formedness**
(each emitted `discrepancies` entry is a dict with non-empty `rule`+`field` and
a `resolved_to` key). `citation_count` anomalies and `soft_failures` are still
computed and emitted as informational telemetry, but do NOT affect pass/fail.

- **Comparator** (`tests/fixtures/comparator.py`): the gating set is now the
  explicit, introspectable `TOLERANT_GATE_FIELDS = ("discrepancies_structural",)`;
  `TolerantTierResult.passed` iterates it. `TOLERANT_GATE_DENYLIST =
  frozenset({"citation_count"})` names what must never re-enter it.
- **Loader Rule 14** (`tests/fixtures/loader.py`): un-reintroducibility lever,
  sibling to Rule 13 — RAISES `FixtureValidationError` if any denylisted field
  re-enters `comparator.TOLERANT_GATE_FIELDS`, so re-arming the flaky gate fails
  in `tests/test_regression_30_fixture.py` (CI), not at 2am. Read via the
  module object so a monkeypatched gating set is honored.
- **Coverage** relocated to `tests/test_gate_opt1_citation_count.py` (6 tests):
  (i) a count carrier returning a real count → connector surfaces a non-null
  per-source count (the genuine stub-bug target, now deterministic); (ii) a
  matched row with BOTH carriers down → comparator does NOT gate (locks the
  06-15 case), exercised at the comparator boundary AND end-to-end via
  `verify_citation`; plus the Rule 14 injection test.
- **Acceptance**: replay `report_20260615T093002Z.raw.json` → PASS (row_018 now
  a non-gating snapshot diff); replay `report_20260614T093002Z.raw.json` (clean
  pre-15 run) → PASS; suite 345 passed / 2 skipped; `git diff bae3ac9 -- src/`
  empty (no connector change — harness/test-tree only).

## Phase CMCP-OAX-DISCO — upstream transient-failure handling (read-only diagnosis)

Read-only confirmation of how OpenAlex / Semantic Scholar handle transient
upstream failures (Open Issue #2). **Hypothesis (timeouts bypass the retry
path) is REFUTED.**

- **Transport timeouts ARE retried.** Both clients' `_request` catch
  `(httpx.TransportError, httpx.TimeoutException)` — which includes
  `ReadTimeout`/`ConnectTimeout`/`PoolTimeout` — and retry up to `_MAX_RETRIES=3`
  (4 attempts total), then re-raise (`openalex.py:188-211`,
  `semantic_scholar.py:133-156`). Crossref/PubMed share the same dual-trigger
  shape. The 06-15 `ReadTimeout` was a post-exhaustion re-raise, not a
  fall-through.
- **429/5xx** are retried with **`Retry-After` honored** (`max(backoff,
  Retry-After)`, capped at `_BACKOFF_CAP=10s`); after retries a 429 is *returned*
  and `raise_for_status()` converts it to the `HTTPStatusError` seen on 06-15.
- **Timeout config**: bare float `_DEFAULT_TIMEOUT=15.0` ⇒ httpx applies 15s to
  each phase (connect/read/write/pool). **Per-DB clients**, not shared. The
  per-DB semaphore (OpenAlex 50, SS 20 auth / 1 anon) wraps the whole retry loop,
  so one hung request can hold a slot ~70s across 4 attempts + backoff.
- **Keys present** (running systemd env): no `"… is not set"` startup warning in
  the journal for either SS or OpenAlex ⇒ SS runs authenticated (20 rps).
- **No retry/timeout/backoff test coverage** for the OA/SS/Crossref/PubMed retry
  loops (only arXiv's distinct 429 soft-warning loop is tested).
- **Fix posture**: largely "working as designed" — the residual failures are
  genuine upstream unavailability / rate-limit walls; the primary lever is
  raising the ceiling (academic / higher-tier access). Minor honest code knobs:
  a per-phase `httpx.Timeout` (shorter connect, longer read) and lifting
  `_BACKOFF_CAP` so a `Retry-After` ≥10s is fully honored. Deferred to a
  separately-approved task.

## Phase 1.D.1 / 1.D.4 known limitations (1.D.2 backlog)

- **Verifier failure reason is not logged.** `CitationMcpTokenVerifier`
  catches every `InvalidTokenError` and returns `None` to the SDK, which
  emits a canned 401 with no detail. From the journal alone it's
  impossible to distinguish "no Authorization header" from "expired" /
  "bad signature" / "wrong audience". A one-line `logger.warning` in the
  except clause would close this — diagnostic gap, not a security issue.
- **Tool name is not logged on `CallToolRequest`.** The SDK logs
  `Processing request of type CallToolRequest` at INFO but omits the
  tool name. Identifying which of the three tools was called requires
  decoding the request payload (not logged) or correlating with claude.ai
  client traces. A wrapper in `register_tools` could log `name=` at INFO.
- **Orphan DCR clients accumulate.** Each failed connector bring-up
  registers a fresh DCR client; client_id rows accumulate in `oauth.db`
  with no cleanup. Six rows after one bring-up session, not actively
  harmful. A `DELETE FROM clients WHERE created_at < now - 30 days AND
  client_id NOT IN (SELECT DISTINCT client_id FROM refresh_tokens WHERE
  revoked_at IS NULL)` style sweep is the right shape.

## Phase v0.3.5 refinements (vs. 1.D.2 / 1.D.4)

- **`parse_author_string` rewrite (v0.3.5)** (scoring.py). Handles three
  explicit forms — Western `"Given Family"`, NLM `"Family Initials"`, Comma
  `"Family, Given"`. Initials-Family inputs like `"A J Wakefield"` fall
  through to the Western branch's last-token-is-family rule, which produces
  identical output because their family is single-token. A dedicated
  Initials-Family branch was introduced in v0.3.5.C.2 and removed in
  v0.3.5.C.3 after Phase v0.3.5.F.1 regeneration revealed it caused
  parser-vs-DB mismatches on multi-token-family + leading-initial inputs
  (e.g. `"S. Campaña Bastidas"`) without adding value for canonical inputs.
  Detection rule: a token is an "initial" if, period-stripped, it is 1–3
  alphabetic all-uppercase characters; comma always wins. The `given` half
  of the returned tuple is best-effort — no production consumer reads it;
  only the unit test and `_empty_canonical_from_input` use it.

- **`_empty_canonical_from_input` routes string authors through the parser.**
  Pre-v0.3.5 the no-match canonical fallback dumped raw NLM strings into
  `{family: "Polack FP", given: ""}`. v0.3.5 routes string inputs through
  `parse_author_string` so no-match outputs are structurally consistent
  with matched-path outputs (which are always structured `{family, given}`
  from the DB clients).

- **`force_refresh: bool = false` parameter** on all three caching tools
  (`verifyCitation`, `bulkVerifyCitations`, `resolveIdentifier`). When
  `true`, the read-cache is bypassed; the write path is unchanged so fresh
  results populate the cache via `INSERT ON CONFLICT DO UPDATE`.
  `make_cache_key` does NOT incorporate `force_refresh` — that would defeat
  the overwrite-stale-value semantic by writing to a different row than
  normal calls. Forced refreshes get fresh full TTL based on the new match
  quality. Use only for testing, validation, or after known upstream-DB
  updates.

## Phase v0.3.5 known behaviors and deferred consolidations

- **Year-guard precedence.** The year sanity guard fires only when BOTH
  `input_year` and `cand_year` are populated. When the candidate lacks a
  year, the guard skips and downstream checks (first-author-mismatch,
  title-similarity) run instead. Observed at Phase 2.D row_029: predicted
  `year_off_by_more_than_one`, actual `title_similarity_below_floor`
  because the candidate had no year field. Behavior is correct; documenting
  so the precedence is visible to future readers.

- **Authors shape asymmetry (input vs output).** Input `authors` accepts
  `list[str]` only on the MCP-published schema. Output `canonical.authors`
  is always `list[{family, given}]` regardless of input form. This is
  deliberate post-merge canonicalization at the scoring layer — string
  inputs route through `parse_author_string` (for no-match paths via
  `_empty_canonical_from_input`) or DB-side splitters (for matched paths).
  Downstream consumers should expect the dict shape on output.

- **Cross-tool cache-warning asymmetry.** `verify_citation` and
  `bulk_verify_citations` append `{"source": "cache"}` to the warnings list
  on cache hits. `resolve_identifier` does not — its cached early-return
  passes the stored result through unchanged. To detect cache hits in
  tests, use the warning convention for verify/bulk and per-DB call counts
  for resolve. The `databases_queried` field is also NOT a reliable
  cache-hit signal: cached returns include the originally stored value.
  Harmonization is a v0.4+ candidate (would change resolve's response
  shape for downstream consumers — non-zero blast radius).

- **Parallel name-splitters (deferred consolidation candidate).** Three DB
  clients have string-parsing name splitters with the same NLM blind-spot
  that `parse_author_string` was rewritten to fix in v0.3.5: `_split_name`
  in `databases/openalex.py` (byte-identical to S2's), `_split_name` in
  `databases/semantic_scholar.py`, and inline name-split in
  `databases/arxiv.py`. They work in practice because DBs return non-NLM
  display strings, but a theoretical Layer 3 cross-DB discrepancy could
  surface if any DB ever returned an NLM string. v0.4+ candidate: extract
  a shared `split_display_name()` helper using `parse_author_string`-
  equivalent three-form logic. Deferred from v0.3.5 to keep hotfix scope
  tight.

- **Fixture tolerant-tier discrepancy assertions are shape-only.** Rows
  whose `expected.tolerant.discrepancies_required` references
  `citation_count` (notably rows 017 and 020) assert the `(rule, field)`
  tuple shape, not specific numeric values. Upstream `citation_count`
  drift is absorbed automatically — no reconciliation needed when DB-side
  counts change. Only structural changes in the discrepancy generation
  (new rule types, removed fields) would invalidate these assertions.
  **As of Phase GATE-OPT1 these `discrepancies_required`/`forbidden`
  set-membership checks no longer GATE** — they are computed and reported,
  but the tolerant gate is `citation_count` (structural guards) +
  `discrepancies_structural` (well-formedness) only. See the GATE-OPT1
  section above.
