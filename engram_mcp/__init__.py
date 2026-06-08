"""engram-mcp — mount the Engram second brain as an MCP shard.

A standalone always-on Python service (contract §11/§11.1 F1) that wraps the
existing Valinor Engram seams (engram_writer / brain_sync / selene_review)
in-process and exposes them as MCP tools over stdio (local Claude Code plugin)
and http (LAN — MacBook + a later Hermes adapter). Rust is untouched.
"""

from engram_mcp.client import EngramClient, EngramError, ERROR_CODES

__all__ = ["EngramClient", "EngramError", "ERROR_CODES"]
__version__ = "0.1.0"
