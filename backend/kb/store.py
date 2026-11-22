"""
ChromaDB knowledge base. Ported from Fractal AI's rag_service._get_chroma/_safe_add
with four defects fixed.

1. COSINE SPACE IS DECLARED AND ASSERTED.
   Fractal calls get_or_create_collection(name="sql", embedding_function=ef) with no
   metadata. Chroma's default space is squared L2 and query() returns DISTANCES, so a
   ">= 0.95 similarity" gate on that number is meaningless AND inverted. Worse,
   get_or_create silently ignores conflicting metadata on an existing collection, so a
   directory created before the fix keeps returning L2 forever. We assert on open.

2. RECORDS CARRY METADATA. Fractal stores bare json.dumps({question, sql}) with no
   metadata at all, which makes trust/version/shape filtering impossible.

3. IDS UPSERT ON (shape, canonical_question) rather than hashing the whole document,
   so re-answering a question replaces its record instead of adding a rival one.

4. ONE COLLECTION, NOT THREE. Fractal creates `ddl` (never written to) and
   `documentation`; here the semantic model goes into the prompt whole, so only the
   Q->SQL cache survives.
"""
from __future__ import annotations

import functools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from backend import config

log = logging.getLogger("paylens.kb")

COLLECTION = "paylens_sql"      # Chroma requires 3-512 chars of [a-zA-Z0-9._-]
EMBED_DIM = 384                 # MiniLM-L6-v2 via ONNX, local, no API key


@dataclass
class KBRecord:
    record_id: str
    canonical_question: str
    raw_question: str
    template_sql: str
    plan: dict
    param_schema: dict[str, str]
    meta: dict[str, Any] = field(default_factory=dict)
    similarity: float | None = None

    def document(self) -> str:
        """
        The DOCUMENT is the canonical question and nothing else, because Chroma
        embeds the document. Storing a JSON payload here instead (as the obvious
        first implementation does) means the embedding is of the JSON blob, so an
        IDENTICAL question scores ~0.43 rather than 1.0 and the tier-1 fast path
        can never fire. The payload rides in metadata instead.
        """
        return self.canonical_question

    def payload(self) -> str:
        return json.dumps({
            "raw_question": self.raw_question,
            "template_sql": self.template_sql,
            "plan": self.plan,
            "param_schema": self.param_schema,
        }, ensure_ascii=False, default=str)

    @classmethod
    def from_chroma(cls, rid: str, doc: str, meta: dict, similarity: float | None) -> "KBRecord":
        meta = dict(meta or {})
        try:
            d = json.loads(meta.pop("payload", "") or "{}")
        except json.JSONDecodeError:
            d = {}
        return cls(record_id=rid, canonical_question=doc or "",
                   raw_question=d.get("raw_question", ""),
                   template_sql=d.get("template_sql", ""), plan=d.get("plan") or {},
                   param_schema=d.get("param_schema") or {}, meta=meta,
                   similarity=similarity)


class KBUnavailable(RuntimeError):
    pass


_state: dict[str, Any] = {}


def available() -> bool:
    if getattr(config, "DISABLE_CHROMA", False):
        return False
    try:
        get_kb()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge base unavailable (%s) — running without the cache", exc)
        return False


def get_kb():
    """Lazy singleton. Raises KBUnavailable if Chroma or the embedder is missing."""
    if "col" in _state:
        return _state["col"]
    try:
        import chromadb
        from chromadb.config import Settings
        from chromadb.utils import embedding_functions
    except ImportError as exc:
        raise KBUnavailable(f"chromadb is not installed: {exc}") from exc

    config.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    ef = embedding_functions.DefaultEmbeddingFunction()
    client = chromadb.PersistentClient(path=str(config.CHROMA_PATH),
                                       settings=Settings(anonymized_telemetry=False))
    col = client.get_or_create_collection(
        name=COLLECTION, embedding_function=ef, metadata={"hnsw:space": "cosine"})

    space = (col.metadata or {}).get("hnsw:space")
    if space != "cosine":
        raise KBUnavailable(
            f"collection '{COLLECTION}' uses hnsw:space={space!r}, not 'cosine'. "
            f"query() would return squared-L2 distances and every similarity gate "
            f"would silently misbehave. Delete {config.CHROMA_PATH} and re-seed.")

    _state.update(col=col, ef=ef, client=client)
    return col


