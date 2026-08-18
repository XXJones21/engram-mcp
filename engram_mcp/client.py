"""Engram client for engram-mcp — the transport switch + the write lock.

Two transports (mirroring comfy-local-mcp's ``direct`` | ``rust``):

  - ``local``  : import the existing Valinor Python seams in-process and call
                 them directly. This is the standalone always-on service path
                 (contract §11/§11.1 F1) AND the co-located dev path. The
                 process that runs in ``local`` is THE single owner of the
                 Engram filesystem, so an in-process async lock linearizes all
                 writes (§6 / F2) — no Rust queue, no tickets.
  - ``http``   : call a remote engram-mcp http service over the LAN (the
                 MacBook / Hermes-adapter case). The remote service runs in
                 ``local`` and owns the FS; this side just forwards.

Path-safety (§3) is enforced SERVER-SIDE here, in the ``local`` path, because
MCP clients are untrusted. We re-run the same validation engram_writer already
does, PLUS the contract-level protected-file guard (§3.7): no raw ``overwrite``
of the root ``CLAUDE.md`` or any ``_index.md``.

Error reasons are the closed set from contract §5.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from engram_mcp import config as cfg

logger = logging.getLogger(__name__)

# ---- closed error-code set (contract §5) --------------------------------
ERROR_CODES = {
    "engram_unavailable",
    "supervisor_unreachable",
    "not_found",
    "bad_path",
    "parent_missing",
    "already_exists",
    "file_not_found",
    "overwrite_forbidden",
    "write_conflict",
    "empty",
    "empty_query",
    "empty_digest",
    "gateway_unreachable",
    "invalid_kind",
    "invalid_args",
}


class EngramError(RuntimeError):
    """A tool failure carrying one of the closed §5 reason codes."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code if code in ERROR_CODES else "invalid_args"
        self.detail = detail or code


# ---- in-process single-writer lock (§6 / F2) ----------------------------
# One process owns the FS; this serializes every mutating tool. Reads do not
# take it (served concurrently).
_WRITE_LOCK = asyncio.Lock()

# Query filler dropped before scoring (voice-phrased queries: "what did I work
# on at Meta" should score on [work, meta], not on "what"/"did"). Deliberately
# small — content-bearing short words (time, word, work) stay. The durable fix
# for common-word noise is the FTS5/BM25 index (IDF); this is the interim.
_SEARCH_STOPWORDS = {
    "the", "and", "for", "with", "about", "that", "this", "what", "did",
    "was", "were", "while", "when", "where", "who", "how", "can", "could",
    "you", "your", "yours", "some", "things", "tell", "know", "they",
    "them", "their", "have", "has", "had", "are", "not", "all", "any",
}


def _import_seams(valinor_root: Path):
    """Put the Valinor checkout (and scripts/) on sys.path and import the seams.

    Importing — never rewriting — engram_writer / brain_sync / selene_review.
    """
    root = str(valinor_root)
    scripts = str(valinor_root / "scripts")
    if root not in sys.path:
        sys.path.insert(0, root)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from Server.tools import engram_writer as ew  # noqa: E402
    from Server.tools import brain_sync as bs  # noqa: E402
    import selene_review as sr  # noqa: E402
    return ew, bs, sr


