"""SQLite FTS5 index over the Engram (the retrieval substrate, 2026-06-07).

BM25 with corpus-level IDF replaces the hand-tuned lexical scorer for content
search: common query words ("worked", "top", "time") are downweighted by the
corpus itself, rare content terms ("Meta", "Sulivan") dominate -- the failure
class where resume-discussion noise outranked the actual Meta bullets dies
here, without stopword whack-a-mole.

Paragraph-granularity rows over the WHOLE Engram markdown tree (including
Thoughts BODIES, never searchable before), incrementally refreshed by file
mtime at query time. The DB lives at <Engram>/state/engram-fts.sqlite --
local-only, derived, safe to delete (rebuilds on next query).

Stdlib only (sqlite3 with FTS5, present on both the Windows and WSL pythons).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Trees indexed (Engram-relative). Root *.md catches CLAUDE.md/operator-facts.
_INDEX_GLOBS = (
    "Career/**/*.md",
    "Areas/**/*.md",
    "Projects/**/*.md",
    "Thoughts/**/*.md",
    "Reviews/**/*.md",
    "Research/**/*.md",
    "Ideas/**/*.md",
    "*.md",
)
_EXCLUDE_PARTS = {".git", "node_modules", ".fennec"}
_MAX_PARA_CHARS = 2000


# Bump to force a clean reindex when the row-splitting scheme changes.
_SCHEMA_VERSION = 7


def _db_path(root: Path) -> Path:
    state = root / "state"
    state.mkdir(exist_ok=True)
    return state / "engram-fts.sqlite"


def _connect(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path(root)))
    (ver,) = con.execute("PRAGMA user_version").fetchone()
    if ver != _SCHEMA_VERSION:
        con.execute("DROP TABLE IF EXISTS files")
        con.execute("DROP TABLE IF EXISTS paras")
        con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    con.execute(
        "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL)"
    )
    # title = the row's nearest heading (field-weighted 3x in ranking: a
    # heading match beats a body match, scaled by IDF -- standard IR field
    # weighting); body = the paragraph/bullet text itself.
    # porter stemming: "work" must match "worked", "projects" ~ "project" --
    # voice-phrased queries never match the notes' exact inflections.
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS paras "
        "USING fts5(path UNINDEXED, title, body, tokenize='porter unicode61')"
    )
    return con


_BULLET_RE = re.compile(r"\s*[-*+] ")


def _split_rows(text: str) -> list[tuple[str, str]]:
    """Markdown-aware (title, body) rows: blank-line paragraphs, except a
    bullet LIST splits into one row per bullet -- a resume-bullets file is
    otherwise one giant paragraph whose FTS snippet shows a 40-token slice of
    the first bullet. Each row carries its nearest preceding heading as the
    TITLE field (field-weighted in ranking): the bullets themselves never name
    the employer, so without heading context a "Meta" query ranks them last.
    Bare heading lines are not their own rows (a 6-word heading with one match
    is a BM25 density trap that outranked real content)."""
    rows: list[tuple[str, str]] = []
    heading = ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        for ln in lines:
            if ln.lstrip().startswith("#"):
                heading = ln.lstrip("# ").strip()
        bullet_lines = sum(1 for ln in lines if _BULLET_RE.match(ln))
        if bullet_lines >= 3:
            # One row per WHOLE bullet: continuation lines (wrapped text not
            # starting with a marker) join their bullet -- splitting per
            # visual line shredded multi-line bullets into sentence shards
            # ("work in this role." as its own row) and diluted ranking.
            current = ""
            for ln in lines:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                if _BULLET_RE.match(ln):
                    if current:
                        rows.append((heading, current))
                    current = s
                else:
                    current = f"{current} {s}".strip()
            if current:
                rows.append((heading, current))
        else:
            body_lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
            body = "\n".join(body_lines).strip()
            if body:
                rows.append((heading, body))
    return rows


def _walk(root: Path):
    seen: set[str] = set()
    for pattern in _INDEX_GLOBS:
        for f in root.glob(pattern):
            if not f.is_file():
                continue
            if any(p in _EXCLUDE_PARTS for p in f.parts):
                continue
            rel = f.relative_to(root).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            yield rel, f


def refresh(root: Path) -> int:
    """Incremental reindex by file mtime; prunes deleted files. Returns the
    number of files (re)indexed or pruned."""
    con = _connect(root)
    try:
        known = dict(con.execute("SELECT path, mtime FROM files"))
        changed = 0
        live: set[str] = set()
        for rel, f in _walk(root):
            live.add(rel)
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if known.get(rel) == mtime:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            con.execute("DELETE FROM paras WHERE path = ?", (rel,))
            rows = [
                (rel, title[:200], body[:_MAX_PARA_CHARS])
                for title, body in _split_rows(text)
            ]
            con.executemany(
                "INSERT INTO paras (path, title, body) VALUES (?, ?, ?)", rows
            )
            con.execute(
                "INSERT INTO files (path, mtime) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime",
                (rel, mtime),
            )
            changed += 1
        for gone in set(known) - live:
            con.execute("DELETE FROM paras WHERE path = ?", (gone,))
            con.execute("DELETE FROM files WHERE path = ?", (gone,))
            changed += 1
        con.commit()
        return changed
    finally:
        con.close()


def search(
    root: Path,
    query: str,
    path_prefixes: tuple[str, ...] | None = None,
    limit: int = 10,
) -> list[dict]:
    """BM25 paragraph search. Returns [{source, snippet, rank}] best-first
    (rank: smaller/more negative = better). Empty on any failure -- callers
    fall back to the lexical scan (which also covers fuzzy STT drift)."""
    terms = re.findall(r"[A-Za-z0-9]+", query or "")
    if not terms:
        return []
    try:
        n = refresh(root)
        if n:
            logger.info("engram-fts: reindexed %d file(s)", n)
        con = _connect(root)
    except Exception as exc:  # noqa: BLE001 - the index is an accelerator, not a dependency
        logger.warning("engram-fts unavailable: %s", exc)
        return []
    try:
        match = " OR ".join(f'"{t}"' for t in terms)
        # Field weighting: path 0 (unindexed anyway), title 3x, body 1x — a
        # heading naming the query subject beats a body word-match, IDF-scaled.
        sql = (
            "SELECT path, title, snippet(paras, 2, '', '', '...', 40), "
            "bm25(paras, 0.0, 3.0, 1.0) AS rank "
            "FROM paras WHERE paras MATCH ?"
        )
        args: list = [match]
        if path_prefixes:
            sql += " AND (" + " OR ".join("path LIKE ?" for _ in path_prefixes) + ")"
            args.extend(p + "%" for p in path_prefixes)
        sql += " ORDER BY rank LIMIT ?"
        args.append(int(limit))
        out = []
        for path, title, snip, rank in con.execute(sql, args):
            text = f"{title} :: {snip}" if title else (snip or "")
            out.append(
                {"source": path, "snippet": text[:280], "rank": float(rank)}
            )
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("engram-fts query failed: %s", exc)
        return []
    finally:
        con.close()
