"""engram-mcp — FastMCP server fronting the Engram second brain.

Exposes the 8 contract tools (recall, search, list_projects, sync_status =
readOnlyHint; append, create, promote_fact, organize = mutating, through the
in-process write lock) over MCP.

Transports (mirroring comfy-local-mcp):
  - stdio (local Claude Code plugin)  : python -m engram_mcp.server --transport stdio
  - http  (LAN — MacBook + Hermes A5) : python -m engram_mcp.server --transport http

The service runs in ``ENGRAM_TRANSPORT=local`` on the desktop (the single FS
owner) and imports the Valinor seams in-process. Remote clients run their MCP
stdio plugin with ``ENGRAM_TRANSPORT=http`` pointed at this service's
``/engram/*`` routes (also what the Hermes adapter, task A5, will call).

Run:  engram-mcp [--transport stdio|http]
  or  python -m engram_mcp.server --transport stdio
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from fastmcp import FastMCP

from engram_mcp import config as cfg
from engram_mcp.client import EngramClient, EngramError

mcp = FastMCP(
    name="engram",
    instructions=(
        "Mount the Engram second brain. Read context before working: "
        "recall(kind='project', project='valinor') loads a project's claude.md; "
        "recall(kind='facts') loads operator facts; recall(kind='global') the root "
        "CLAUDE.md; recall(kind='thought', slug=...) a saved session; "
        "recall(kind='file', target='Path/x.md') any .md. search(query) finds across "
        "facts/thoughts/projects. list_projects enumerates mountable contexts. "
        "sync_status confirms you're talking to the live source-of-truth brain. "
        "Record durable knowledge: append(target, content, heading=...) adds under a "
        "heading (the safe default); create(target, content) makes a new .md; "
        "promote_fact(text) stores a durable operator fact. organize runs a Selene "
        "consolidation (Selene/librarian only). All paths are Engram-relative, "
        "forward-slashed, .md only — never absolute, never with '..'. Writes are "
        "serialized server-side; path-safety is enforced server-side."
    ),
)

_client: EngramClient | None = None


def client() -> EngramClient:
    global _client
    if _client is None:
        _client = EngramClient()
    return _client


def _err(e: EngramError) -> dict[str, Any]:
    return {"ok": False, "error": e.code, "detail": e.detail}


# =====================================================================
#  PER-PERSONA TOOL-SCOPING (contract §4 matrix · §4.3 · §11.1.5)
# =====================================================================
# Scoping is enforced SERVER-SIDE: a profile's MCP server only *registers* the
# tools in its allowlist, so a Sulivan / remote mount literally never sees
# `organize` (or `create`, for Sulivan) in its tool list — it cannot be called,
# not merely hidden. Selected via the ENGRAM_PROFILE env var at startup,
# mirroring the env-driven ENGRAM_TRANSPORT idiom and the Hermes `profile`
# concept. Default (unset) = selene (the desktop full mount), preserving the
# pre-A3 behavior where every tool was exposed.
PROFILE_TOOLSETS: dict[str, set[str]] = {
    # Selene — the librarian: everything, including organize.
    "selene": {"recall", "search", "list_projects", "sync_status",
               "append", "create", "promote_fact", "organize"},
    # Sulivan — the daily driver: reads + append + promote_fact. NO organize, NO create.
    "sulivan": {"recall", "search", "list_projects", "sync_status",
                "append", "promote_fact"},
    # Remote Claude Code — reads + append/create/promote_fact. NO organize
    # (a remote machine must not trigger a 35B Selene swap on the home box).
    "remote": {"recall", "search", "list_projects", "sync_status",
               "append", "create", "promote_fact"},
}


def _active_profile() -> str:
    p = (os.environ.get("ENGRAM_PROFILE") or "selene").strip().lower()
    return p if p in PROFILE_TOOLSETS else "selene"


_ACTIVE_PROFILE = _active_profile()
_ALLOWED_TOOLS = PROFILE_TOOLSETS[_ACTIVE_PROFILE]


def profiled_tool(*dargs, **dkwargs):
    """Register an @mcp.tool ONLY if it's in the active profile's allowlist.

    Tools outside the profile are bound as plain functions (still importable /
    callable in-process, e.g. by the smoke test) but are NOT exposed to MCP
    clients — they never appear in the tool list of a scoped mount.
    """
    def decorate(fn):
        if fn.__name__ in _ALLOWED_TOOLS:
            return mcp.tool(*dargs, **dkwargs)(fn)
        return fn  # not registered with FastMCP for this profile
    # Support both @profiled_tool and @profiled_tool(annotations=...)
    if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
        fn, dargs = dargs[0], ()
        return decorate(fn)
    return decorate


# =====================================================================
#  READ TOOLS (readOnlyHint)
# =====================================================================

@profiled_tool(annotations={"readOnlyHint": True})
def recall(kind: str = "project", project: str | None = None,
           target: str | None = None, slug: str | None = None,
           max_chars: int = 8000, full: bool = False) -> dict[str, Any]:
    """Read Engram content by kind.

    kind: 'project' (needs project), 'global', 'facts', 'thought' (needs slug),
    'file' (needs Engram-relative .md target). Returns markdown text (UTF-8),
    truncated to max_chars. Never returns raw bytes beyond the requested md.

    full=True (kind project/global only): read the claude.md / CLAUDE.md directly,
    bypassing the 4000-char load_engram_context prompt-budget cap — the librarian's
    un-truncated read path (contract §11.1). max_chars still applies as the outer cap.
    """
    try:
        return client().recall(kind, project, target, slug,
                               max(200, min(int(max_chars), 200000)), bool(full))
    except EngramError as e:
        return _err(e)


@profiled_tool(annotations={"readOnlyHint": True})
def search(query: str, scope: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    """Substring/keyword search across operator facts, recent thoughts, project
    notes, and background knowledge (Career/ + Areas/ context files — the
    'knowledge' scope is paragraph-level and fuzzy-tolerant for STT drift).

    Terms >= 3 chars, case-insensitive, newest-first. scope defaults to all
    four: ['facts','thoughts','projects','knowledge'].
    """
    try:
        return client().search(
            query, scope or ["facts", "thoughts", "projects", "knowledge"],
            max(1, min(int(limit), 100)))
    except EngramError as e:
        return _err(e)


@profiled_tool(annotations={"readOnlyHint": True})
def list_projects() -> dict[str, Any]:
    """Enumerate mountable project contexts (dirs under Projects/ with a claude.md)."""
    try:
        return client().list_projects()
    except EngramError as e:
        return _err(e)


@profiled_tool(annotations={"readOnlyHint": True})
def sync_status(activity_hours: int = 24) -> dict[str, Any]:
    """Report shard health: source-of-truth host, reachability, write-queue depth,
    last consolidation, and recent activity. The 'am I mounted to the live brain?' probe.
    """
    try:
        return client().sync_status(max(1, min(int(activity_hours), 720)))
    except EngramError as e:
        return _err(e)


# =====================================================================
#  WRITE TOOLS (mutating → in-process write lock)
# =====================================================================

@profiled_tool
async def append(target: str, content: str, heading: str | None = None,
                 mode: str = "append") -> dict[str, Any]:
    """Append content to an Engram .md file, optionally under a '## heading' (safe default).

    target is Engram-relative and .md. With heading: inserts under it (created if
    absent). Without heading: mode 'append' (end-of-file) or 'overwrite' (guarded
    — overwrite of CLAUDE.md or any _index.md is forbidden).
    """
    try:
        return await client().append(target, content, heading, mode)
    except EngramError as e:
        return _err(e)


@profiled_tool
async def create(target: str, content: str, make_parents: bool = True,
                 index_entry: dict | None = None) -> dict[str, Any]:
    """Create a new Engram .md file that does NOT already exist (thought, review, note).

    make_parents creates intermediate dirs under the root. Optional index_entry
    {target, heading, line} also appends a row/bullet to an index file via the
    structured path (never a raw overwrite).
    """
    try:
        return await client().create(target, content, make_parents, index_entry)
    except EngramError as e:
        return _err(e)


@profiled_tool
async def promote_fact(text: str, source: str = "engram-mcp") -> dict[str, Any]:
    """Add a durable operator fact (dated bullet under operator-facts.md → Facts, 1000-char cap)."""
    try:
        return await client().promote_fact(text, source)
    except EngramError as e:
        return _err(e)


@profiled_tool
async def organize(scope: str = "daily", days: int = 1, hours: int = 24,
                   dry_run: bool = False, target_override: str | None = None) -> dict[str, Any]:
    """Run a Selene consolidation: gather a window of activity, synthesize as
    profile=selene via the Hermes gateway, and write a structured digest + index entry.

    dry_run returns the digest WITHOUT writing. Preserves the 'never write an
    empty digest' guard. Selene/librarian tool — kept off remote profiles by default.
    """
    try:
        return await client().organize(scope, max(1, min(int(days), 30)),
                                       max(1, min(int(hours), 720)), bool(dry_run), target_override)
    except EngramError as e:
        return _err(e)


# =====================================================================
#  /engram/* HTTP routes (for the http-transport client + Hermes adapter A5)
# =====================================================================

def _build_http_app():
    """A small Starlette app exposing /engram/<tool> so a remote http-transport
    EngramClient (and the future Hermes plugin) can call the same logic. Mounted
    alongside the MCP http surface."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _read(request):
        tool = request.path_params["tool"]
        if tool not in _ALLOWED_TOOLS:
            return JSONResponse({"ok": False, "error": "invalid_args",
                                 "detail": f"tool {tool!r} not enabled for profile {_ACTIVE_PROFILE!r}"}, 403)
        raw = request.query_params.get("json", "{}")
        try:
            p = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid_args", "detail": "bad json"}, 400)
        c = client()
        try:
            if tool == "recall":
                return JSONResponse(c.recall(p.get("kind", "project"), p.get("project"),
                                             p.get("target"), p.get("slug"), int(p.get("max_chars", 8000)),
                                             bool(p.get("full", False))))
            if tool == "search":
                return JSONResponse(c.search(p.get("query", ""),
                                             p.get("scope") or ["facts", "thoughts", "projects"],
                                             int(p.get("limit", 20))))
            if tool == "list_projects":
                return JSONResponse(c.list_projects())
            if tool == "sync_status":
                return JSONResponse(c.sync_status(int(p.get("activity_hours", 24))))
        except EngramError as e:
            return JSONResponse(_err(e), 200)
        return JSONResponse({"ok": False, "error": "invalid_args", "detail": f"unknown read tool {tool}"}, 404)

    async def _write(request):
        tool = request.path_params["tool"]
        if tool not in _ALLOWED_TOOLS:
            return JSONResponse({"ok": False, "error": "invalid_args",
                                 "detail": f"tool {tool!r} not enabled for profile {_ACTIVE_PROFILE!r}"}, 403)
        p = await request.json()
        c = client()
        try:
            if tool == "append":
                return JSONResponse(await c.append(p["target"], p["content"],
                                                   p.get("heading"), p.get("mode", "append")))
            if tool == "create":
                return JSONResponse(await c.create(p["target"], p["content"],
                                                   p.get("make_parents", True), p.get("index_entry")))
            if tool == "promote_fact":
                return JSONResponse(await c.promote_fact(p["text"], p.get("source", "engram-mcp")))
            if tool == "organize":
                return JSONResponse(await c.organize(p.get("scope", "daily"), int(p.get("days", 1)),
                                                     int(p.get("hours", 24)), bool(p.get("dry_run", False)),
                                                     p.get("target_override")))
        except EngramError as e:
            return JSONResponse(_err(e), 200)
        except KeyError as e:
            return JSONResponse({"ok": False, "error": "invalid_args", "detail": f"missing {e}"}, 400)
        return JSONResponse({"ok": False, "error": "invalid_args", "detail": f"unknown write tool {tool}"}, 404)

    return Starlette(routes=[
        Route("/engram/{tool}", _read, methods=["GET"]),
        Route("/engram/{tool}", _write, methods=["POST"]),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="engram-mcp server")
    parser.add_argument(
        "--transport",
        default=os.environ.get("ENGRAM_MCP_TRANSPORT_MODE", "http"),
        choices=["http", "stdio"],
        help="MCP transport (default http; the local Claude Code plugin uses stdio).",
    )
    args, _ = parser.parse_known_args()

    print(f"[engram-mcp] profile={_ACTIVE_PROFILE} "
          f"tools={sorted(_ALLOWED_TOOLS)}", flush=True)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    host = os.environ.get("ENGRAM_MCP_HOST", "0.0.0.0")  # LAN bind (R-A: MacBook reach)
    port = int(os.environ.get("ENGRAM_MCP_PORT", "8765"))
    # Serve the /engram/* REST routes (http-transport client + Hermes A5) and the
    # MCP-over-http surface from one process. Run the REST app on the configured
    # port; the MCP http app is reachable under its own path.
    try:
        import uvicorn
    except ImportError:
        # No uvicorn: fall back to FastMCP's own http runner (MCP surface only).
        mcp.run(transport="http", host=host, port=port)
        return

    rest_app = _build_http_app()
    uvicorn.run(rest_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
