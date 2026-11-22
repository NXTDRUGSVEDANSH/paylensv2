"""
Few-shot retrieval from the existing Chroma knowledge base.

WHAT IS RETRIEVED AND WHAT IS NOT
---------------------------------
Retrieved: question -> SQL examples. That corpus is genuinely large (the seeded
shapes plus everything the classic pipeline has learned) and grows with use, so
similarity search earns its cost.

NOT retrieved: the advisory catalogue. There are nineteen entries in it. Choosing
among nineteen things by cosine distance means an embedding round-trip, a
threshold to tune, and a selection nobody can audit — in order to pick from a list
that fits in a few hundred tokens. The whole catalogue goes in the prompt instead
and the model returns the ids it used, which Python then verifies exist.

A v2 record is never admitted for exact reuse. See writeback().
"""
from __future__ import annotations

import logging

from paylens2 import config

log = logging.getLogger("paylens2.retrieval")


def _stamp() -> str:
    from paylens2.core.model import get_model
    return f"{get_model().version}:{config.PROMPT_VERSION}"


def fewshots(question: str, k: int | None = None) -> list[dict]:
    """
    Nearest question->SQL pairs, as plain dicts for the prompt.

    Failure here is never fatal: a cold prompt with the schema alone is a
    perfectly good prompt, just a less informed one.
    """
    if not config.USE_KB:
        return []
    k = k or config.FEWSHOT_K
    try:
        from backend.kb import store
        if not store.available():
            return []
        recs = store.query(question, n=k)
    except Exception as exc:  # noqa: BLE001
        log.warning("few-shot retrieval failed, continuing without: %s", exc)
        return []

    out = []
    stamp = _stamp()
    for r in recs:
        sql = (r.template_sql or "").strip()
        if not sql:
            continue
        # A v2 record written under a different prompt or schema is dropped.
        # An example the model will copy verbatim outranks any instruction, so a
        # stale one silently reverses a fix. Classic records carry no stamp and
        # are kept, because they are labelled `legacy_schema` below rather than
        # offered as something to copy.
        if str(r.meta.get("source", "")) == "v2" \
                and str(r.meta.get("v2_stamp", "")) != stamp:
            log.info("dropping stale v2 few-shot (stamp %r)", r.meta.get("v2_stamp"))
            continue
        # Classic records are written against fact_transactions. Shown verbatim
        # they would teach the model to query a table v2 blocks — the opposite of
        # what a worked example is for. They are still useful as SHAPE, so they
        # are labelled rather than silently dropped.
        legacy = "fact_transactions" in sql or "dim_" in sql
        out.append({
            "question": r.raw_question or r.canonical_question,
            "sql": sql,
            "similarity": round(r.similarity or 0.0, 4),
            "source": str(r.meta.get("source", "")),
            "legacy_schema": legacy,
        })
    return out


def v2_fewshots(question: str, k: int | None = None) -> list[dict]:
    """Only examples already written against the v2 views — safe to copy verbatim."""
    return [f for f in fewshots(question, k) if not f["legacy_schema"]]


def purge_v2(stale_only: bool = True) -> int:
    """Remove v2 records. With stale_only, keep those matching the current stamp."""
    try:
        from backend.kb import store
        if not store.available():
            return 0
        kb = store.get_kb()
        got = kb.get(where={"source": {"$eq": "v2"}})
        ids = [i for i, m in zip(got.get("ids", []), got.get("metadatas", []))
               if not (stale_only and str((m or {}).get("v2_stamp", "")) == _stamp())]
        if ids:
            kb.delete(ids=ids)
        return len(ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("v2 purge failed: %s", exc)
        return 0


def writeback(question: str, sql: str, *, ok: bool) -> None:
    """
    Record a successful v2 query as a future few-shot.

    Marked `verified=false` and `source=v2`, which keeps it out of the classic
    pipeline's tier-1 exact-reuse path: that path replays stored SQL without
    replanning, and a plausible-but-wrong query admitted there would be served
    verbatim to later questions. As a few-shot it can only ever influence a
    fresh authoring call that still passes every check.
    """
    if not (ok and config.USE_KB):
        return
    try:
        from backend.kb import store
        from backend.kb.keys import record_id
        from backend.kb.store import KBRecord
        if not store.available():
            return
        # v2 has no Resolution, so the classic canonicaliser (which needs metric
        # and dimension ids) does not apply. The question itself is the document,
        # lowercased and whitespace-collapsed — enough for nearest-neighbour
        # retrieval, and deliberately NOT the classic canonical form, so a v2
        # record can never collide with a classic one in the tier-1 index.
        canon = " ".join((question or "").lower().split())
        rec = KBRecord(
            record_id=record_id("v2", canon),
            canonical_question=canon, raw_question=question,
            template_sql=sql, plan={}, param_schema={},
            meta={"source": "v2", "verified": False, "engine": "v2",
                  "v2_stamp": _stamp()})
        store.upsert(rec)
    except Exception as exc:  # noqa: BLE001
        log.warning("v2 write-back skipped: %s", exc)
