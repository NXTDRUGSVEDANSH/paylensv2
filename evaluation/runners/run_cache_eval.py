"""
The NLQ→SQL cache, measured on its own.

WHY IT IS NOT PART OF THE MAIN RUN
----------------------------------
A cache hit skips call 1 and nothing else. The author stage drops by roughly
three orders of magnitude while synthesis, execution, the guard and the
data-quality probes all still run — so the stage speedup and the end-to-end
speedup differ by about 100x, and quoting the stage figure as if it were the
end-to-end one is the inflated claim this split exists to avoid.

It also cannot share a run with the latency table: a hit landing on an unknown
subset of 400 questions turns one latency distribution into two reported as
one, and P50 becomes a function of question ordering.

PROMOTION MEANS THREE PASSES, NOT TWO
-------------------------------------
`CACHE_PROMOTE_AFTER` is 2: a query must produce an equivalent query twice
before it is marked verified and eligible for reuse. So pass 1 records it, pass
2 promotes it, and pass 3 is the first that can actually be served. Measuring
pass 1 against pass 2 would report a miss as if it were the steady state.

Single-threaded on purpose — the L0 layer is a process-local LRU and
concurrent passes would race each other's promotion counters.

Usage:
    python -m evaluation.runners.run_cache_eval --n 30
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The opposite of the main run: here the cache is the thing under test.
os.environ["V2_CACHE"] = "true"

from evaluation import config  # noqa: E402
from evaluation.runners.run_own_eval import load_bank, write_json  # noqa: E402

MEASURE_BUCKETS = ("measurement_single", "breakdown", "trend", "rank", "compare")


def main() -> int:
    ap = argparse.ArgumentParser(description="NLQ->SQL cache speedup")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--today", default=config.EVAL_TODAY.isoformat())
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()

    today = date.fromisoformat(args.today)
    run_id = args.run_id or f"cache_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir = config.RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    from paylens2 import config as v2cfg
    if not v2cfg.USE_CACHE:
        raise SystemExit("the cache is disabled; this run would measure nothing")

    bank = load_bank(config.OWN_QUERIES_PATH)
    # Only the measuring buckets: advise, explain, clarify and refuse never
    # author a query, so there is nothing for the cache to store.
    recs = [r for r in bank if r["bucket"] in MEASURE_BUCKETS
            and r["role"] == "cxo"][:args.n]
    print(f"cache run {run_id}: {len(recs)} questions x {args.repeats} passes")
    print(f"  promote_after={v2cfg.CACHE_PROMOTE_AFTER}  "
          f"ttl={v2cfg.CACHE_TTL_S}s  size={v2cfg.CACHE_SIZE}")
    print(f"  pass {v2cfg.CACHE_PROMOTE_AFTER + 1} is the first that can be "
          f"served\n")

    from evaluation.pipelines.v2_pipeline import V2Pipeline
    # use_kb=True here: write-back is what records the cache entry, and
    # disabling it would mean measuring a cache that is never populated.
    pipe = V2Pipeline(today=today, use_kb=True)

    passes: list[list[dict]] = []
    for p in range(1, args.repeats + 1):
        rows = []
        for i, rec in enumerate(recs, start=1):
            res = pipe.run(rec)
            rows.append({"id": rec["id"], "cache_hit": res.cache_hit,
                         "cache_state": res.cache_state,
                         "author_ms": (res.timings_ms or {}).get("author", 0),
                         "total_ms": res.latency_ms, "ok": res.success})
            print(f"  pass {p}  [{i:3}/{len(recs)}] "
                  f"{'HIT ' + res.cache_hit if res.cache_hit else 'miss':8} "
                  f"author {(res.timings_ms or {}).get('author', 0):7.0f}ms  "
                  f"total {res.latency_ms:7.0f}ms  {rec['q'][:40]}")
        passes.append(rows)
        hits = sum(1 for r in rows if r["cache_hit"])
        print(f"  -- pass {p}: {hits}/{len(rows)} hits\n")

    cold, warm = passes[0], passes[-1]

    def p50(rows, key):
        vals = [r[key] for r in rows if r["ok"]]
        return round(st.median(vals), 1) if vals else None

    metrics = {
        "n_questions": len(recs), "n_passes": args.repeats,
        "promote_after": v2cfg.CACHE_PROMOTE_AFTER,
        "cold_author_p50_ms": p50(cold, "author_ms"),
        "warm_author_p50_ms": p50(warm, "author_ms"),
        "cold_total_p50_ms": p50(cold, "total_ms"),
        "warm_total_p50_ms": p50(warm, "total_ms"),
        "hit_rate_final_pass": round(
            sum(1 for r in warm if r["cache_hit"]) / len(warm), 4),
        "promoted_rate_final_pass": round(
            sum(1 for r in warm if r["cache_state"] == "promoted")
            / len(warm), 4),
        "per_pass_hit_rate": [
            round(sum(1 for r in rows if r["cache_hit"]) / len(rows), 4)
            for rows in passes],
    }
    ca, wa = metrics["cold_author_p50_ms"], metrics["warm_author_p50_ms"]
    ct, wt = metrics["cold_total_p50_ms"], metrics["warm_total_p50_ms"]
    metrics["author_stage_speedup"] = round(ca / wa, 1) if ca and wa else None
    metrics["end_to_end_speedup"] = round(ct / wt, 2) if ct and wt else None

    write_json(out_dir / "cache_metrics.json", metrics)
    (out_dir / "cache_raw.json").write_text(
        json.dumps(passes, indent=2, default=str), encoding="utf-8")

    print("  " + "=" * 56)
    print(f"  author stage p50  : {ca:,.0f}ms cold -> {wa:,.0f}ms warm   "
          f"{metrics['author_stage_speedup']}x")
    print(f"  end to end p50    : {ct:,.0f}ms cold -> {wt:,.0f}ms warm   "
          f"{metrics['end_to_end_speedup']}x")
    print(f"  hit rate by pass  : {metrics['per_pass_hit_rate']}")
    print("  " + "=" * 56)
    print(f"\nThe two speedups differ because a hit skips call 1 and nothing "
          f"else.\nwrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
