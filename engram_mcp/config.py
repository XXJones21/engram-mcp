"""Per-user configuration for engram-mcp.

The Engram knowledge base lives on the Windows FS at
``D:/Tools/personalAI/Engram`` and is read/written by Python functions that
live in the Valinor checkout (``Server/tools/engram_writer.py``,
``Server/tools/brain_sync.py``, ``scripts/selene_review.py``). engram-mcp wraps
those functions in-process (the ``local`` transport) or calls a LAN service that
wraps them (the ``http`` transport).

Like ``comfy-local-mcp``, machine-specific bits (transport, the home-LAN base
URL, where the Valinor checkout is, the offline cache dir) live in a small JSON
config the user writes once. Everything else reads it; every consumer has a
sensible fallback so the package works with no config at all.

Location: ``~/.engram-mcp/config.json`` (override with ``ENGRAM_MCP_CONFIG``).

Schema (all keys optional)::

    {
      "transport":   "local",                       # local | http
      "base_url":    "http://192.168.1.50:8765",     # the home-LAN engram-mcp http service
      "valinor_root":"D:/Tools/Valinor",             # checkout holding Server/ + scripts/
      "engram_root": "D:/Tools/personalAI/Engram",   # canonical source-of-truth tree (R-A5)
      "source_of_truth_host": "desktop-warm-stack",  # diagnostic id sync_status reports
      "cache_dir":   "/home/me/.engram-mcp/cache"     # Q2 offline read cache (structured-for, not built)
    }

Env overrides (parallel to ``COMFY_*``):
  ENGRAM_MCP_TRANSPORT  local | http        (which MCP transport-side path to use)
  ENGRAM_TRANSPORT      local | http        (alias, mirrors the contract's transport switch)
  ENGRAM_BASE_URL       http://host:port     (http service base url)
  ENGRAM_VALINOR_ROOT   path                 (Valinor checkout for in-process imports)
  ENGRAM_ROOT           path                 (override the canonical Engram root)
  ENGRAM_MCP_HOST / ENGRAM_MCP_PORT          (http serve bind; default 0.0.0.0:8765)
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

# Where the Valinor checkout (with Server/ and scripts/) is most likely to live.
# The first one that contains Server/tools/engram_writer.py wins.
_VALINOR_CANDIDATES = [
    "D:/Tools/Valinor",
    "/mnt/d/Tools/Valinor",  # WSL view of the Windows FS
    str(Path.home() / "Valinor"),
]

# Candidate canonical Engram roots (mirror engram_writer / brain_sync resolution,
# R-A5). The first that exists is the single source of truth.
_ENGRAM_CANDIDATES = [
    "D:/Tools/personalAI/Engram",
    "/mnt/d/Tools/personalAI/Engram",
]


def config_dir() -> Path:
    """Root dir for engram-mcp user state (config + offline cache)."""
    return Path.home() / ".engram-mcp"


def config_path() -> Path:
    """Path to the config file (override with ENGRAM_MCP_CONFIG)."""
    override = os.environ.get("ENGRAM_MCP_CONFIG")
    return Path(override) if override else config_dir() / "config.json"


def default_cache_dir() -> str:
    """Q2 offline read-cache location (structured-for; replay not built this week)."""
    return str(config_dir() / "cache")


def load_config() -> dict[str, Any]:
    """Load the config, or {} if none/unreadable (never raises)."""
    path = config_path()
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``updates`` into the config and write it. Returns the result."""
    cfg = load_config()
    merged = {**cfg, **updates}
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def resolve_transport(explicit: str | None = None) -> str:
    """local (in-process import) | http (LAN service). Default local."""
    cfg = load_config()
    t = (
        explicit
        or os.environ.get("ENGRAM_MCP_TRANSPORT")
        or os.environ.get("ENGRAM_TRANSPORT")
        or cfg.get("transport")
        or "local"
    ).lower()
    if t not in {"local", "http"}:
        raise ValueError(f"transport must be 'local' or 'http', got {t!r}")
    return t


def resolve_base_url(explicit: str | None = None) -> str:
    """The home-LAN engram-mcp http service base url (for the http transport)."""
    cfg = load_config()
    return (
        explicit
        or os.environ.get("ENGRAM_BASE_URL")
        or cfg.get("base_url")
        or "http://127.0.0.1:8765"
    ).rstrip("/")


def resolve_valinor_root() -> Path | None:
    """Locate the Valinor checkout that holds Server/ + scripts/ for in-process import."""
    cfg = load_config()
    explicit = os.environ.get("ENGRAM_VALINOR_ROOT") or cfg.get("valinor_root")
    candidates = ([explicit] if explicit else []) + _VALINOR_CANDIDATES
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if (p / "Server" / "tools" / "engram_writer.py").exists():
            return p.resolve()
    return None


def resolve_engram_root() -> Path | None:
    """Resolve the ONE canonical Engram root (R-A5). Env/config first, then candidates."""
    cfg = load_config()
    explicit = os.environ.get("ENGRAM_ROOT") or cfg.get("engram_root")
    candidates = ([explicit] if explicit else []) + _ENGRAM_CANDIDATES
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if p.exists():
            return p.resolve()
    return None


def source_of_truth_host() -> str:
    """Diagnostic id sync_status reports as the FS owner."""
    cfg = load_config()
    return (
        os.environ.get("ENGRAM_SOURCE_OF_TRUTH")
        or cfg.get("source_of_truth_host")
        or socket.gethostname()
        or "desktop-warm-stack"
    )


def cache_dir() -> str:
    cfg = load_config()
    return cfg.get("cache_dir") or os.environ.get("ENGRAM_CACHE_DIR") or default_cache_dir()