def collection(name: str):
    """
    A second collection in the same store, sharing the client and the embedder.

    The ONNX model costs ~25s to load; opening a separate PersistentClient for
    another corpus would pay that twice and hold two handles on one directory.

    Separate collections rather than one with a `kind` filter: the Q->SQL corpus
    is retrieved to show the model how a query is SHAPED, and a definition
    retrieved into that slot would teach it to answer a measurement with prose.
    Keeping them apart means neither can ever be returned in the other's place.
    """
    get_kb()                                    # ensures client + ef exist
    key = f"col:{name}"
    if key in _state:
        return _state[key]
    col = _state["client"].get_or_create_collection(
        name=name, embedding_function=_state["ef"],
        metadata={"hnsw:space": "cosine"})
    space = (col.metadata or {}).get("hnsw:space")
    if space != "cosine":
        raise KBUnavailable(
            f"collection {name!r} uses hnsw:space={space!r}, not 'cosine'")
    _state[key] = col
    return col


def embed(texts: list[str]) -> list[list[float]]:
    get_kb()
    return [list(v) for v in _state["ef"](texts)]


def count(*, verified_only: bool = False) -> int:
    col = get_kb()
    if not verified_only:
        return col.count()
    got = col.get(where={"verified": True}, include=[])
    return len(got.get("ids") or [])


def _meta_for(rec: KBRecord) -> dict:
    meta = {k: v for k, v in rec.meta.items() if isinstance(v, (str, int, float, bool))}
    meta["payload"] = rec.payload()
    return meta


def upsert(rec: KBRecord) -> str:
    col = get_kb()
    col.upsert(ids=[rec.record_id], documents=[rec.document()],
               metadatas=[_meta_for(rec)])
    return rec.record_id


def upsert_many(recs: list[KBRecord]) -> int:
    if not recs:
        return 0
    col = get_kb()
    col.upsert(ids=[r.record_id for r in recs], documents=[r.document() for r in recs],
               metadatas=[_meta_for(r) for r in recs])
    return len(recs)


def query(canonical_question: str, *, where: dict | None = None, n: int = 3
          ) -> list[KBRecord]:
    """Similarity = 1 - cosine distance, valid only because the space is asserted."""
    col = get_kb()
    if col.count() == 0:
        return []
    try:
        res = col.query(query_texts=[canonical_question], n_results=min(n, col.count()),
                        where=where or None)
    except Exception as exc:  # noqa: BLE001
        # Chroma can throw while the HNSW index is uninitialised. Degrade to COLD
        # rather than failing the request.
        log.warning("KB query failed: %s", exc)
        return []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    out = []
    for i, rid in enumerate(ids):
        try:
            out.append(KBRecord.from_chroma(rid, docs[i], metas[i], 1.0 - float(dists[i])))
        except Exception:  # noqa: BLE001
            continue
    return out


def get(record_id: str) -> KBRecord | None:
    col = get_kb()
    got = col.get(ids=[record_id])
    if not got.get("ids"):
        return None
    return KBRecord.from_chroma(got["ids"][0], got["documents"][0],
                                (got.get("metadatas") or [{}])[0], None)


def mark_invalid(record_id: str, reason: str) -> None:
    """
    Eviction-on-guard-failure. A tier-1 record whose SQL no longer passes the guard
    is demoted in the same request, so the KB cleans itself.
    """
    rec = get(record_id)
    if not rec:
        return
    rec.meta.update(verified=False, invalid_reason=reason[:300])
    upsert(rec)
    log.info("KB record %s demoted: %s", record_id, reason[:120])


def touch(record_id: str, *, success: bool) -> None:
    rec = get(record_id)
    if not rec:
        return
    rec.meta["last_used_at"] = int(time.time())
    rec.meta["use_count"] = int(rec.meta.get("use_count", 0)) + 1
    if success:
        rec.meta["success_count"] = int(rec.meta.get("success_count", 0)) + 1
    upsert(rec)


def reset(*, keep_seeds: bool = True) -> int:
    """`--reset-kb`: drop learned records so a demo run is reproducible."""
    col = get_kb()
    where = {"source": {"$ne": "seed"}} if keep_seeds else None
    got = col.get(where=where, include=[])
    ids = got.get("ids") or []
    if ids:
        col.delete(ids=ids)
    return len(ids)


@functools.lru_cache(maxsize=1)
def warm() -> bool:
    """Pay the one-off ONNX model load (~25s cold) at startup, not mid-request."""
    try:
        embed(["warmup"])
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("KB warmup failed: %s", exc)
        return False
