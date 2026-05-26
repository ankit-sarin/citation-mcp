# Changelog

All notable changes to citation-mcp are documented in this file.

For release history prior to v0.3.3, see the project's unified planning
documents. Source-pin versions (`pyproject.toml`, `__version__`) were not
incremented for tag-only releases v0.3.1 (deployment-only) and v0.3.2
(SDK host-check disable, commit `b9c50aa`); v0.3.3 brings the pin in sync
with the tag history.

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
