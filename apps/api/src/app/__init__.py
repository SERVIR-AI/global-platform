"""SERVIR Global Platform API: a FastAPI service that hosts the governed MCP server."""

from __future__ import annotations

import tomllib
from pathlib import Path

__all__ = ["__version__"]


def _read_version() -> str:
    """The version is written in ONE place, apps/api/pyproject.toml; FastAPI, the MCP
    serverInfo and /api/health all read it from here. A build that ships without the
    file (a bare wheel) reports an unknown version rather than a stale one."""
    try:
        with (Path(__file__).resolve().parents[2] / "pyproject.toml").open("rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except (OSError, KeyError, ValueError, TypeError):
        try:
            from importlib.metadata import version
            return version("global-risk-api")
        except Exception:                       # noqa: BLE001 — not installed, not packaged
            return "0.0.0+unknown"


__version__ = _read_version()
