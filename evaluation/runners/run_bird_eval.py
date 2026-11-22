"""
Track 1 — BIRD mini-dev.

Preflights which databases are actually on disk and evaluates only those, so
the harness is correct at 0, 3 or 11 databases. The header records how many
were used: a 3-database EX is not a 500-question EX and must not be reported
as one.

Gold SQL is executed in SQLite, its native dialect, and timed — which is what
VES needs. Predicted SQL is validated in DuckDB (the guard's dialect) and then
transpiled and executed in SQLite too, so both sides of every EX comparison
ran in the same engine.

Usage:
    python -m evaluation.runners.run_bird_eval --smoke
    python -m evaluation.runners.run_bird_eval --limit 100 --workers 3
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("V2_CACHE", "false")
os.environ.setdefault("DUCKDB_MEMORY_LIMIT", "1GB")

from evaluation import config  # noqa: E402
from evaluation.datasets import bird_loader  # noqa: E402
from evaluation.metrics import error_breakdown, ex_accuracy, healing  # noqa: E402
from evaluation.metrics import latency, ves  # noqa: E402
from evaluation.runners.run_own_eval import (already_done, append_jsonl,  # noqa: E402
                                             write_json)

_stop = threading.Event()


def exec_gold(db_id: str, gold_sql: str) -> tuple[list[dict], float, str]:
    """(rows, ms, error). Gold runs in SQLite — its own dialect, untouched."""
    conn = None
    try:
        conn = bird_loader.gold_conn(db_id)
        t0 = time.time()
        rows = conn.execute(gold_sql).fetchall()
        return [dict(r) for r in rows], (time.time() - t0) * 1000, ""
    except Exception as exc:  # noqa: BLE001
        return [], 0.0, f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="PayLens v2 BIRD evaluation")
    ap.add_argument("--pipeline", default="v2", help="comma list: v2,baseline")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-db", default="")
    ap.add_argument("--workers", type=int, default=config.MAX_WORKERS)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--resume", default="")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "local", "hub", "datasets"])
    ap.add_argument("--smoke", action="store_true", help="5 questions, 1 db")
    args = ap.parse_args()

    args.run_id = (args.resume or args.run_id
                   or f"bird_{datetime.now():%Y%m%d_%H%M%S}")
    out_dir = config.RESULTS_DIR / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "bird_raw.jsonl"

    present = bird_loader.available_dbs()
    print(f"BIRD databases found in {config.BIRD_DB_ROOT}:")
    if not present:
        print("  (none)\n")
        print("Download the Mini-Dev databases from https://bird-bench.github.io/")
        print("and extract them so that each database sits at:")
        print(f"  {config.BIRD_DB_ROOT}/<db_id>/<db_id>.sqlite")
        print("\nThe questions and gold SQL load separately and need no "
              "download; everything else in the harness runs without this.")
        return 2
    for d in present:
        print(f"  {d}")
    print()

    try:
        questions = bird_loader.load_questions(source=args.source)
    except Exception as exc:  # noqa: BLE001
        print(f"could not load questions: {exc}")
        return 2

    want = ({d.strip() for d in args.only_db.split(",") if d.strip()}
            or set(present))
    recs = [q for q in questions if q["db_id"] in want and q["db_id"] in present]
    if args.smoke:
        recs = recs[:5]
    elif args.limit:
        recs = recs[:args.limit]
    for i, r in enumerate(recs, start=1):
        r["id"] = f"b{i:04d}"

    skipped = len(questions) - len([q for q in questions if q["db_id"] in present])
    print(f"{len(questions)} questions loaded, {len(recs)} evaluated, "
          f"{skipped} skipped (database not present)")

    pipelines = []
    if "v2" in args.pipeline:
        from evaluation.pipelines.v2_bird_pipeline import V2BirdPipeline
        pipelines.append(V2BirdPipeline())
    if "baseline" in args.pipeline:
        from evaluation.pipelines.bird_baseline import BirdBaselinePipeline
        pipelines.append(BirdBaselinePipeline())

    done = already_done(raw_path) if args.resume else set()
    env = {"run_id": args.run_id,
           "started": datetime.now().isoformat(timespec="seconds"),
           "n_questions_loaded": len(questions), "n_evaluated": len(recs),
           "n_skipped_no_db": skipped, "databases_present": present,
           "n_databases": len(set(r["db_id"] for r in recs)),
           "pipelines": [p.name for p in pipelines],
           "dialect": "sqlite (validated in duckdb, executed in sqlite)",
           "checks_skipped": sorted(
               __import__("evaluation.datasets.bird_adapter",
                          fromlist=["x"]).BIRD_SKIP),
           "result_aware_healing": False}
    write_json(out_dir / "run_env.json", env)

    results: dict[str, list] = {p.name: [] for p in pipelines}
    counter = {"n": 0}
    total = len(recs) * len(pipelines) - len(done)

    def on_sigint(signum, frame):  # noqa: ARG001
        if not _stop.is_set():
            print("\n  interrupted — saving what finished\n")
            _stop.set()

    signal.signal(signal.SIGINT, on_sigint)

    def run_one(pipe, rec) -> None:
        if _stop.is_set() or (rec["id"], pipe.name) in done:
            return
        res = pipe.run(rec)
        gold_rows, gold_ms, gold_err = exec_gold(rec["db_id"], rec["gold_sql"])
        res.gold_rows = gold_rows
        res.gold_exec_time_ms = gold_ms
        if gold_err:
            # A gold query that will not run is not evidence about the model.
            # It is excluded from EX with the count stated rather than scored
            # as a miss the model did not earn.
            res.record["gold_exec_failed"] = gold_err
        results[pipe.name].append(res)
        append_jsonl(raw_path, res.to_json())
        counter["n"] += 1
        ex = ex_accuracy.ex_score(res.rows or [], gold_rows) if res.success else 0.0
        print(f"  [{counter['n']:4}/{total}] "
              f"{'EX' if ex else '--'} {res.question_id} {rec['db_id'][:20]:20} "
              f"{res.latency_ms:7.0f}ms  {res.question[:44]}")
        if counter["n"] % config.CHECKPOINT_EVERY == 0:
            write_json(out_dir / "checkpoint.json",
                       {"completed": counter["n"], "of": total})

    t0 = time.time()
    for pipe in pipelines:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for f in [pool.submit(run_one, pipe, r) for r in recs]:
                try:
                    f.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"  [worker error] {type(exc).__name__}: {exc}")
    elapsed = time.time() - t0

    metrics: dict = {}
    for name, rs in results.items():
        if not rs:
            continue
        scorable = [r for r in rs if not r.record.get("gold_exec_failed")]
        m = ex_accuracy.compute_ex_metrics(scorable)
        m["ves"] = ves.compute_ves_bird(scorable)
        m.update(healing.compute_healing_metrics(rs))
        lat = latency.compute_latency_percentiles(rs)
        m.update({"p50_ms": lat["p50"], "p95_ms": lat["p95"]})
        m["error_breakdown"] = error_breakdown.compute_error_breakdown(rs)
        m["n_gold_exec_failed"] = len(rs) - len(scorable)
        m["n_databases"] = env["n_databases"]
        m["ex_by_difficulty"] = {
            d: ex_accuracy.compute_ex_metrics(
                [r for r in scorable if r.difficulty == d])["ex"]
            for d in sorted({r.difficulty for r in scorable})}
        metrics[name] = m

    env["ended"] = datetime.now().isoformat(timespec="seconds")
    env["elapsed_s"] = round(elapsed, 1)
    env["partial"] = _stop.is_set()
    write_json(out_dir / "run_env.json", env)
    write_json(out_dir / "bird_metrics.json", metrics)

    from evaluation.runners import report
    report.print_bird_report(metrics)
    print(f"\n{counter['n']} results in {elapsed / 60:.1f} min -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
