# engram-mcp

Mount the Engram second brain as an MCP shard. A **standalone always-on Python service**
(contract §11/§11.1 F1) that wraps the existing Valinor Engram seams
(`Server/tools/engram_writer.py`, `Server/tools/brain_sync.py`, `scripts/selene_review.py`)
**in-process** — no rewrite — and exposes them as MCP tools over **stdio** (local Claude Code
plugin) and **http** (LAN — the MacBook and, later, a Hermes adapter). **Rust is untouched**:
there are no `/engram/*` routes on the Rust supervisor.

## Tools (contract §2)

| Tool | Mutates | Wraps |
|---|---|---|
| `recall` | no (readOnlyHint) | `brain_sync.load_engram_context`, project/thought reads, `load_operator_facts` |
| `search` | no | `brain_sync.search_operator_facts` + thoughts/projects scan |
| `list_projects` | no | `brain_sync._list_projects` |
| `sync_status` | no | `recent_project_activity` + `list_recent_thoughts` + source-of-truth identity |
| `append` | yes | `engram_writer.append_under_heading` / `write_engram_file` |
| `create` | yes | `write_engram_file` + structured index append (`_update_thoughts_index` shape) |
| `promote_fact` | yes | `brain_sync.add_operator_fact` |
| `organize` | yes | `selene_review.gather_material → call_selene(profile=selene) → write_digest` |

All mutating tools funnel through an **in-process async write lock** (one process owns the FS,
contract §6/F2 — no Rust queue). Path-safety (§3) and the protected-file guard (§3.7: no raw
`overwrite` of root `CLAUDE.md` or any `_index.md`) are enforced **server-side**. Errors use the
closed §5 code set.

## Per-persona tool-scoping (contract §4 matrix · W23 A3)

Tool exposure is **scoped per persona, server-side**, via the `ENGRAM_PROFILE` env var read at
startup (mirrors the env-driven `ENGRAM_TRANSPORT` idiom and the Hermes `profile` concept). The
server only **registers** the tools in the active profile's allowlist, so a scoped mount never sees
the omitted tools in its tool list — they cannot be called, not merely hidden. The http surface
enforces the same allowlist (a 403 for an out-of-profile tool), so the LAN/Hermes path is scoped too.

| Profile (`ENGRAM_PROFILE`) | recall | search | list_projects | sync_status | append | create | promote_fact | organize |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| `selene` (default / desktop full mount) | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | **✔** |
| `sulivan` (daily driver) | ✔ | ✔ | ✔ | ✔ | ✔ | – | ✔ | – |
| `remote` (Claude Code off-box) | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | – |

`organize` is exposed **only** to `selene` — a remote machine can't trigger a 35B Selene swap on the
home box (§4.3, R-A3). `create` is additionally withheld from `sulivan` (append-only daily driver).
Default (unset) = `selene`, preserving the pre-A3 full surface. Mount files: `.mcp.json` (selene,
local), `.mcp.sulivan.json`, `.mcp.remote.json`.

## One consolidation path (the nightly timer · W23 A3)

The nightly `selene-review.timer` no longer runs `selene_review.py` directly. `selene_review_boot.sh`
now invokes `python -m engram_mcp.organize_cli --once`, which forwards to **the same**
`EngramClient.organize` that on-demand `organize` calls — one code path for both. `organize_cli` maps
the result back onto selene_review's exit codes (0/2/3/4) so the timer/journald semantics are
unchanged, and the "never write an empty digest" guard (inside `organize`) is preserved. The boot
script falls back to the standalone script if the engram-mcp checkout is absent; `selene_review.py`
itself is unchanged (it remains the seam `organize` imports).

## Un-truncated read path (contract §11.1 · W23 A3)

`recall` gained a `full: bool = False` param. For `kind` `project`/`global`, `full=True` reads the
`claude.md` / root `CLAUDE.md` **directly from disk**, bypassing `brain_sync.load_engram_context`'s
4000-char (`ENGRAM_CONTEXT_MAX_CHARS`) prompt-budget cap — the librarian's un-truncated read. The
`brain_sync` seam is untouched (the `brain_sync` truncation still governs the normal turn-time
context-load path). `max_chars` still applies as the outer cap.

## Transports

- `--transport stdio` — spawned per-client by a Claude Code plugin `.mcp.json`.
- `--transport http`  — one shared long-running LAN service; serves `/engram/<tool>` REST routes
  for remote `http`-transport clients and the future Hermes adapter (task A5). Binds `0.0.0.0:8765`
  by default (`ENGRAM_MCP_HOST` / `ENGRAM_MCP_PORT`).

The client-side `transport` switch (env `ENGRAM_TRANSPORT`, mirrors comfy's `direct`|`rust`):

- `local` — import the Valinor seams in-process. The desktop service and co-located dev use this.
- `http`  — forward to a remote `engram-mcp` service over the LAN. Remote machines use this.

## Deployment decision (the one to resolve)

**The service runs as a WSL systemd user service on the desktop**, sibling to `hermes-gateway`
and `sulivan-bot`, in `ENGRAM_TRANSPORT=local`, accessing Engram via the WSL mount
`/mnt/d/Tools/personalAI/Engram`, exposing http on `0.0.0.0:8765` to the LAN (same WSL-networking
fix the echo-show report documents: mirrored-networking / `netsh portproxy` / `0.0.0.0` bind).
Rationale: it sits beside the other always-warm services on the single FS-owner host, reuses the
proven LAN-reach pattern, and keeps one canonical Engram root (R-A5 — verified to resolve to
`D:\Tools\personalAI\Engram`, which WSL sees through the mount; `sync_status` reports it as
`engram_root` and the host as `source_of_truth`).

- **Local co-located Claude Code** mounts via stdio with `ENGRAM_TRANSPORT=local` (in-process, no
  network) — this repo's `.mcp.json`.
- **Remote machines (MacBook)** mount via stdio with `ENGRAM_TRANSPORT=http` +
  `ENGRAM_BASE_URL=http://<home-LAN-IP>:8765`.

Note on Windows-vs-WSL: the seams resolve the same canonical root from either side
(`D:/...` on Windows, `/mnt/d/...` in WSL) — `config.resolve_engram_root()` and the seam root are
asserted equal at startup, and divergence is logged (R-A5).

## Remote `.mcp.json` (MacBook, http transport)

```json
{
  "mcpServers": {
    "engram": {
      "command": "python",
      "args": ["-m", "engram_mcp.server", "--transport", "stdio"],
      "env": {
        "ENGRAM_TRANSPORT": "http",
        "ENGRAM_BASE_URL": "http://<home-LAN-IP>:8765"
      }
    }
  }
}
```

## Offline (contract §7-Q2) — structured for, not built

`engram_mcp/outbox.py` is a thin journal interface (cache dir + append-only journal stub) for the
deferred offline read-cache + write-outbox replay. **W23 P0 needs only the online case**; full
reconciliation is deferred (R-A6) and not wired into the live tools.

## Health check

`python smoke_test.py` is a **read-only** health check (11/11) — it confirms a mount is alive and the
read surface works (`recall`/`search`/`list_projects`/`sync_status` + server-side `bad_path` rejection
on reads) and **writes nothing** to the real Engram. Safe to run anytime, on any machine.

Full acceptance of the write tools (the in-process write-lock serializing two concurrent appends, and
the `overwrite_forbidden` / `already_exists` / `empty` guards) plus the http and MCP-protocol stdio
round-trips were validated once at build time. If you change the write path later, write a
**sandboxed** write test (client pointed at a throwaway temp Engram root) — never exercise writes
against the real brain.
