# Changelog

All notable changes to citation-mcp are documented in this file.

For release history prior to v0.3.3, see the project's unified planning
documents. Source-pin versions (`pyproject.toml`, `__version__`) were not
incremented after v0.3.0: the v0.3.1 milestone was a deployment-only event
(systemd unit installation; no source change, no git tag), and the v0.3.2
tagged release (SDK host-check disable, commit `b9c50aa`) shipped without
a corresponding source-pin bump. v0.3.3 brings the pin in sync with the
tag history.

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
