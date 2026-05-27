# citation-mcp

A self-hosted Model Context Protocol server for citation verification, integrity,
and discovery — built to complement Anthropic's hosted PubMed connector and
`web_fetch` by filling the gaps they cannot.

**Status:** Phase 1.D — multi-database verification, bulk verify, identifier
resolution, plus Streamable HTTP transport behind OAuth 2.1 + PKCE +
Dynamic Client Registration. stdio transport remains unchanged. Cloudflare
Tunnel / Access deployment lands in a downstream step.

## Features

- `verifyCitation` — fans out a single citation across Crossref, PubMed,
  OpenAlex, and Semantic Scholar in parallel (arXiv is opt-in, queried only
  when input has an explicit `arxiv_id`), merges the per-DB records into a
  single canonical, and surfaces inter-database discrepancies
- `bulkVerifyCitations` — verifies up to 200 citations per call, with
  citation-level concurrency control
- `resolveIdentifier` — cross-converts DOI ↔ PMID ↔ arXiv ID ↔ OpenAlex Work
  ID ↔ Semantic Scholar paper ID

## Tool usage notes

### Forcing a fresh fetch

All three caching tools accept an optional `force_refresh: bool = false`.
Pass `true` to bypass the read-cache and force a fresh DB roundtrip; results
are still written to cache for subsequent normal calls.

```text
verifyCitation(doi="10.1056/NEJMoa2034577", force_refresh=true)
bulkVerifyCitations(citations=[...], force_refresh=true)
resolveIdentifier(identifier="10.1056/NEJMoa2034577", from_type="doi", force_refresh=true)
```

Use only for testing, validation, or after known upstream-DB updates.
Routine production calls should leave it at the default.

### Authors field — input vs output shape

`verifyCitation` and `bulkVerifyCitations` accept `authors: list[str]` only
on input. The response's `canonical.authors` is always
`list[{family, given}]` regardless of input form. String inputs are parsed
into structured form at the scoring layer (Comma, NLM, Initials-Family, and
Western forms are recognised).

## Quick start

```bash
git clone git@github.com:ankit-sarin/citation-mcp.git
cd citation-mcp
uv sync
uv run pytest
```

To launch the server interactively in mcp-inspector:

```bash
uv run mcp dev src/citation_mcp/server.py
```

To run the stdio server directly (for an MCP client to launch):

```bash
uv run citation-mcp
```

