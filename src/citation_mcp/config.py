"""Rate and budget configuration, read once at startup (v1.0.5).

Each value must be a positive, finite number. A bad value stops startup with a
ConfigError naming the variable. No dotenv file is read; the service gets its
environment from systemd.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Mapping

# Variable name -> (RateConfig attribute, default)
_SETTINGS: dict[str, tuple[str, float]] = {
    "CITATION_MCP_RATE_SEMANTIC_SCHOLAR": ("semantic_scholar", 0.7),
    "CITATION_MCP_RATE_CROSSREF_QUERY": ("crossref_query", 2.5),
    "CITATION_MCP_RATE_CROSSREF_SINGLE": ("crossref_single", 8.0),
    "CITATION_MCP_RATE_PUBMED": ("pubmed", 7.0),  # with an NCBI key; keyless stays 3/s
    "CITATION_MCP_BULK_BUDGET_SECONDS": ("bulk_budget_seconds", 210.0),
}


class ConfigError(ValueError):
    """A configuration variable holds a value the server cannot run with."""


@dataclass(frozen=True)
class RateConfig:
    semantic_scholar: float = 0.7
    crossref_query: float = 2.5
    crossref_single: float = 8.0
    pubmed: float = 7.0
    bulk_budget_seconds: float = 210.0


def _positive(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a positive number (got {raw!r})") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number (got {raw!r})")
    return value


def load_rate_config(environ: Mapping[str, str] | None = None) -> RateConfig:
    env = os.environ if environ is None else environ
    values: dict[str, float] = {}
    for name, (attr, default) in _SETTINGS.items():
        raw = env.get(name)
        values[attr] = default if raw is None else _positive(name, raw)
    return RateConfig(**values)
