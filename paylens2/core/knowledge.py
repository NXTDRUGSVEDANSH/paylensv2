"""
Retrieval over the knowledge corpus, for questions that want a definition
rather than a number.

WHY THIS IS A SEPARATE CORPUS FROM THE FEW-SHOTS
------------------------------------------------
`paylens_sql` holds question -> SQL pairs and is retrieved to show the model how
a query is SHAPED. A definition retrieved into that slot would teach it to
answer a measurement with prose. They are different collections so neither can
ever be returned in the other's place.

WHAT IS AUTHORITATIVE AND WHAT IS NOT
-------------------------------------
Chunks carry `authoritative`. The metric and glossary tiers are generated from
the semantic model and the catalogue, so they are what PayLens actually
computes. The industry tier is sourced context and is explicitly NOT the
definition: the public definitions of authorisation rate disagree with each
other about the denominator and none of them address unresolved attempts, which
is the one choice that matters most here.

`canonical_formula()` is the backstop. A composed explanation that states a
formula for a known metric is checked against views.yaml, for the same reason
check 10d exists: the formula in the prose and the formula in the SQL have to be
the same formula, or the system is lying in one of the two places.
"""
from __future__ import annotations

import logging
import re

from paylens2 import config
from paylens2.knowledge.build import COLLECTION

log = logging.getLogger("paylens2.knowledge")

TOP_K = int(__import__("os").getenv("V2_KNOWLEDGE_K", "5"))


def available() -> bool:
    try:
        from backend.kb import store
        return store.available() and store.collection(COLLECTION).count() > 0
    except Exception:  # noqa: BLE001
        return False


def search(question: str, k: int | None = None) -> list[dict]:
    """
    Nearest chunks, authoritative tiers first.

    Failure is never fatal: an explanation built from the semantic model alone
    is still a correct explanation, just a less contextual one.
    """
    if not config.USE_KB:
        return []
    k = k or TOP_K
    try:
        from backend.kb import store
        col = store.collection(COLLECTION)
        n = col.count()
        if not n:
            return []
        res = col.query(query_texts=[question], n_results=min(k, n))
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge retrieval failed, continuing without: %s", exc)
        return []

    out = []
    for i, cid in enumerate((res.get("ids") or [[]])[0]):
        meta = ((res.get("metadatas") or [[]])[0])[i] or {}
        dist = ((res.get("distances") or [[]])[0])[i]
        out.append({
            "id": cid,
            "tier": meta.get("tier", ""),
            "term": meta.get("term", ""),
            "ref": meta.get("ref", ""),
            "text": meta.get("text", ""),
            "formula": meta.get("formula", ""),
            "authoritative": bool(meta.get("authoritative")),
            "sources": [s for s in str(meta.get("sources", "")).split() if s],
            "similarity": round(1.0 - float(dist), 4),
        })
    # Authoritative first, then by similarity. A definition the system can stand
    # behind outranks a closer-matching magazine paragraph.
    out.sort(key=lambda c: (not c["authoritative"], -c["similarity"]))
    return out


def canonical_formula(metric_id: str) -> str:
    """The expression check 10d enforces, or '' if the metric is unknown."""
    from paylens2.core.model import get_model
    m = get_model()
    met = m.metrics.get(metric_id)
    if met is None:
        return ""
    num = getattr(met, "numerator", "") or getattr(met, "expr", "") or ""
    den = getattr(met, "denominator", "") or ""
    return f"({num}) / nullif({den}, 0)" if den else num


def mentioned_metrics(text: str) -> list[str]:
    """Which known metrics a piece of prose is talking about, by id or synonym."""
    from paylens2.core.model import get_model
    low = " ".join((text or "").lower().split())
    hits = []
    for mid, met in get_model().metrics.items():
        names = [mid, mid.replace("_", " "), met.label.lower()]
        names += [str(s).lower() for s in getattr(met, "synonyms", []) or []]
        if any(re.search(rf"\b{re.escape(n)}\b", low) for n in names if n):
            hits.append(mid)
    return hits


_FORMULA_HINT = re.compile(
    r"count\(\s*\*\s*\)|sum\s*\(|avg\s*\(|÷|/\s*nullif|divided by|filter\s*\(", re.I)


def verify_formula(text: str, metric_ids: list[str]) -> tuple[bool, str]:
    """
    If the prose states a formula for a known metric, it must be the real one.

    Returns (ok, note). A composition that describes a metric WITHOUT giving a
    formula passes — not every explanation needs to be arithmetic. One that
    gives arithmetic is checked, because a plausible-but-wrong formula is the
    single most damaging thing this path could produce: the user would go and
    compute it that way.
    """
    if not _FORMULA_HINT.search(text or ""):
        return True, "no formula stated; nothing to check"
    from paylens2.core.model import get_model

    def norm(x: str) -> str:
        return re.sub(r"[^a-z0-9*()]+", "", (x or "").lower())

    body = norm(text)
    metrics = get_model().metrics
    checked = []
    for mid in metric_ids:
        met = metrics.get(mid)
        if met is None:
            continue
        checked.append(mid)
        num = norm(getattr(met, "numerator", "") or getattr(met, "expr", ""))
        den = norm(getattr(met, "denominator", "") or "")
        # The numerator is the discriminating half: every ratio here shares the
        # same denominator, so matching only that would pass a composition that
        # swapped success for failure.
        if num and num in body and (not den or den in body):
            return True, f"formula for {mid} matches the semantic model"
    if not checked:
        return True, "no known metric named; nothing to check"
    return False, (f"the explanation states a formula but it does not match the "
                   f"expression PayLens runs for {', '.join(checked)}")