To launch the HTTP transport (requires `OAUTH_SIGNING_KEY` — see
[HTTP transport and OAuth](#http-transport-and-oauth)):

```bash
export OAUTH_SIGNING_KEY=$(openssl rand -hex 32)
uv run citation-mcp --transport http --port 8080
```

## Configuration

Set whichever database keys you have; missing keys cause that backend to
degrade gracefully (warning logged at startup, skipped for live queries).

```bash
export CROSSREF_POLITE_EMAIL=you@example.org
export NCBI_API_KEY=...
export OPENALEX_API_KEY=...
export SEMANTIC_SCHOLAR_API_KEY=...
```

## Security / Logging

httpx logs each outbound request URL at INFO. NCBI E-utilities and OpenAlex
authenticate via `api_key=` query params, which would otherwise leak the
key into log files and terminal scrollback. On package import, citation-mcp
installs `QueryParamRedactionFilter` on the `httpx` logger; sensitive query
values (`api_key`, `apikey`, `api-key`, `key`, `token`, `access_token`,
`refresh_token`, `client_secret`) are rewritten to `***` before emission.
Header-based auth (e.g. Semantic Scholar's `x-api-key`) is unaffected
because httpx does not log request headers at INFO.

**Exception-string redaction.** Each `raise_for_status()` call site in the
database clients wraps in `try / except httpx.HTTPStatusError /
reraise_redacted`, which re-raises with the URL query string scrubbed.
This closes the path where `httpx.HTTPStatusError.__str__` would otherwise
embed the unredacted URL in logs and tracebacks.

**Re-attaching after dictConfig.** If a consumer reconfigures Python
logging via `logging.config.dictConfig` after import, call
`citation_mcp.log_redaction.install_redaction_filter()` afterward to
reattach the filter.

## HTTP transport and OAuth

`--transport http` exposes Streamable HTTP at `/mcp`, behind OAuth 2.1
(authorization code + PKCE, refresh-token rotation with reuse detection),
Dynamic Client Registration at `/oauth/register`, and the two discovery
documents (`/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource`).

### Required env vars

| Variable | Required | Default | Notes |
|---|---|---|---|
| `OAUTH_SIGNING_KEY` | yes (http) | — | 32-byte hex; generate via `openssl rand -hex 32`. |
| `OAUTH_ISSUER` | no | `https://citation-mcp.digitalsurgeon.dev` | `iss` claim and discovery base URL. |
| `OAUTH_AUDIENCE` | no | same as `OAUTH_ISSUER` | `aud` claim. |
| `OAUTH_DB_PATH` | no | `data/oauth.db` | aiosqlite path for clients + tokens. |
| `OAUTH_ALLOW_MISSING_CF_EMAIL` | no | `false` | Dev only: bypass the Cloudflare Access header check. |
| `OAUTH_DEV_USER_EMAIL` | no | `dev@localhost` | Fallback `sub` when the dev bypass is on. |
| `MCP_ORIGIN_ALLOWLIST` | no | `https://claude.ai` | CSV; enforced on `/mcp` POST. |
| `MCP_HOST_ALLOWLIST` | no | `citation-mcp.digitalsurgeon.dev,localhost,127.0.0.1` | CSV; enforced globally. |
| `MCP_HTTP_HOST` | no | `127.0.0.1` | CLI default, overridden by `--host`. |
| `MCP_HTTP_PORT` | no | `8080` | CLI default, overridden by `--port`. |

### CVE defenses

Three custom middlewares are wired into the HTTP transport:

- **`HostHeaderMiddleware`** → `421 Misdirected Request` when the `Host`
  header is not in `MCP_HOST_ALLOWLIST`. Defends against CVE-2026-35568
  (Java SDK DNS rebinding); belt-and-suspenders over the SDK's internal
  CVE-2025-66416 fix and applies to all paths beyond `/mcp`.
- **`McpOriginMiddleware`** → `403 Forbidden` when a `/mcp` POST carries a
  non-allowlisted `Origin`. Defends against CVE-2026-33252 (Go SDK
  cross-site POST). Absent `Origin` is allowed (server-to-server flows).
- **`McpContentTypeMiddleware`** → `415 Unsupported Media Type` when a
  `/mcp` POST is not `application/json`. Belt-and-suspenders against the
  cross-site form-POST vector that backs CVE-2026-33252.

### Trust boundary

In production the server runs behind Cloudflare Access at the edge.
Cloudflare stamps `Cf-Access-Authenticated-User-Email` on requests that
pass its challenge; the authorization server trusts this header as the
user identity (the accompanying JWT is not verified against Cloudflare's
JWKS — this is intentional and documented).

## Roadmap

- **Phase 1.A** — stdio transport, `verifyCitation` tool, Crossref-only, SQLite cache *(shipped)*
- **Phase 1.B** — PubMed, OpenAlex, Semantic Scholar, arXiv clients; `bulkVerifyCitations`, `resolveIdentifier` *(shipped)*
- **Phase 1.D.0** — httpx URL query-param redaction filter *(shipped)*
- **Phase 1.D** — Streamable HTTP transport + OAuth 2.1 + DCR + CVE defenses + exception-string redaction *(shipped)*
- **Phase 1.C** — additional citation-integrity tools
- **Downstream** — Cloudflare Tunnel + Cloudflare Access (Google SSO), claude.ai custom-connector registration

## License

MIT — see [LICENSE](LICENSE).

---

Built and maintained at [digitalsurgeon.dev](https://digitalsurgeon.dev).
