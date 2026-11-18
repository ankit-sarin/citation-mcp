# Changelog

All notable changes to citation-mcp are documented in this file.

For release history prior to v0.3.3, see the project's unified planning
documents. Source-pin versions (`pyproject.toml`, `__version__`) were not
incremented after v0.3.0: the v0.3.1 milestone was a deployment-only event
(systemd unit installation; no source change, no git tag), and the v0.3.2
tagged release (SDK host-check disable, commit `b9c50aa`) shipped without
a corresponding source-pin bump. v0.3.3 brings the pin in sync with the
tag history.

## [Unreleased]

### Added
- **Per-database request pacing.** Crossref, PubMed and Semantic Scholar requests are
  now spaced so that citation-mcp stays inside each service's published rate limit,
  however many citations or calls are in flight. Every attempt is paced, retries
  included. Crossref list/query searches and single-record DOI lookups are paced
  separately, because Crossref limits them separately. OpenAlex and arXiv are
  unchanged (arXiv already keeps 3 s between requests).
- **Configurable rates**, read once at startup from the environment (defaults in
  brackets): `CITATION_MCP_RATE_SEMANTIC_SCHOLAR` (0.7 req/s),
  `CITATION_MCP_RATE_CROSSREF_QUERY` (2.5), `CITATION_MCP_RATE_CROSSREF_SINGLE` (8),
  `CITATION_MCP_RATE_PUBMED` (7, with an NCBI key; keyless stays at 3). A value that is
  not a positive number stops the server at startup with an error naming the variable.
- **A time budget per tool call**, `CITATION_MCP_BULK_BUDGET_SECONDS` (210 s). It applies
  to verifyCitation, bulkVerifyCitations (one budget for the whole batch) and
  resolveIdentifier. No request to any database starts after the budget runs out: when a
  request, a retry or Retry-After wait, or arXiv's 3 s spacing could not start in time,
  that database is skipped for that citation instead of waiting. It is listed in
  `databases_failed` with the warning `"<db> skipped: rate budget exhausted"`, and the
  call still returns a result for every citation. A request already in flight may finish
  after the budget, within its client timeout (10–30 s), which keeps a call inside the
  300 s tool-call limit.
- README: an Acknowledgements section, including the citation requested for Semantic
  Scholar data, and a "Request pacing and time budget" section documenting the five
  settings.

### Changed
- **One database can no longer fail the whole call.** Any ordinary error from one
  database (not only HTTP errors) is now reported as that database failing, in the
  existing `databases_failed` + error-warning form, and the other databases' answers are
  still used. This applies to verifyCitation, bulkVerifyCitations and resolveIdentifier.
- **The contact address is redacted.** The query-string redaction used for logs and for
  error and warning text now blanks `email=` and `mailto=` values as well as API keys.
- **Results missing a database are cached for 24 hours, not 14 days.** A match is cached
  for 14 days only when every queried database answered; if any database failed or was
  skipped, the result is cached for 24 hours so the gap is refilled soon. The same rule
  now applies to resolveIdentifier. The cache-key version moves from 2 to 3, so
  verifyCitation entries cached under the old rule are not served (resolveIdentifier
  keys are unversioned; its old entries expire on their own TTL).

### Unchanged
- The shape of every tool result: no new keys, no changed field types.
- Matching decisions, the Retry-After handling and the backoff schedule.
- No version strings change in this entry.

## [1.0.4] — 2026-11-16

Correctness release. Tool inputs are unchanged. Response keys are unchanged; some values change as described.

### Fixed

- A lookup with a title and no author could accept a different paper with a loosely similar title. Without an author, a candidate now needs a title similarity of at least 0.90, or a title that differs only by a subtitle.
- The canonical record could combine fields from different papers, and the reported score could describe a different paper from the canonical record. Records that disagree with the best match are now excluded from the merge and reported in `warnings`.
- Lookups that differed only in `pmid`, `arxiv_id` or another input field could share a cache entry and return another lookup's result. The cache key now covers every input field. Existing cache entries are not reused.
- With equal confidence, an identifier match is now reported in preference to a metadata match.

