#!/usr/bin/env bash
# Nightly citation-mcp validation harness run (invoked by cron at 09:30 UTC).
# Writes a timestamped report to harness/reports/. Exits 0 on gate pass,
# non-zero on gate fail / auth fail / orchestration error. cron captures
# stdout+stderr to ~/logs/citation_mcp_cron.log.
set -uo pipefail
cd /home/ankitsarin/projects/citation-mcp
exec /home/ankitsarin/.local/bin/uv run python -m harness.cli validate
