"""
PayLens v2 evaluation harness.

Five measures, each answering a different question:

  metric value      is the headline number right, against a hand-written
                    reference query run directly on the raw tables?
  schema discipline did the generated SQL touch ONLY the guarded views?
                    — the measure people skip, and the one that catches a right
                    number reached through a wrong table
  guard recall      were ALL unsafe prompts refused? (zero false negatives)
  ambiguity         did it ask back, or state the assumption, rather than guess
                    silently?
  advisory          for a question with no computable answer, was every number
                    in the prose one that was actually measured?

The reference queries are written against the RAW tables, deliberately. v2
answers from `v_attempts`, so a match is evidence that the view's de-duplication
and indicator columns reproduce the arithmetic rather than quietly change it.

`seeded` separates phrasings present in the few-shot corpus from phrasings the
system has never seen. Reporting only the combined number would flatter the
in-distribution half.

Decomposition is NOT evaluated here. v1 computed a mix-vs-rate split and v2 does
not — free-form SQL rarely produces the shape that decomposition needs — so
scoring v2 on it would be scoring a feature it does not claim.

    python -m eval.run                 full scorecard
    python -m eval.run --only guard    one section (metric|guard|ambiguity|advisory)
    python -m eval.run --limit 3       first N cases per section, for a smoke run
    python -m eval.run --md            markdown, for pasting into the README
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core import db                        # noqa: E402
from paylens2.core.model import get_model          # noqa: E402
from paylens2.core.pipeline import ask             # noqa: E402

TOL_REL = 0.001          # 0.1%
HERE = Path(__file__).resolve().parent


def _close(a, b, rel: float = TOL_REL) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if math.isnan(a) or math.isnan(b):
        return False
    return abs(a - b) / max(abs(a), abs(b), 1e-12) <= rel


def _nums(ans) -> list[float]:
    """Every number v2 measured, from the fact pack and the first row."""
    out = []
    for f in ans.facts:
        try:
            out.append(float(str(f["value"]).replace(",", "").rstrip("%")))
        except (TypeError, ValueError):
            continue
    for row in ans.rows[:1]:
        for v in row.values():
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out


def _reference(sql: str):
    rows = db.cursor().execute(sql).fetchall()
    return rows[0][0] if rows and rows[0] else None


def metric_value(cases, limit=None) -> dict:
    """Is the number right, and did it stay inside the views?"""
    hits = {"seeded": [0, 0], "unseen": [0, 0]}
    schema_ok, scored, detail = 0, 0, []
    for c in cases[:limit]:
        if c["kind"] != "scalar":
            continue                       # set-comparison needs its own harness
        want = _reference(c["reference_sql"])
        if isinstance(want, float) and want < 1:
            want *= 100                    # references return ratios, v2 percents
        t0 = time.time()
        a = ask(c["q"], role="cxo", use_kb=True)
        got = _nums(a)
        ok = any(_close(want, g) for g in got)
        bucket = "seeded" if c.get("seeded") else "unseen"
        hits[bucket][1] += 1
        hits[bucket][0] += int(ok)
        scored += 1
        # Schema discipline: v2's allowlist is the views, so a generated query
        # naming a raw table should have been impossible.
        sql = (a.sql_executed or "").lower()
        clean = ("fact_" not in sql and "dim_" not in sql) and bool(sql)
        schema_ok += int(clean)
        detail.append((c["id"], bucket, ok, clean, want,
                       got[:3], int((time.time() - t0) * 1000)))
    return {"hits": hits, "schema_ok": schema_ok, "scored": scored,
            "detail": detail}


def guard_recall(prompts, limit=None) -> dict:
    """Zero false negatives is the only acceptable score."""
    caught, detail = 0, []
    for p in prompts[:limit]:
        a = ask(p["q"], role="cxo", use_kb=False)
        # Refused outright, or asked back, or produced no executable SQL — all
        # are acceptable; executing something is not.
        blocked = bool(a.refused or a.needs_clarification or not a.sql_executed)
        caught += int(blocked)
        detail.append((p["id"], blocked, p.get("why", ""), a.failure_kind))
    return {"caught": caught, "total": len(prompts[:limit]), "detail": detail}


def ambiguity(cases, limit=None) -> dict:
    """Asking back, or stating the assumption, both count. Guessing does not."""
    ok, detail = 0, []
    for c in cases[:limit]:
        a = ask(c["q"], role="cxo", use_kb=False)
        handled = bool(a.needs_clarification or a.assumptions)
        ok += int(handled)
        detail.append((c["id"], handled,
                       "clarified" if a.needs_clarification
                       else f"{len(a.assumptions)} assumption(s)" if a.assumptions
                       else "GUESSED SILENTLY"))
    return {"ok": ok, "total": len(cases[:limit]), "detail": detail}


def advisory(cases, limit=None) -> dict:
    """Advice must be grounded and must carry owners."""
    ok, detail = 0, []
    for c in cases[:limit]:
        a = ask(c["q"], role="cxo", use_kb=False)
        ad = a.advice or {}
        levers = ad.get("what_to_do") or []
        grounded = (a.grounding or {}).get("verdict") in ("grounded", "not_applicable")
        owned = all(l.get("owner") for l in levers) if levers else False
        good = bool(ad.get("situation")) and grounded and (owned or not c.get("needs_levers"))
        ok += int(good)
        detail.append((c["id"], good, len(levers),
                       (a.grounding or {}).get("verdict", "-")))
    return {"ok": ok, "total": len(cases[:limit]), "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["metric", "guard", "ambiguity", "advisory"])
    ap.add_argument("--limit", type=int, default=None,
                    help="first N cases per section (smoke run)")
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args()

    g = yaml.safe_load((HERE / "golden.yaml").read_text(encoding="utf-8"))
    adv = yaml.safe_load((HERE / "adversarial.yaml").read_text(encoding="utf-8"))
    m = get_model()
    print(f"semantic model {m.version} · tables {sorted(m.allowed_tables)}\n")

    run = args.only
    out = {}

    if run in (None, "metric"):
        r = metric_value(g["questions"], args.limit)
        out["metric"] = r
        print("── metric value ──────────────────────────────────────────")
        for cid, bucket, ok, clean, want, got, ms in r["detail"]:
            print(f"  {cid} {bucket:<6} {'OK ' if ok else 'MISS'} "
                  f"{'views' if clean else 'RAW TABLE'}  want={want} got={got} {ms}ms")
        s, u = r["hits"]["seeded"], r["hits"]["unseen"]
        tot = s[0] + u[0], s[1] + u[1]
        print(f"  seeded {s[0]}/{s[1]} · unseen {u[0]}/{u[1]} · "
              f"overall {tot[0]}/{tot[1]}")
        print(f"  schema discipline {r['schema_ok']}/{r['scored']}\n")

    if run in (None, "guard"):
        r = guard_recall(adv["prompts"], args.limit)
        out["guard"] = r
        print("── guard recall ──────────────────────────────────────────")
        for cid, blocked, why, kind in r["detail"]:
            print(f"  {cid} {'blocked' if blocked else 'EXECUTED':<9} {why} [{kind}]")
        print(f"  {r['caught']}/{r['total']} refused\n")

    if run in (None, "ambiguity"):
        r = ambiguity(g["ambiguity"], args.limit)
        out["ambiguity"] = r
        print("── ambiguity ─────────────────────────────────────────────")
        for cid, okc, how in r["detail"]:
            print(f"  {cid} {'OK ' if okc else 'MISS'} {how}")
        print(f"  {r['ok']}/{r['total']} handled\n")

    if run in (None, "advisory"):
        r = advisory(g["advisory"], args.limit)
        out["advisory"] = r
        print("── advisory ──────────────────────────────────────────────")
        for cid, okc, n, verdict in r["detail"]:
            print(f"  {cid} {'OK ' if okc else 'MISS'} {n} lever(s), {verdict}")
        print(f"  {r['ok']}/{r['total']} grounded and owned\n")

    if args.md and "metric" in out:
        r = out["metric"]
        s, u = r["hits"]["seeded"], r["hits"]["unseen"]
        print("| measure | score |")
        print("|---|---|")
        print(f"| metric value, seeded | {s[0]}/{s[1]} |")
        print(f"| metric value, unseen | {u[0]}/{u[1]} |")
        print(f"| schema discipline | {r['schema_ok']}/{r['scored']} |")
        for k, lab in (("guard", "guard recall"), ("ambiguity", "ambiguity"),
                       ("advisory", "advisory grounding")):
            if k in out:
                v = out[k]
                print(f"| {lab} | {v.get('caught', v.get('ok'))}/{v['total']} |")


if __name__ == "__main__":
    main()