### Changed

- The User-Agent sent to Crossref, PubMed and arXiv carries the package version.
- Version bumped 1.0.3 → 1.0.4 (`pyproject.toml`, `citation_mcp.__version__`, `uv.lock`, `CITATION.cff`).

## [1.0.3] — 2026-11-16

No change to tool inputs, outputs or matching behaviour.

### Added

- `date-released` in `CITATION.cff`.

### Changed

- The default contact address sent to Crossref and PubMed is corrected to the
  maintainer's current address. `CROSSREF_POLITE_EMAIL` still overrides it.
- The package author email in `pyproject.toml` is corrected.
- Version bumped 1.0.2 → 1.0.3 (`pyproject.toml`, `citation_mcp.__version__`,
  `uv.lock`, `CITATION.cff`).

### Removed

- A personal email address from a test fixture and the developer notes.

## [1.0.2] — 2026-11-14

No functional change.

### Added

- `CITATION.cff` — citation metadata for the repository.
- `.gitattributes` — excludes `CLAUDE.md` from source archives
  (`export-ignore`).

### Changed

- Version bumped 1.0.1 → 1.0.2 (`pyproject.toml`, `citation_mcp.__version__`,
  `uv.lock` editable entry) for Zenodo archival.

## [1.0.1] — 2026-10-24

### Fixed

- **AUD-FIX-1024 — canonical RFC 8707 resource; `aud` always canonical**
  (`c144b5d`). The protected-resource document advertised `OAUTH_AUDIENCE`
  without its trailing slash, `/oauth/token` minted `aud` from the raw client
  resource, and the verifier exact-matched the slashed configured audience.
  Since 2026-10-17 claude.ai has sent the slashless resource it read from the
  protected-resource document, so every token it received 401'd at `/mcp`
  (refresh → 401 loop).
  - `tokens.canonical_resource()`: single owner of the trailing-slash rule.
  - `/authorize` + both token grants: resource absent or equal up to the
    trailing slash → accept; else `invalid_target`, no code or token issued,
    grant untouched.
  - Minted `aud` is always the canonical value; the verifier also accepts the
    slashed form so tokens issued before this fix stay valid.
  - Harness sends the protected-resource value on authorize, code exchange
    and refresh, so the nightly run uses the same path as claude.ai.
  - Version bumped 1.0.0 → 1.0.1 (`pyproject.toml`, `citation_mcp.__version__`,
    `uv.lock` editable entry). Tests: 345 → 406 passed, 2 skipped.

### Changed

- **Harness: `citation_count` demoted from gating to snapshot-informational**
  (`01dd96c`). `citation_count` organic drift no longer fails the v1.0 gate.
  Replaced the ±20% tolerant band with coarse stub guards (`stub_null`,
  `stub_zero`); added a 10× tripwire behind
  `CITATION_COUNT_ORDER_OF_MAGNITUDE_GUARD` (default off). Hard tier and
  cross-DB discrepancy tier unchanged. `report.py` now writes a `.raw.json`
  sidecar with per-row actuals.
- **Phase GATE-OPT1 — gate reduced to carrier-invariant checks**
  (`014df9d`, `b04c92d`, `4a05761`, `e909112`). No connector `src/` change.
  - Harness (`014df9d`): `citation_count` stub guards conditioned on
    `verified=True`; added a `not_per_source_dict` structural guard; replaced
    frozen-baseline `discrepancies_required`/`forbidden` gating with a
    structural well-formedness check (each entry carries
    `rule`+`field`+`resolved_to`). Required/forbidden remain computed for the
    report but no longer gate.
  - Fixture (`b04c92d`): emptied the hard block on every matched row; retained
    only the carrier-invariant negative on no-match row_029
    (`match_found=False` + null doi/pmid/arxiv, strengthened). Loader Rule 7/9
    read completeness/quality from `expected.snapshot` uniformly, collapsing
    the `YEAR_FROM_SNAPSHOT_ROWS` named-set stopgap; Rule 6 reads adversarial
    baselines from snapshot; added Rule 13 (re-arming any positive denylisted
    field in `expected.hard` fails the loader).
  - Tests (`4a05761`): T1–T6 deterministic mocked correctness (incl. the
    row_013 OpenAlex sole-carrier case) and `python -m harness.replay
    <sidecar.raw.json>` offline replay entrypoint, codified in
    `tests/harness/test_replay.py`.
