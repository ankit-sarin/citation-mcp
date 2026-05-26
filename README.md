# citation-mcp

A self-hosted Model Context Protocol server for citation verification, integrity,
and discovery — built to complement Anthropic's hosted PubMed connector and
`web_fetch` by filling the gaps they cannot.

**Status:** Phase 1.A — Crossref single-database verifier over stdio transport.
Not yet production-ready. Cloudflare Tunnel exposure and the `claude.ai`
custom-connector registration come in later phases.

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

## Roadmap

- **Phase 1.A** — stdio transport, `verifyCitation` tool, Crossref-only, SQLite cache
- **Phase 1.B+** — PubMed, OpenAlex, Semantic Scholar, arXiv clients; `bulkVerifyCitations`, `resolveIdentifier`
- **Phase 2** — HTTP transport + OAuth + Cloudflare Tunnel + Cloudflare Access (Google SSO)
- **Phase 3+** — remaining citation-integrity tools per the phasing schedule

## License

MIT — see [LICENSE](LICENSE).

---

Built and maintained at [digitalsurgeon.dev](https://digitalsurgeon.dev).
