"""Offline outbox journal — STRUCTURED FOR, not built (contract §7-Q2 / R-A6).

Policy (contract §7-Q2): the desktop supervisor is the sole source of truth.
When it is unreachable and the user is mobile (MacBook), engram-mcp should:
  - serve reads from a read-only local cache (``cache_dir``), stamped stale;
  - record writes to an append-only, ordered, timestamped outbox journal and
    return ``deferred: true`` rather than committing to any canonical file;
  - on reconnect, REPLAY the outbox through the in-process write lock in order
    (additive ops are conflict-free; overwrite carries base_hash).

W23 P0 only requires the online case, so this module is a thin journal
INTERFACE plus an enqueue stub. Full reconciliation/replay is deferred (R-A6);
if it starts to balloon, flag and stop. Nothing here is wired into the live
tools yet — it exists so the cache dir + journal shape are settled.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from engram_mcp import config as cfg


def _journal_path() -> Path:
    d = Path(cfg.cache_dir())
    d.mkdir(parents=True, exist_ok=True)
    return d / "outbox.jsonl"


def enqueue(tool: str, payload: dict[str, Any]) -> str:
    """Append a deferred write to the journal; return its correlation id.

    NOTE: not called by the live tools in W23 — present so the offline path has
    a settled shape. The online path (transport=local/http reachable) never
    touches this.
    """
    entry = {
        "ts": time.time(),
        "tool": tool,
        "payload": payload,
        "applied": False,
    }
    with open(_journal_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return f"outbox-{int(entry['ts'] * 1000)}"


def pending() -> list[dict[str, Any]]:
    """Return unapplied journal entries (for a future replay pass). Deferred."""
    path = _journal_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not entry.get("applied"):
            out.append(entry)
    return out


def replay(client) -> int:  # pragma: no cover - DEFERRED (R-A6)
    """Replay pending writes through the supervisor write lock, in order.

    DEFERRED to a follow-on: full reconciliation (mark-applied, overwrite
    base_hash conflict handling) is out of W23 scope. Raises to make the
    deferral explicit if something calls it prematurely.
    """
    raise NotImplementedError("offline outbox replay is deferred (contract §7-Q2 / R-A6).")