- **Phase CMCP-GATE-0615 — `citation_count` surface demoted out of tolerant
  gating** (`34d3802`). The 06-15 nightly FAIL (row_018) was a matched row
  whose only `citation_count` carriers were both down. The tolerant gating set
  is now `TOLERANT_GATE_FIELDS = ("discrepancies_structural",)` with
  `TOLERANT_GATE_DENYLIST = {"citation_count"}`; stub anomalies are still
  computed and emitted but non-gating. Loader Rule 14 raises if a denylisted
  field re-enters the gating set. Coverage relocated to
  `tests/test_gate_opt1_citation_count.py`. Suite 345 passed / 2 skipped;
  `src/` byte-identical to `bae3ac9`.
- Regression fixture `tests/fixtures/regression_30.json` — per-row hard-tier
  gate scoping for edition/availability-volatile fields (baselines preserved
  in `expected.snapshot`; drift now surfaces as non-gating snapshot diffs
  instead of false gate failures):
  - **row_030**: gate scoped to edition-invariant fields only (Crossref
    re-ranks the Tukra book-chapter editions). (`063ab14`)
  - **row_016 / row_028**: `year` de-gated (earliest-year preprint/publication
    duality trap). The loader's Rule 7 mandatory-non-null-`year` assertion now
    reads `expected.snapshot.canonical.year` for the named set
    `{"row_016", "row_028"}` (set later removed by GATE-OPT1, b04c92d).
    (`d4b766c`)
  - **row_016 / row_017 / row_028**: `arxiv_id_resolved` de-gated where it is
    upstream-availability-dependent (title-only rows have Semantic Scholar as
    the sole carrier; row_016 was observed dropping to `None` on 2026-06-08
    during an SS outage). **row_027 keeps `arxiv_id_resolved` gated** — it is
    echo-backed and never flips. (`d4b766c`, `d7b787e`)
- Regression-30 tooling and v0.3.5 response snapshot tracked (`e6266b0`). Six
  sibling scripts of the already-tracked `regenerate_v0_3_5.py` were never
  committed, nor was the response snapshot that script stages. The snapshot is
  retained rather than gitignored because it is not reliably regenerable —
  reproducing it means re-querying five live citation databases whose
  responses drift over time. Additive only: no tracked file is modified.

### Tests

- `tests/test_arxiv_id_authority.py` — 7 deterministic invariants guarding the
  `canonical.arxiv_id` authority list (`[arxiv, semantic_scholar]` only) and
  the title-only-sole-carrier / explicit-arxiv-echo / arXiv+SS-redundancy
  behaviors. No network/auth. (`60bf2c8`)
- `tests/test_arxiv_id_authority_live.py` — opt-in live upstream-contract check
  (gated behind `CITATION_MCP_LIVE=1`; `pytest.skip` on SS rate-limit, never
  flakes). (`60bf2c8`)
- `tests/test_regression_30_fixture.py` — extended to assert the Rule 7
  `year`-from-snapshot relocation for the named set. (`d4b766c`)

## [1.0.0] — 2026-05-28

First stable release. The deployed connector is validated end-to-end
against a 30-citation regression baseline through the live OAuth +
Cloudflare Tunnel + bulk-endpoint path.

### Added

- Validation harness (`harness/`): OAuth 2.1 PKCE client, MCP Streamable
  HTTP transport client with reactive 401-refresh, three-tier comparator
  (hard / tolerant / snapshot), Markdown report generation, and the
  `validate` CLI subcommand running cold-cache and warm-cache bulk
  passes.
- Nightly validation via cron (`harness/run_nightly.sh`, 09:30 UTC) with
  morning-digest gate reporting.

### Notes

- The validation gate measures connector-controlled correctness only —
  citation matching, canonical merge, citation-count bands, and
  discrepancy detection. Upstream-database latency and transient
  single-DB availability are reported informationally and do not gate.

