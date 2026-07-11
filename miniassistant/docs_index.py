"""
Semantic docs index (RAG) over agent reference docs + direction files.

Separate ChromaDB collection ("docs_chunks") inside the existing mempalace
palace. The markdown files on disk stay the single source of truth — the
index is derived and rebuilt incrementally via content hashes, so files can
be edited freely (WebUI editor, exec, git) and re-synced cheaply.

The manifest lives INSIDE the palace directory: when the palace is wiped by
the ChromaDB version migration in memory.py, the manifest goes with it and
the next sync rebuilds the collection from scratch (self-healing).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from miniassistant.config import load_config

_log = logging.getLogger("miniassistant.docs_index")

DOCS_COLLECTION = "docs_chunks"
_MANIFEST_NAME = ".docs_index.json"
_INDEX_EXTS = {".md", ".txt"}
# (subdir under agent_dir, category label)
_INDEX_ROOTS: tuple[tuple[str, str], ...] = (("docs", "docs"), ("directions", "directions"), ("prefs", "prefs"))

_CHUNK_TARGET = 1500   # soft max chars per chunk
_CHUNK_MIN = 200       # merge sections smaller than this into the next one

_sync_lock = threading.Lock()
_last_freshness_check = 0.0
_FRESHNESS_DEBOUNCE_S = 30.0


def docs_index_enabled(project_dir: str | None = None, config: dict[str, Any] | None = None) -> bool:
    """Enabled when mempalace is enabled and mempalace.docs_index is not false."""
    if config is None:
        config = load_config(project_dir)
    mp = config.get("mempalace") or {}
    return bool(mp.get("enabled", False)) and bool(mp.get("docs_index", True))


def _agent_dir(project_dir: str | None = None) -> Path | None:
    config = load_config(project_dir)
    ad = (config.get("agent_dir") or "").strip()
    return Path(ad).expanduser().resolve() if ad else None


def _iter_index_files(project_dir: str | None = None) -> list[tuple[str, str, Path]]:
    """Returns [(relpath, category, abspath)] for all indexable files."""
    base = _agent_dir(project_dir)
    if not base:
        return []
    out: list[tuple[str, str, Path]] = []
    for sub, category in _INDEX_ROOTS:
        d = base / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in _INDEX_EXTS:
                out.append((f"{sub}/{p.name}", category, p))
    return out


def _chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split markdown into heading-aware chunks. Returns [(heading, chunk_text)].

    Sections are cut at #/##/### headings, merged when tiny, and hard-split
    at paragraph boundaries when a single section exceeds the target size.
    """
    lines = text.split("\n")
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    buf: list[str] = []
    for line in lines:
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            if buf and any(l.strip() for l in buf):
                sections.append((heading, buf))
            heading = m.group(2).strip()
            buf = [line]
        else:
            buf.append(line)
    if buf and any(l.strip() for l in buf):
        sections.append((heading, buf))

    # merge tiny sections into the following one
    merged: list[tuple[str, str]] = []
    carry_head, carry = "", ""
    for head, sec_lines in sections:
        sec = "\n".join(sec_lines).strip()
        if carry:
            sec = carry + "\n\n" + sec
            head = carry_head or head
            carry_head, carry = "", ""
        if len(sec) < _CHUNK_MIN:
            carry_head, carry = head, sec
            continue
        merged.append((head, sec))
    if carry:
        if merged:
            h, prev = merged[-1]
            merged[-1] = (h, prev + "\n\n" + carry)
        else:
            merged.append((carry_head, carry))

    # hard-split oversized sections at paragraph boundaries
    chunks: list[tuple[str, str]] = []
    for head, sec in merged:
        if len(sec) <= _CHUNK_TARGET:
            chunks.append((head, sec))
            continue
        paras = re.split(r"\n\s*\n", sec)
        cur = ""
        for para in paras:
            if cur and len(cur) + len(para) + 2 > _CHUNK_TARGET:
                chunks.append((head, cur.strip()))
                cur = para
            else:
                cur = (cur + "\n\n" + para) if cur else para
        if cur.strip():
            chunks.append((head, cur.strip()))
    return [(h, c) for h, c in chunks if c.strip()]


def _palace_and_collection(project_dir: str | None = None):
    """Returns (palace_path, collection) or (None, None) when unavailable."""
    from miniassistant.memory import _check_mempalace, _mempalace_palace_path
    if not _check_mempalace(project_dir):
        return None, None
    import os as _os
    _os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    import chromadb
    palace_path = _mempalace_palace_path(project_dir)
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection(DOCS_COLLECTION, metadata={"hnsw:space": "cosine"})
    if (col.metadata or {}).get("hnsw:space") != "cosine":
        # legacy collection created with default l2 space → rebuild with cosine
        client.delete_collection(DOCS_COLLECTION)
        Path(palace_path, _MANIFEST_NAME).unlink(missing_ok=True)
        col = client.get_or_create_collection(DOCS_COLLECTION, metadata={"hnsw:space": "cosine"})
    return palace_path, col


def _load_manifest(palace_path: str) -> dict[str, str]:
    p = Path(palace_path) / _MANIFEST_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_manifest(palace_path: str, manifest: dict[str, str]) -> None:
    p = Path(palace_path) / _MANIFEST_NAME
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(p)


