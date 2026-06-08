"""engram-organize — the nightly consolidation entry point (W23 A3, piece 1).

ONE consolidation code path. The nightly `selene-review.timer` (via
`selene_review_boot.sh`) and any on-demand `organize` MCP call now run the SAME
logic: `EngramClient.organize` (transport=local), which is
`selene_review.gather_material -> call_selene(profile=selene) -> write_digest`.
This wrapper exists only so the systemd boot script has one stable thing to
invoke; it does NOT duplicate the consolidation logic — it forwards to the live
service method and maps the result onto selene_review.py's original exit codes
so the timer/journald semantics are unchanged:

    0  ok (digest written, or dry-run synthesized)
    2  engram_unavailable   (was selene_review exit 2)
    3  gateway_unreachable  (Hermes :8770 down — was exit 3)
    4  empty_digest         (model returned nothing; never writes — was exit 4)
    1  any other failure

The "never write an empty digest" guard is preserved inside
`EngramClient.organize` (it raises `empty_digest` before writing).

Run:  python -m engram_mcp.organize_cli --once
  or  engram-organize --once
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from engram_mcp.client import EngramClient, EngramError

# Map the closed §5 error codes back onto selene_review.py's exit codes so the
# nightly timer's success/failure semantics (and journald) are unchanged.
_EXIT_FOR_CODE = {
    "engram_unavailable": 2,
    "gateway_unreachable": 3,
    "empty_digest": 4,
}


async def _run(scope: str, days: int, hours: int, dry_run: bool,
               target_override: str | None) -> int:
    # transport=local: this is the desktop FS owner; the SAME path on-demand
    # organize uses. No duplication of gather/synthesize/write logic.
    client = EngramClient(transport="local")
    try:
        result = await client.organize(scope, days, hours, dry_run, target_override)
    except EngramError as e:
        print(f"[engram-organize] FAIL: {e.code}: {e.detail}", file=sys.stderr)
        return _EXIT_FOR_CODE.get(e.code, 1)

    if not result.get("ok"):
        code = result.get("error", "unknown")
        print(f"[engram-organize] FAIL: {code}: {result.get('detail', '')}", file=sys.stderr)
        return _EXIT_FOR_CODE.get(code, 1)

    written = result.get("written_to")
    counts = result.get("counts", {})
    if dry_run:
        print(f"[engram-organize] dry-run ok: {len(result.get('digest', ''))} digest chars "
              f"(facts={counts.get('facts')}, thoughts={counts.get('thoughts')}, "
              f"projects={counts.get('projects')})")
    else:
        print(f"[engram-organize] wrote Engram/{written} "
              f"({len(result.get('digest', ''))} digest chars; index_updated="
              f"{result.get('index_updated')})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the nightly Selene Engram consolidation "
                                                 "through the engram-mcp organize tool.")
    parser.add_argument("--once", action="store_true",
                        help="explicit single run (default; for the systemd timer / manual use)")
    parser.add_argument("--days", type=int, default=1, help="thoughts look-back (days)")
    parser.add_argument("--hours", type=int, default=24, help="project-activity look-back (hours)")
    parser.add_argument("--scope", default="daily", choices=["daily", "window"])
    parser.add_argument("--dry-run", action="store_true",
                        help="synthesize + report the digest WITHOUT writing it")
    parser.add_argument("--target-override", default=None,
                        help="optional Engram-relative .md target (defaults to Reviews/daily/<date>.md)")
    args = parser.parse_args()

    return asyncio.run(_run(args.scope, max(1, args.days), max(1, args.hours),
                            args.dry_run, args.target_override))


if __name__ == "__main__":
    sys.exit(main())