## [0.3.5] — 2026-05-27

### Fixed

- `parse_author_string` now correctly handles NLM "Family Initials" form
  (e.g. `"Polack FP"`), comma form (`"Polack, Fernando P."`), and
  Initials-Family form (`"A J Wakefield"`). Previously NLM and
  Initials-Family inputs mis-extracted the surname; `"Polack FP"` returned
  `("FP", "Polack")` instead of `("Polack", "FP")`. Cascade fix applies to
  `normalize_author_surname` and all downstream Layer 2 first-author /
  other-authors scoring, Layer 3 cross-DB author discrepancy detection,
  metadata-lookup cache keys, and server-side DB query construction for
  non-Western author inputs.
- `_empty_canonical_from_input` no-match fallback now routes string author
  inputs through `parse_author_string`. Previously,
  `authors=["Polack FP"]` on a no-match path returned canonical
  `{"family": "Polack FP", "given": ""}`; now returns
  `{"family": "Polack", "given": "FP"}`.

### Added

- `force_refresh: bool = false` optional parameter on `verifyCitation`,
  `bulkVerifyCitations`, and `resolveIdentifier`. When `true`, bypasses
  the read-cache and forces a fresh DB roundtrip; results are still
  written to cache via `INSERT ... ON CONFLICT(key) DO UPDATE` so
  subsequent normal calls benefit from the refresh. Intended for testing,
  validation, or after known upstream-DB updates.

### Changed

- Regression fixture `tests/fixtures/regression_30.json` refreshed for 6
  of 30 rows: rows 003, 017, 020, 027 absorb Semantic Scholar
  `citation_count` drift; row 028 captures persistent arXiv availability
  change; row 030 captures Crossref alternate-paper shift on the
  adversarial title-only test input. `expected.tolerant.citation_count`
  ±20% bands absorb the drift; `expected.snapshot` regenerated to reflect
  new responses. Twenty-four rows byte-unchanged.

### Tests

Suite expanded from 190 to 228 (+38 tests):

- 21 new parametrized `parse_author_string` cases covering the
  four-input-form matrix
- 6 new `normalize_author_surname_both_formats` cases covering NLM,
  Initials-Family, and accented inputs
- 3 new `_empty_canonical_from_input` integration tests
- 6 new `force_refresh` tests (2 per tool × 3 tools)
- The Phase 1.E.1 regression fixture (introduced at `6a4dc81`, May 26)
  contributes the loader-validated baseline; v0.3.5 refreshes 6 of its
  rows.

### Internal

- Parser implementation evolved during development: a dedicated
  Initials-Family branch added in v0.3.5.C.2 was removed in v0.3.5.C.3
  after the Phase 1.E.2.A regeneration revealed it caused parser-vs-DB
  mismatches on multi-token-family + leading-initial inputs (e.g.
  `"S. Campaña Bastidas"`) without adding value for canonical
  Initials-Family inputs (e.g. `"A J Wakefield"`) whose family is
  single-token and handled identically by the Western branch's
  last-token-as-family rule. Final parser has three explicit branches:
  comma, NLM, Western (which handles Initials-Family via fall-through).

## v0.3.3 — Phase 1.D.5 (diagnostic logging)

### Added

- `verify_token` now logs at WARNING level with PyJWT exception class and
  truncated message on token-validation failure. Previously DEBUG-only with
  class name. This is the sole auth-failure log surface for bearer-token
  rejection on `/mcp` — SDK bearer middleware is silent on rejection.
- Each registered tool handler (`verifyCitation`, `bulkVerifyCitations`,
  `resolveIdentifier`) now emits an INFO-level `tool_call name=<toolName>`
  log at entry. Tool arguments are not logged.

### Fixed

- Source-pin catch-up: `pyproject.toml` and `src/citation_mcp/__init__.py`
  bumped from `0.3.0` to `0.3.3` to reflect actual release state.

### Tests

- 188 passing (was 186); two regression tests added covering the new
  log surfaces and the no-token-leak invariant.