def sync_docs_index(project_dir: str | None = None, force: bool = False) -> dict[str, Any]:
    """Incremental sync of docs/directions into the docs_chunks collection.

    Per file: sha256 vs manifest → unchanged files are skipped entirely.
    Changed/new files: delete old chunks (where source=relpath), re-chunk, add.
    Files gone from disk: chunks deleted. Idempotent, thread-safe.
    """
    if not docs_index_enabled(project_dir):
        return {"error": "docs index disabled (mempalace.enabled / mempalace.docs_index)"}
    with _sync_lock:
        t0 = time.time()
        palace_path, col = _palace_and_collection(project_dir)
        if col is None:
            return {"error": "mempalace/chromadb not available"}
        manifest = {} if force else _load_manifest(palace_path)
        new_manifest: dict[str, str] = {}
        stats = {"files": 0, "unchanged": 0, "indexed": 0, "removed": 0, "chunks": 0}

        if force:
            try:
                existing = col.get(include=[])
                if existing.get("ids"):
                    col.delete(ids=existing["ids"])
            except Exception as e:
                _log.warning("docs_index: force-clear failed: %s", e)

        for relpath, category, path in _iter_index_files(project_dir):
            stats["files"] += 1
            try:
                raw = path.read_bytes()
            except OSError as e:
                _log.warning("docs_index: cannot read %s: %s", relpath, e)
                continue
            digest = hashlib.sha256(raw).hexdigest()
            new_manifest[relpath] = digest
            if manifest.get(relpath) == digest:
                stats["unchanged"] += 1
                continue
            text = raw.decode("utf-8", errors="replace")
            chunks = _chunk_markdown(text)
            try:
                col.delete(where={"source": relpath})
            except Exception:
                pass
            if not chunks:
                continue
            ids = [f"doc::{relpath}::{i}" for i in range(len(chunks))]
            docs = []
            metas = []
            for heading, chunk in chunks:
                # prefix breadcrumb so the embedding carries file context
                docs.append(f"[{relpath}{' § ' + heading if heading else ''}]\n{chunk}")
                metas.append({"source": relpath, "category": category, "heading": heading or "", "file_hash": digest})
            col.add(ids=ids, documents=docs, metadatas=metas)
            stats["indexed"] += 1
            stats["chunks"] += len(chunks)

        # files removed from disk → drop their chunks
        for relpath in manifest:
            if relpath not in new_manifest:
                try:
                    col.delete(where={"source": relpath})
                    stats["removed"] += 1
                except Exception:
                    pass

        _save_manifest(palace_path, new_manifest)
        stats["total_chunks"] = col.count()
        stats["seconds"] = round(time.time() - t0, 1)
        _log.info("docs_index: sync done — %s", stats)
        return stats


def sync_docs_index_background(project_dir: str | None = None) -> None:
    """Fire-and-forget sync in a daemon thread (startup / WebUI save hook)."""
    if not docs_index_enabled(project_dir):
        return
    t = threading.Thread(target=lambda: sync_docs_index(project_dir), name="docs-index-sync", daemon=True)
    t.start()


def _ensure_fresh(project_dir: str | None = None) -> None:
    """Cheap freshness check before a search, debounced. Re-syncs when the
    file set or any hash changed (agents create directions via exec — no hook)."""
    global _last_freshness_check
    now = time.time()
    if now - _last_freshness_check < _FRESHNESS_DEBOUNCE_S:
        return
    _last_freshness_check = now
    try:
        from miniassistant.memory import _mempalace_palace_path
        palace_path = _mempalace_palace_path(project_dir)
        manifest = _load_manifest(palace_path)
        files = _iter_index_files(project_dir)
        if len(files) != len(manifest):
            sync_docs_index(project_dir)
            return
        for relpath, _cat, path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if manifest.get(relpath) != digest:
                sync_docs_index(project_dir)
                return
    except Exception as e:
        _log.debug("docs_index freshness check failed: %s", e)


def search_docs_index(
    query: str,
    project_dir: str | None = None,
    n_results: int = 5,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search over the docs index. Returns
    [{'content','source','heading','category','similarity'}]."""
    if not docs_index_enabled(project_dir):
        return []
    _ensure_fresh(project_dir)
    try:
        _palace, col = _palace_and_collection(project_dir)
        if col is None or col.count() == 0:
            return []
        where = {"category": category} if category in ("docs", "directions", "prefs") else None
        res = col.query(query_texts=[query], n_results=max(1, min(10, n_results)), where=where)
        out: list[dict[str, Any]] = []
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, doc in enumerate(docs):
            meta = metas[i] if i < len(metas) else {}
            dist = dists[i] if i < len(dists) else None
            sim = round(1.0 - dist, 3) if isinstance(dist, (int, float)) else 0.0
            out.append({
                "content": doc,
                "source": meta.get("source", ""),
                "heading": meta.get("heading", ""),
                "category": meta.get("category", ""),
                "similarity": sim,
            })
        return out
    except Exception as e:
        _log.warning("docs_index search failed: %s", e)
        return []
