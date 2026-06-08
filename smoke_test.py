"""engram-mcp READ-ONLY health check.

Confirms a mount is alive and the read surface works WITHOUT writing anything to
the real Engram. Acceptance of the write tools (append/create/promote_fact/
organize) was done once at build time; this probe is the ongoing "did my mount
break?" check and is safe to run anytime, on any machine, against the live brain.

It writes NOTHING. The only "rejection" checks call read tools with bad paths and
assert they're refused server-side — no file is ever created or modified.

If you change engram-mcp's write path later, write a SANDBOXED write test then
(point the client at a throwaway temp Engram root) — never exercise writes
against the real brain.

Run:  python smoke_test.py        (uses ENGRAM_TRANSPORT, default local)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engram_mcp.client import EngramClient, EngramError  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    c = EngramClient(transport="local")
    root = c.engram_root
    print(f"engram_root = {root}")
    record("engram_root resolves", root is not None and Path(root).exists(), str(root))

    # ---- reads only ----
    lp = c.list_projects()
    record("list_projects", lp.get("ok") and len(lp.get("projects", [])) > 0,
           f"{len(lp.get('projects', []))} projects")

    g = c.recall("global", None, None, None, 8000)
    record("recall(global)", g.get("ok") and g.get("source") == "CLAUDE.md", f"{g.get('chars')} chars")

    pj = lp.get("projects", [{}])[0].get("name", "valinor")
    rp = c.recall("project", pj, None, None, 8000)
    record("recall(project)", rp.get("ok"), f"{pj}: {rp.get('chars')} chars")

    f = c.recall("facts", None, None, None, 8000)
    record("recall(facts)", f.get("ok"), f"{f.get('chars')} chars")

    s = c.search("valinor", ["facts", "thoughts", "projects"], 20)
    record("search", s.get("ok"), f"{len(s.get('results', []))} hits")

    ss = c.sync_status(24)
    record("sync_status", ss.get("ok") and ss.get("reachable"),
           f"sot={ss.get('source_of_truth')} root={ss.get('engram_root')} qdepth={ss.get('write_queue_depth')}")

    # ---- path-safety on reads (no write happens; the call is refused) ----
    for bad in ["../escape.md", "/abs/path.md", "notes.txt", "Projects/../../x.md"]:
        try:
            c.recall("file", None, bad, None, 8000)
            record(f"bad_path rejects {bad!r}", False, "NOT rejected!")
        except EngramError as e:
            record(f"bad_path rejects {bad!r}", e.code == "bad_path", e.code)

    print("\n=== SUMMARY (read-only) ===")
    npass = sum(1 for _, v, _ in results if v == PASS)
    for name, verdict, detail in results:
        print(f"  {verdict}  {name}")
    print(f"{npass}/{len(results)} passed — nothing written to Engram")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