class EngramClient:
    """In-process or LAN client over the Engram seams."""

    def __init__(self, transport: str | None = None, base_url: str | None = None):
        self.transport = cfg.resolve_transport(transport)
        self.base_url = cfg.resolve_base_url(base_url)
        self._seams = None  # lazy (ew, bs, sr)
        self._engram_root: Path | None = None

    # ---- seam / root resolution -------------------------------------

    def _ensure_local(self):
        if self._seams is not None:
            return self._seams
        valinor_root = cfg.resolve_valinor_root()
        if valinor_root is None:
            raise EngramError(
                "engram_unavailable",
                "Valinor checkout (Server/tools/engram_writer.py) not found; "
                "set valinor_root in config or ENGRAM_VALINOR_ROOT.",
            )
        self._seams = _import_seams(valinor_root)
        ew, _bs, _sr = self._seams
        # R-A5: assert the seams resolve to ONE canonical root, the one we report.
        seam_root = ew.ENGRAM_ROOT
        cfg_root = cfg.resolve_engram_root()
        if seam_root is None:
            raise EngramError("engram_unavailable", "engram_writer could not resolve an Engram root.")
        if cfg_root is not None and Path(seam_root).resolve() != Path(cfg_root).resolve():
            logger.warning(
                "Engram-root divergence (R-A5): seam=%s config=%s — using the seam root.",
                seam_root, cfg_root,
            )
        self._engram_root = Path(seam_root).resolve()
        return self._seams

    @property
    def engram_root(self) -> Path | None:
        try:
            self._ensure_local()
        except EngramError:
            return cfg.resolve_engram_root()
        return self._engram_root

    # ---- server-side path-safety (§3) -------------------------------

    @staticmethod
    def _is_protected(target: str) -> bool:
        """Contract §3.7 protected set: root CLAUDE.md and any _index.md."""
        norm = target.replace("\\", "/").strip("/")
        name = norm.rsplit("/", 1)[-1]
        if norm == "CLAUDE.md":
            return True
        if name == "_index.md":
            return True
        return False

    def validate_path(self, target: str, *, for_write: bool) -> Path:
        """Re-run §3 server-side. Raises EngramError(bad_path) on any rejection.

        Mirrors engram_writer._validate_engram_path (rules 1-4) but we own the
        error mapping so untrusted clients can't bypass it. parent_missing is
        raised distinctly (rule 5) for writes that need an existing dir.
        """
        ew, _bs, _sr = self._ensure_local()
        root = self._engram_root
        if root is None:
            raise EngramError("engram_unavailable", "Engram root not resolved.")
        t = (target or "").replace("\\", "/")
        if not t or t.startswith("/") or (len(t) > 1 and t[1] == ":"):
            raise EngramError("bad_path", f"target must be relative to the Engram root: {target!r}")
        if ".." in t:
            raise EngramError("bad_path", f"path traversal not allowed: {target!r}")
        if not t.endswith(".md"):
            raise EngramError("bad_path", f"only .md files are addressable: {target!r}")
        resolved = (root / t).resolve()
        if root not in resolved.parents and resolved != root:
            raise EngramError("bad_path", f"path must resolve under the Engram root: {target!r}")
        return resolved

    # =================================================================
    #  READS (no lock)
    # =================================================================

    def recall(self, kind: str, project: str | None, target: str | None,
               slug: str | None, max_chars: int, full: bool = False) -> dict[str, Any]:
        if self.transport == "http":
            return self._http_get("recall", {
                "kind": kind, "project": project, "target": target,
                "slug": slug, "max_chars": max_chars, "full": full,
            })
        ew, bs, _sr = self._ensure_local()
        root = self.engram_root
        if root is None:
            raise EngramError("engram_unavailable", "Engram root not found.")

        if kind == "project":
            if not project:
                raise EngramError("invalid_args", "kind=project requires 'project'.")
            if full:
                # Un-truncated read path (contract §11.1): read the project's
                # claude.md DIRECTLY, bypassing load_engram_context's 4000-char
                # cap (ENGRAM_CONTEXT_MAX_CHARS). The brain_sync seam is untouched
                # — this is a separate, root-confined disk read for Selene.
                ppath = root / "Projects" / project / "claude.md"
                if not ppath.exists():
                    raise EngramError("not_found", f"project context not found: {project}")
                content = ppath.read_text(encoding="utf-8")
            else:
                content = bs.load_engram_context(project)
                if not content:
                    raise EngramError("not_found", f"project context not found: {project}")
            source = f"Projects/{project}/claude.md"
        elif kind == "global":
            if full:
                gpath = root / "CLAUDE.md"
                if not gpath.exists():
                    raise EngramError("not_found", "global CLAUDE.md not found.")
                content = gpath.read_text(encoding="utf-8")
            else:
                content = bs.load_engram_context(None)
                if not content:
                    raise EngramError("not_found", "global CLAUDE.md not found.")
            source = "CLAUDE.md"
        elif kind == "facts":
            content = bs.load_operator_facts(limit=bs.OPERATOR_FACTS_MAX)
            source = bs.OPERATOR_FACTS_TARGET
        elif kind == "thought":
            if not slug:
                raise EngramError("invalid_args", "kind=thought requires 'slug'.")
            tpath = root / "Thoughts" / slug / "claude.md"
            if not tpath.exists():
                raise EngramError("not_found", f"thought not found: {slug}")
            content = tpath.read_text(encoding="utf-8")
            source = f"Thoughts/{slug}/claude.md"
        elif kind == "file":
            if not target:
                raise EngramError("invalid_args", "kind=file requires 'target'.")
            resolved = self.validate_path(target, for_write=False)  # bad_path on escape/non-.md
            if not resolved.exists():
                raise EngramError("not_found", f"file not found: {target}")
            content = resolved.read_text(encoding="utf-8")
            source = target.replace("\\", "/")
        else:
            raise EngramError("invalid_kind", f"unknown kind: {kind!r}")

        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
        return {
            "ok": True, "kind": kind, "source": source,
            "content": content, "chars": len(content), "truncated": truncated,
        }

    def search(self, query: str, scope: list[str], limit: int) -> dict[str, Any]:
        if self.transport == "http":
            return self._http_get("search", {"query": query, "scope": scope, "limit": limit})
        ew, bs, _sr = self._ensure_local()
        root = self.engram_root
        if root is None:
            raise EngramError("engram_unavailable", "Engram root not found.")
        import re as _re

        results: list[dict[str, Any]] = []
        terms = [w for w in _re.findall(r"[a-z0-9]+", (query or "").lower())
                 if len(w) >= 3 and w not in _SEARCH_STOPWORDS]

        def _score(text_l: str, score_terms) -> int:
            """Word-boundary, frequency-weighted relevance. Distinct matched
            terms dominate (x10); occurrence count (capped per term) breaks
            ties. Fixes the 2026-06-07 conflation: 'Meta' no longer matches
            'MetaQuest', and the Career file (many 'Meta' mentions) outranks a
            project note's single 'Meta Spatial SDK' aside."""
            distinct = 0
            occ = 0
            for t in score_terms:
                n = len(_re.findall(rf"\b{_re.escape(t)}\b", text_l))
                if n:
                    distinct += 1
                    occ += min(n, 10)
            return distinct * 10 + occ if distinct else 0

        def _first_match_idx(text_l: str, score_terms) -> int:
            starts = []
            for t in score_terms:
                m = _re.search(rf"\b{_re.escape(t)}\b", text_l)
                if m:
                    starts.append(m.start())
            return min(starts) if starts else 0

        if "facts" in scope:
            for f in bs.search_operator_facts(query, limit=limit):
                fh = _score(f.lower(), terms)
                if terms and not fh:
                    # brain_sync substring-matched ("time" inside "itinerary"
                    # etc.) but no word-boundary hit -- not a real match.
                    continue
                results.append({"scope": "facts", "source": bs.OPERATOR_FACTS_TARGET,
                                "snippet": f[:280], "date": "", "hits": fh or 1})
        if "thoughts" in scope:
            seen_thoughts: set[str] = set()
            for t in bs.list_recent_thoughts(days=365):
                hay = f"{t.get('title','')} {t.get('summary','')}".lower()
                hits = _score(hay, terms)
                if not terms or hits:
                    src = f"Thoughts/{t['slug']}/claude.md"
                    seen_thoughts.add(src)
                    results.append({
                        "scope": "thoughts",
                        "source": src,
                        "snippet": (t.get("title", "") + " — " + t.get("summary", ""))[:280],
                        "date": t.get("date", ""),
                        "hits": hits or 1,
                    })
            # Thought BODIES via the FTS index (2026-06-07) — the diaries were
            # previously searchable only by title/summary metadata.
            if terms:
                from . import fts as _fts

                for h in _fts.search(root, query, path_prefixes=("Thoughts/",),
                                     limit=5):
                    if h["source"] in seen_thoughts:
                        continue
                    results.append({
                        "scope": "thoughts",
                        "source": h["source"],
                        "snippet": h["snippet"],
                        "date": "",
                        "hits": int(max(1, round(-h["rank"] * 4))),
                    })
        if "projects" in scope:
            # Keep only the best few projects so any-match noise cannot flood
            # the list in scope order and crowd out better hits downstream.
            project_hits: list[dict[str, Any]] = []
            for name in bs._list_projects():
                ctx = bs.load_engram_context(name) or ""
                low = ctx.lower()
                hits = _score(low, terms)
                if terms and hits:
                    idx = _first_match_idx(low, terms)
                    project_hits.append({
                        "scope": "projects",
                        "source": f"Projects/{name}/claude.md",
                        "snippet": ctx[max(0, idx - 60): idx + 220].strip(),
                        "date": "",
                        "hits": hits,
                    })
            project_hits.sort(key=lambda r: -r["hits"])
            results.extend(project_hits[:4])
            # Full project bodies + digests (knowledge-base.md) + sub-project
            # claude.md via the FTS index (2026-06-13). load_engram_context above
            # only sees the truncated TOP of each project's claude.md, so deep
            # content and non-claude.md project files (e.g. the wiki-mirror
            # knowledge-base.md digests) were unsearchable. Dedup by source so a
            # project already surfaced via its context load is not double-listed.
            if terms:
                from . import fts as _fts

                seen_proj = {r["source"] for r in project_hits}
                for h in _fts.search(root, query, path_prefixes=("Projects/",),
                                     limit=6):
                    if h["source"] in seen_proj:
                        continue
                    seen_proj.add(h["source"])
                    results.append({
                        "scope": "projects",
                        "source": h["source"],
                        "snippet": h["snippet"],
                        "date": "",
                        "hits": int(max(1, round(-h["rank"] * 4))),
                    })
        if "knowledge" in scope:
            # Background notes OUTSIDE the three classic scopes: career history
            # and area notes. PRIMARY backend (2026-06-07): the FTS5/BM25 index
            # (engram_mcp.fts) — corpus IDF downweights common query words
            # ("worked", "top") so the actual Meta bullets outrank
            # resume-discussion noise. The fuzzy paragraph scan below remains
            # the FALLBACK: it covers STT drift ("Metta" -> "Meta"), which
            # exact FTS matching cannot.
            from . import fts as _fts

            fts_hits = _fts.search(
                root, query,
                path_prefixes=("Career/", "Areas/", "Research/", "Ideas/"),
                limit=max(int(limit), 6),
            )
            if fts_hits:
                for h in fts_hits:
                    # bm25 rank: more negative = better. Scale to the positive
                    # cross-scope "hits" axis (rough comparability is enough;
                    # intent-scoped queries are knowledge-only anyway).
                    results.append({
                        "scope": "knowledge",
                        "source": h["source"],
                        "snippet": h["snippet"],
                        "date": "",
                        "hits": int(max(1, round(-h["rank"] * 4))),
                    })
            kterms = [] if fts_hits else list(terms)
            import difflib

            if kterms:
                # The WHOLE Career and Areas trees (2026-06-07): the strong
                # detail lives in nested files -- Career/resume/bullets/
                # meta-ise.md held the "what did I work on at Meta" answer
                # while only Career/claude.md (the summary layer) was searched.
                kfiles: list[Path] = []
                for pattern in ("Career/**/*.md", "Areas/**/*.md",
                                "Research/**/*.md", "Ideas/**/*.md"):
                    kfiles.extend(root.glob(pattern))
                for kf in kfiles:
                    if not kf.is_file():
                        continue
                    try:
                        text = kf.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    vocab = set(_re.findall(r"[a-z0-9]+", text.lower()))
                    expanded: set[str] = set()
                    for term in kterms:
                        if term in vocab:
                            expanded.add(term)
                        else:
                            expanded.update(
                                difflib.get_close_matches(term, vocab, n=2, cutoff=0.8))
                    if not expanded:
                        continue
                    rel = kf.relative_to(root).as_posix()
                    # File-level boost: a file where the query terms recur
                    # throughout (the Career notes for an employer question)
                    # outranks a file with one incidental mention.
                    file_boost = min(_score(text.lower(), expanded) // 10, 5)
                    scored: list[tuple[int, str]] = []
                    for para in _re.split(r"\n\s*\n", text):
                        hits = _score(para.lower(), expanded)
                        if hits:
                            scored.append((hits + file_boost, para.strip()))
                    scored.sort(key=lambda x: -x[0])
                    for hits, para in scored[:3]:
                        results.append({
                            "scope": "knowledge",
                            "source": rel,
                            "snippet": para[:280],
                            "date": "",
                            "hits": hits,
                        })
        # Rank by relevance (distinct term hits) BEFORE truncating, so a
        # strong knowledge/thought hit is never crowded out by weak any-match
        # results that happened to append earlier. Stable: ties keep scope
        # order (facts > thoughts > projects > knowledge).
        results.sort(key=lambda r: -int(r.get("hits", 1)))
        return {"ok": True, "results": results[:limit]}

    def list_projects(self) -> dict[str, Any]:
        if self.transport == "http":
            return self._http_get("list_projects", {})
        ew, bs, _sr = self._ensure_local()
        root = self.engram_root
        if root is None:
            raise EngramError("engram_unavailable", "Engram root not found.")
        projects = []
        for name in bs._list_projects():
            cp = root / "Projects" / name / "claude.md"
            try:
                mtime = datetime.fromtimestamp(cp.stat().st_mtime).isoformat(timespec="seconds")
            except OSError:
                mtime = ""
            projects.append({
                "name": name,
                "context_path": f"Projects/{name}/claude.md",
                "last_modified": mtime,
            })
        return {"ok": True, "projects": projects}

    def sync_status(self, activity_hours: int) -> dict[str, Any]:
        if self.transport == "http":
            return self._http_get("sync_status", {"activity_hours": activity_hours})
        try:
            ew, bs, _sr = self._ensure_local()
        except EngramError as e:
            return {"ok": False, "source_of_truth": cfg.source_of_truth_host(),
                    "transport": self.transport, "reachable": False,
                    "write_queue_depth": 0, "error": e.code, "detail": e.detail}
        root = self.engram_root
        recent = bs.recent_project_activity(hours=activity_hours)
        thoughts = bs.list_recent_thoughts(days=max(1, activity_hours // 24))
        last_consolidation = ""
        reviews = (root / "Reviews" / "daily") if root else None
        if reviews and reviews.exists():
            dailies = sorted(reviews.glob("*.md"))
            if dailies:
                last_consolidation = datetime.fromtimestamp(
                    dailies[-1].stat().st_mtime).isoformat(timespec="seconds")
        return {
            "ok": True,
            "source_of_truth": cfg.source_of_truth_host(),
            "transport": self.transport,
            "reachable": True,
            "engram_root": str(root) if root else "",
            "write_queue_depth": 1 if _WRITE_LOCK.locked() else 0,
            "last_consolidation": last_consolidation,
            "recent_projects": [{"project": a["project"], "modified": a["modified"]} for a in recent],
            "recent_thoughts_count": len(thoughts),
        }

    # =================================================================
    #  WRITES (through the in-process lock §6/F2)
    # =================================================================

    async def append(self, target: str, content: str,
                     heading: str | None, mode: str) -> dict[str, Any]:
        if self.transport == "http":
            return await self._http_post("append", {
                "target": target, "content": content, "heading": heading, "mode": mode})
        ew, _bs, _sr = self._ensure_local()
        resolved = self.validate_path(target, for_write=True)
        # §3.7 protected-file guard: no raw overwrite of CLAUDE.md / _index.md.
        if mode == "overwrite" and not heading and self._is_protected(target):
            raise EngramError("overwrite_forbidden",
                              f"overwrite of protected file is forbidden: {target}")
        async with _WRITE_LOCK:
            try:
                if heading:
                    if not resolved.exists():
                        raise EngramError("file_not_found", f"heading-append to nonexistent file: {target}")
                    result = ew.append_under_heading(target, heading, content)
                else:
                    if mode == "append" and not resolved.parent.exists():
                        raise EngramError("parent_missing", f"parent dir absent (use create): {target}")
                    result = ew.write_engram_file(target, content, mode=mode)
            except FileNotFoundError as e:
                raise EngramError("file_not_found", str(e))
            except ValueError as e:
                raise EngramError("bad_path", str(e))
        result["queued_write_id"] = uuid.uuid4().hex
        return result

    async def create(self, target: str, content: str, make_parents: bool,
                     index_entry: dict | None) -> dict[str, Any]:
        if self.transport == "http":
            return await self._http_post("create", {
                "target": target, "content": content,
                "make_parents": make_parents, "index_entry": index_entry})
        ew, _bs, _sr = self._ensure_local()
        root = self.engram_root
        if root is None:
            raise EngramError("engram_unavailable", "Engram root not found.")
        # validate path-safety BEFORE touching the FS (rules 1-4). Parent-exist
        # check is intentionally skipped here — create may make parents.
        t = (target or "").replace("\\", "/")
        if not t or t.startswith("/") or (len(t) > 1 and t[1] == ":") or ".." in t or not t.endswith(".md"):
            raise EngramError("bad_path", f"bad target: {target!r}")
        resolved = (root / t).resolve()
        if root not in resolved.parents and resolved != root:
            raise EngramError("bad_path", f"path escapes the Engram root: {target!r}")
        async with _WRITE_LOCK:
            if resolved.exists():
                raise EngramError("already_exists", f"target already exists (use append): {target}")
            if not resolved.parent.exists():
                if make_parents:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                else:
                    raise EngramError("parent_missing", f"parent dir absent: {target}")
            try:
                result = ew.write_engram_file(t, content, mode="append")  # append to fresh file == create
            except (ValueError, FileNotFoundError) as e:
                raise EngramError("bad_path", str(e))
            result["mode"] = "create"
            result["queued_write_id"] = uuid.uuid4().hex
            # optional structured index append (never a raw overwrite — §3.7)
            if index_entry and index_entry.get("target"):
                try:
                    if index_entry.get("heading"):
                        ew.append_under_heading(
                            index_entry["target"], index_entry["heading"],
                            index_entry.get("line", ""))
                    else:
                        ew.write_engram_file(
                            index_entry["target"], index_entry.get("line", ""), mode="append")
                    result["index_update"] = {"ok": True, "target": index_entry["target"]}
                except (ValueError, FileNotFoundError) as e:
                    result["index_update"] = {"ok": False, "target": index_entry["target"], "error": str(e)}
        return result

    async def promote_fact(self, text: str, source: str) -> dict[str, Any]:
        if self.transport == "http":
            return await self._http_post("promote_fact", {"text": text, "source": source})
        ew, bs, _sr = self._ensure_local()
        if not (text or "").strip():
            raise EngramError("empty", "fact text is blank.")
        async with _WRITE_LOCK:
            result = bs.add_operator_fact(text, source=source)
        if not result.get("ok"):
            raise EngramError(
                result.get("error") if result.get("error") in ERROR_CODES else "engram_unavailable",
                result.get("error", "promote_fact failed"))
        result["queued_write_id"] = uuid.uuid4().hex
        return result

    async def organize(self, scope: str, days: int, hours: int,
                       dry_run: bool, target_override: str | None) -> dict[str, Any]:
        if self.transport == "http":
            return await self._http_post("organize", {
                "scope": scope, "days": days, "hours": hours,
                "dry_run": dry_run, "target_override": target_override})
        ew, bs, sr = self._ensure_local()
        root = self.engram_root
        if root is None:
            raise EngramError("engram_unavailable", "Engram root not found.")  # selene exit code 2
        # gather → call_selene(profile=selene) → write_digest, preserving the
        # "never write an empty digest" guard. Synthesis is serialized too so two
        # consolidations can't run at once (R-A3).
        material, counts = sr.gather_material(days, hours)
        try:
            digest = sr.call_selene(material)  # uses Hermes :8770, profile=selene
        except RuntimeError as e:
            # selene_review maps gateway failure to exit 3
            raise EngramError("gateway_unreachable", str(e))
        if not digest:
            raise EngramError("empty_digest", "Selene returned an empty digest; not writing.")  # exit 4
        if dry_run:
            return {"ok": True, "digest": digest, "counts": counts, "index_updated": False}
        async with _WRITE_LOCK:
            if target_override:
                resolved = self.validate_path(target_override, for_write=True)
                if not resolved.parent.exists():
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                body = f"# Selene Review\n\n{digest}\n"
                ew.write_engram_file(target_override, body, mode="overwrite")
                written = target_override
                index_updated = False
            else:
                written = sr.write_digest(digest, counts)  # Reviews/daily/<date>.md + index
                index_updated = True
        return {"ok": True, "digest": digest, "written_to": written,
                "counts": counts, "index_updated": index_updated}

    # =================================================================
    #  http transport forwarders
    # =================================================================

    def _http_get(self, tool: str, params: dict) -> dict[str, Any]:
        return self._http_call("GET", tool, params)

    async def _http_post(self, tool: str, body: dict) -> dict[str, Any]:
        return self._http_call("POST", tool, body)

    def _http_call(self, method: str, tool: str, payload: dict) -> dict[str, Any]:
        import httpx
        url = f"{self.base_url}/engram/{tool}"
        try:
            if method == "GET":
                resp = httpx.get(url, params={"json": __import__("json").dumps(payload)}, timeout=30.0)
            else:
                resp = httpx.post(url, json=payload, timeout=480.0)  # organize can cold-swap 35B
        except httpx.RequestError as e:
            raise EngramError("supervisor_unreachable", f"LAN route to engram-mcp failed: {e}")
        if resp.status_code >= 500:
            raise EngramError("engram_unavailable", f"service error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not data.get("ok", False) and data.get("error"):
            raise EngramError(data["error"], data.get("detail", ""))
        return data
