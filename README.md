# citation-mcp

A self-hosted Model Context Protocol server for citation verification, integrity,
and discovery — built to complement Anthropic's hosted PubMed connector and
`web_fetch` by filling the gaps they cannot.

**Status:** Phase 1.B.1 — multi-database verification, bulk verify, and
identifier resolution over stdio transport. Production deployment (HTTP
transport, OAuth, Cloudflare Tunnel) is still pending — Phase 1.D.

## Features

- `verifyCitation` — fans out a single citation across Crossref, PubMed,
  OpenAlex, and Semantic Scholar in parallel (arXiv is opt-in, queried only
  when input has an explicit `arxiv_id`), merges the per-DB records into a
  single canonical, and surfaces inter-database discrepancies
- `bulkVerifyCitations` — verifies up to 200 citations per call, with
  citation-level concurrency control
- `resolveIdentifier` — cross-converts DOI ↔ PMID ↔ arXiv ID ↔ OpenAlex Work
  ID ↔ Semantic Scholar paper ID

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

## Roadmap

- **Phase 1.A** — stdio transport, `verifyCitation` tool, Crossref-only, SQLite cache *(shipped)*
- **Phase 1.B** — PubMed, OpenAlex, Semantic Scholar, arXiv clients; `bulkVerifyCitations`, `resolveIdentifier` *(shipped)*
- **Phase 1.C** — additional citation-integrity tools
- **Phase 1.D** — HTTP transport + OAuth + Cloudflare Tunnel + Cloudflare Access (Google SSO)

## License

MIT — see [LICENSE](LICENSE).

---

Built and maintained at [digitalsurgeon.dev](https://digitalsurgeon.dev).
