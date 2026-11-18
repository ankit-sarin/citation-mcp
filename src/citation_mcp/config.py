"""Rate and budget configuration, read once at startup (v1.0.5).

Rates must lie between 0.01 and 1000 requests per second, and the budget between
1 and 3600 seconds, inclusive. A bad value stops startup with a ConfigError
naming the variable. No dotenv file is read; the service gets its
environment from systemd.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Mapping

RATE_BOUNDS = (0.01, 1000.0)  # requests per second, inclusive
BUDGET_BOUNDS = (1.0, 3600.0)  # seconds, inclusive

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


def _bounded(name: str, raw: str, low: float, high: float, unit: str) -> float:
    problem = f"{name} must be a number from {low:g} to {high:g} {unit} (got {raw!r})"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(problem) from None
    if not math.isfinite(value) or not (low <= value <= high):
        raise ConfigError(problem)
    return value


def load_rate_config(environ: Mapping[str, str] | None = None) -> RateConfig:
    env = os.environ if environ is None else environ
    values: dict[str, float] = {}
    for name, (attr, default) in _SETTINGS.items():
        raw = env.get(name)
        if raw is None:
            values[attr] = default
        elif attr == "bulk_budget_seconds":
            values[attr] = _bounded(name, raw, *BUDGET_BOUNDS, "seconds")
        else:
            values[attr] = _bounded(name, raw, *RATE_BOUNDS, "requests per second")
    return RateConfig(**values)
