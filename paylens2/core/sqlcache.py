"""
The NLQ -> SQL cache.

WHAT IS CACHED, AND WHAT DELIBERATELY IS NOT
--------------------------------------------
The AUTHORED query, and nothing else. A hit skips call 1 — the 7-13 second
model call that writes the SQL — and then rejoins the normal path: check 10,
all nine guard checks, execute, profile, compose. So a cached answer is still
measured against today's data, and the row filter is still injected for
whoever is asking.

The answer itself is never cached. A stored answer is a number that was true
once, and there is no way to show a reader which of those two it is.

TWO TRAPS THIS DESIGN EXISTS TO AVOID
-------------------------------------
1. DATES. `periods.resolve()` turns "last week" into literal dates that are
   baked into the SQL. Replaying that query a week later answers a different
   question with no error anywhere — correct SQL, wrong window, confident
   prose. So the resolved window is part of the key: the same words asked
   after the week rolls over are a MISS, by construction.

2. ROW SCOPE. What is stored is the SQL as the model wrote it, never
   `sql_executed`, because the executed text has one role's predicate welded
   into it. Replay hands the stored query back to `guard()`, which injects the
   predicates for the caller. A cached entry therefore cannot leak a scope.

PROMOTION, NOT TRUST
--------------------
A query is not eligible the first time it works. It has to produce an
equivalent query `CACHE_PROMOTE_AFTER` times before it is marked verified. The
classic pipeline could reuse SQL immediately because its queries were compiled
from the semantic model and could not be wrong; a model-authored query can be
plausible and wrong, and one of those served verbatim would answer the next
ten questions.

A replay that fails validation is a MISS, never an error: the record is
demoted and the question goes to the model as if the cache had been empty.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from paylens2 import config
from paylens2.core import periods

log = logging.getLogger("paylens2.sqlcache")

# key -> (stored_at, payload)
_l0: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


@dataclass
class Hit:
    layer: str                       # "l0" | "l1"
    key: str
    sql: str
    record_id: str = ""              # "" for an L0-only hit
    intent: str = "single_value"
    chart: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    playbook_ids: list = field(default_factory=list)


def _norm_q(question: str) -> str:
    """Trivial retyping still hits; a different question never does."""
    return re.sub(r"\s+", " ", (question or "").strip().lower()).rstrip("?.! ")


def norm_sql(sql: str) -> str:
    """For deciding whether two authorings produced the SAME query."""
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).lower()


def _windows(question: str, today: date) -> str:
    """
    The resolved period, as part of the key.

    Every relative phrase the question mentions is resolved against today and
    pinned into the key. A question naming no relative period is keyed on the
    date itself, because the authoring prompt carries today and a default
    window may have been chosen from it.
    """
    low = _norm_q(question)
    found = [f"{p}={s}:{e}" for p, s, e in periods.resolve(today) if p in low]
    return "|".join(sorted(found)) if found else f"asof={today.isoformat()}"


def key(question: str, *, role: str, today: date) -> str:
    from paylens2.core.retrieval import _stamp
    parts = ["plv2", _norm_q(question), role, _windows(question, today),
             _stamp(), str(config.MAX_SQL_ROWS), config.AUTHOR_MODEL]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


# ── layer 0: in process ─────────────────────────────────────────────────────
def _l0_get(k: str) -> dict | None:
    hit = _l0.get(k)
    if not hit:
        return None
    stored_at, payload = hit
    if time.time() - stored_at > config.CACHE_TTL_S:
        _l0.pop(k, None)
        return None
    _l0.move_to_end(k)
    return payload


def _l0_put(k: str, payload: dict) -> None:
    _l0[k] = (time.time(), payload)
    _l0.move_to_end(k)
    while len(_l0) > config.CACHE_SIZE:
        _l0.popitem(last=False)


# ── layer 1: the Chroma record, by metadata ─────────────────────────────────
# Matched on an exact metadata key rather than by similarity, so there is no
# embedding round-trip: a `get(where=...)` is a lookup, not a vector search.
def _l1_get(k: str) -> tuple[dict, str] | None:
    try:
        from backend.kb import store
        if not store.available():
            return None
        got = store.get_kb().get(where={"$and": [
            {"v2_cache_key": {"$eq": k}}, {"verified": {"$eq": True}}]})
    except Exception as exc:  # noqa: BLE001
        log.warning("cache lookup failed, continuing without: %s", exc)
        return None
    ids = got.get("ids") or []
    if not ids:
        return None
    meta = (got.get("metadatas") or [{}])[0] or {}
    try:
        payload = json.loads(meta.get("v2_payload") or "{}")
    except json.JSONDecodeError:
        return None
    return (payload, ids[0]) if payload.get("sql") else None


def lookup(question: str, *, role: str, today: date) -> Hit | None:
    if not config.USE_CACHE:
        return None
    k = key(question, role=role, today=today)
    payload = _l0_get(k)
    if payload:
        return Hit(layer="l0", key=k, **_fields(payload))
    found = _l1_get(k)
    if found:
        payload, rid = found
        _l0_put(k, payload)          # warm L0 so the next hit is instant
        return Hit(layer="l1", key=k, record_id=rid, **_fields(payload))
    return None


def _fields(p: dict) -> dict:
    return {"sql": p.get("sql", ""), "intent": p.get("intent", "single_value"),
            "chart": p.get("chart") or {}, "assumptions": p.get("assumptions") or [],
            "playbook_ids": p.get("playbook_ids") or []}


def payload_of(a) -> dict:
    """The part of an authoring result worth keeping. The chart spec matters:
    without it a cached answer would silently lose its visual."""
    return {"sql": a.sql, "intent": a.intent, "chart": a.chart or {},
            "assumptions": list(a.assumptions or []),
            "playbook_ids": list(a.playbook_ids or [])}


def remember(question: str, *, role: str, today: date, payload: dict) -> str:
    """
    Record a clean run against this key. Returns the promotion state:
    "pending" while it still needs repeats, "promoted" once it is eligible.

    L0 is filled immediately — it is process-local, expires, and is discarded
    on restart, so there is nothing to be gained by making it wait. L1 is what
    requires repeats, because that is the layer that survives.
    """
    if not (config.USE_CACHE and payload.get("sql")):
        return "off"
    k = key(question, role=role, today=today)
    _l0_put(k, payload)
    try:
        return _promote(question, k, payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache promotion skipped: %s", exc)
        return "pending"


def _promote(question: str, k: str, payload: dict) -> str:
    """
    Count successful repeats on the KB record and flip `verified` at the
    threshold. Reuses the record write-back already created, so one row serves
    as both the few-shot example and the cache entry.
    """
    from backend.kb import store
    from backend.kb.keys import record_id
    from backend.kb.store import KBRecord
    from paylens2.core.retrieval import _stamp
    if not store.available():
        return "pending"

    canon = " ".join((question or "").lower().split())
    rid = record_id("v2", canon)
    existing = store.get(rid)
    meta: dict[str, Any] = dict(existing.meta) if existing else {}

    # A different query for the same key resets the count: the point of
    # counting is agreement between independent authorings, and two different
    # queries are evidence of the opposite.
    same = norm_sql(str(meta.get("v2_sql_norm", ""))) == norm_sql(payload["sql"])
    hits = int(meta.get("v2_hits", 0)) + 1 if same and meta.get("v2_cache_key") == k else 1

    promoted = hits >= config.CACHE_PROMOTE_AFTER
    meta.update(source="v2", engine="v2", v2_stamp=_stamp(), v2_cache_key=k,
                v2_hits=hits, v2_sql_norm=norm_sql(payload["sql"]),
                v2_payload=json.dumps(payload), verified=bool(promoted))
    store.upsert(KBRecord(record_id=rid, canonical_question=canon,
                          raw_question=question, template_sql=payload["sql"],
                          plan={}, param_schema={}, meta=meta))
    return "promoted" if promoted else "pending"


def demote(hit: Hit, reason: str) -> None:
    """A replay that failed validation. Drop it from both layers."""
    _l0.pop(hit.key, None)
    if not hit.record_id:
        return
    try:
        from backend.kb import store
        store.mark_invalid(hit.record_id, f"cache replay rejected: {reason}")
    except Exception as exc:  # noqa: BLE001
        log.warning("cache demotion failed: %s", exc)


def touch(hit: Hit) -> None:
    """A replay that worked — bump usage counters for the record."""
    if not hit.record_id:
        return
    try:
        from backend.kb import store
        store.touch(hit.record_id, success=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache touch failed: %s", exc)


def stats() -> dict:
    return {"entries": len(_l0), "capacity": config.CACHE_SIZE,
            "ttl_s": config.CACHE_TTL_S, "enabled": config.USE_CACHE,
            "promote_after": config.CACHE_PROMOTE_AFTER}


def clear() -> int:
    n = len(_l0)
    _l0.clear()
    return n
