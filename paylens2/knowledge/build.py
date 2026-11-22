"""
Build the knowledge corpus and embed it into Chroma.

    python -m paylens2.knowledge.build            # rebuild
    python -m paylens2.knowledge.build --show     # print the chunks, embed nothing

THREE TIERS, AND WHY THE ORDER MATTERS
--------------------------------------
1. metric   — generated from `schema/views.yaml`. The AUTHORITY on what PayLens
              computes, because those expressions are what check 10d enforces in
              generated SQL. Generated rather than written, so a metric whose
              definition changes cannot leave a stale explanation behind.
2. glossary — the existing `semantic/playbooks.yaml` entries, already written and
              already the words the rest of the product uses.
3. industry — `industry.yaml`: how the industry words the same ideas, with
              sources, and where PayLens deliberately differs.

Tier 3 is context, never the definition. The denominator is the live example of
why: no public definition of authorisation rate addresses unresolved attempts,
and excluding PENDING is a choice this system made and must own, not one it can
cite someone else for. A chunk that said "approvals / attempts" would put the
knowledge base at odds with the guard.

Chunking is one chunk per concept rather than a fixed window. These are already
short, self-contained definitions; splitting them at 500 characters would cut a
formula away from the note saying what it excludes.
"""
from __future__ import annotations

import argparse
import hashlib
import logging

import yaml

from paylens2 import config

log = logging.getLogger("paylens2.knowledge")

COLLECTION = "paylens_knowledge"
INDUSTRY_YAML = config.VIEWS_YAML.parent.parent / "knowledge" / "industry.yaml"


def _cid(kind: str, key: str) -> str:
    return hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()[:24]


def _plain_formula(met) -> str:
    """The arithmetic in words, for a reader who does not read SQL."""
    num = getattr(met, "numerator", "") or ""
    den = getattr(met, "denominator", "") or ""
    if not den:
        return num
    def words(expr: str) -> str:
        e = expr.strip()
        if e.startswith("count(*) filter (where ") and e.endswith(")"):
            return e[len("count(*) filter (where "):-1].strip() + " attempts"
        if e.startswith("sum("):
            return "the sum of " + e[4:-1].strip()
        return e
    return f"{words(num)} ÷ {words(den)}"


def metric_chunks() -> list[dict]:
    """One chunk per metric, straight from the semantic model."""
    from paylens2.core.model import get_model
    m = get_model()
    out = []
    for mid, met in m.metrics.items():
        # A ratio carries numerator/denominator; a count or sum carries `expr`.
        # Reading only the first pair left txn_count and gmv with no formula at
        # all — the two metrics a business reader is most likely to ask about.
        num = getattr(met, "numerator", "") or ""
        den = getattr(met, "denominator", "") or ""
        expr = getattr(met, "expr", "") or ""
        if not num and expr:
            num = expr
        note = " ".join(str(getattr(met, "denominator_note", "") or "").split())
        syn = list(getattr(met, "synonyms", []) or [])

        body = [f"{met.label} ({mid}) is a {getattr(met, 'type', 'metric')} "
                f"computed over {getattr(met, 'base', 'v_attempts')}."]
        if den:
            body.append(f"In plain arithmetic: {_plain_formula(met)}.")
            body.append(f"The exact expression PayLens runs is "
                        f"({num}) / nullif({den}, 0).")
        elif num:
            body.append(f"The exact expression PayLens runs is {num}.")
        if note:
            body.append(note)
        body.append(f"It is reported as {getattr(met, 'format', 'number')}"
                    + (f" to {met.decimals} decimals"
                       if getattr(met, "decimals", None) is not None else "") + ".")
        if getattr(met, "higher_is_better", None) is not None:
            body.append("Higher is better." if met.higher_is_better
                        else "Lower is better.")
        if syn:
            body.append(f"Also called: {', '.join(syn)}.")

        # The DOCUMENT is what gets embedded, so it leads with the words someone
        # would actually type. The body is what the model reads back.
        doc = f"{met.label} {mid} formula definition how is it calculated " \
              f"{' '.join(syn)}"
        out.append({"id": _cid("metric", mid), "document": doc,
                    "text": " ".join(body),
                    "meta": {"tier": "metric", "ref": mid, "term": met.label,
                             "formula": f"({num}) / nullif({den}, 0)" if den else num,
                             "authoritative": True}})
    return out


def glossary_chunks() -> list[dict]:
    """The catalogue's own glossary — already the product's vocabulary."""
    from paylens2.core.catalogue import _raw
    out = []
    for g in _raw().get("glossary", []) or []:
        says = " ".join(str(g.get("says", "")).split())
        if not says:
            continue
        out.append({"id": _cid("glossary", g["id"]), "document":
                    f"{g['term']} what is meaning definition explain",
                    "text": says,
                    "meta": {"tier": "glossary", "ref": g["id"],
                             "term": g["term"], "authoritative": True}})
    return out


def industry_chunks() -> list[dict]:
    """Sourced context. Never the definition — see the module docstring."""
    if not INDUSTRY_YAML.exists():
        log.warning("no industry.yaml at %s", INDUSTRY_YAML)
        return []
    data = yaml.safe_load(INDUSTRY_YAML.read_text(encoding="utf-8")) or {}
    out = []
    for e in data.get("entries", []) or []:
        says = " ".join(str(e.get("says", "")).split())
        contrast = " ".join(str(e.get("contrast", "")).split())
        text = says + (f" HOW PAYLENS DIFFERS: {contrast}" if contrast else "")
        out.append({"id": _cid("industry", e["id"]),
                    "document": f"{e['term']} {' '.join(e.get('tags', []))}",
                    "text": text,
                    "meta": {"tier": "industry", "ref": e["id"],
                             "term": e["term"], "authoritative": False,
                             "sources": " ".join(e.get("sources", []) or [])}})
    return out


def all_chunks() -> list[dict]:
    return metric_chunks() + glossary_chunks() + industry_chunks()


def build(show: bool = False) -> int:
    chunks = all_chunks()
    if show:
        for c in chunks:
            print(f"\n── [{c['meta']['tier']}] {c['meta']['term']} "
                  f"({c['meta']['ref']})")
            print(f"   embed: {c['document']}")
            print(f"   text : {c['text'][:400]}")
        return len(chunks)

    from backend.kb import store
    col = store.collection(COLLECTION)
    # Upsert by stable id, then drop anything no longer generated: a metric
    # removed from views.yaml must not leave its explanation behind to be
    # retrieved for a question about a metric that no longer exists.
    col.upsert(ids=[c["id"] for c in chunks],
               documents=[c["document"] for c in chunks],
               metadatas=[{**c["meta"], "text": c["text"]} for c in chunks])
    live = {c["id"] for c in chunks}
    stale = [i for i in (col.get(include=[]).get("ids") or []) if i not in live]
    if stale:
        col.delete(ids=stale)
        log.info("removed %d stale chunk(s)", len(stale))
    return len(chunks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true",
                    help="print the chunks without embedding them")
    args = ap.parse_args()
    n = build(show=args.show)
    if not args.show:
        from backend.kb import store
        print(f"{n} chunks in '{COLLECTION}' "
              f"({store.collection(COLLECTION).count()} total)")
