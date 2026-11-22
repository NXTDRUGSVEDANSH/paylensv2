"""
Track 2 — the 400-question own-query evaluation.

RESUMABILITY IS NOT OPTIONAL HERE
---------------------------------
400 questions at ~22s each is over two hours per pipeline, against a
rate-limited provider. A run that loses everything to a 429 storm or a closed
laptop is a run nobody will start. So: every completed record is appended to a
JSONL immediately, the key is the bank's stable id rather than a loop index,
`--resume` skips what is already there, and Ctrl+C writes the metrics over
whatever finished instead of discarding it.

WHY THE CACHE IS OFF
--------------------
A cache hit drops the author call from ~11,700ms to ~15ms. Left on, the
latency table would be two distributions reported as one, and P50 would be a
function of the order the bank happened to be in. Cache behaviour is measured
separately, as its own metric, on its own run.

Usage:
    python -m evaluation.runners.run_own_eval --smoke
    python -m evaluation.runners.run_own_eval --pipeline v2 --workers 3
    python -m evaluation.runners.run_own_eval --resume own_20261122_041500
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
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The cache must be off BEFORE paylens2.config is imported, because it reads the
# environment at import time. Setting it afterwards is a no-op that silently
# produces a mixed run.
os.environ.setdefault("V2_CACHE", "false")
# Four concurrent scans of a 776k-row view against a 256MB budget spill to
# disk. Correctness is unaffected; the run just gets slower for no reason.
os.environ.setdefault("DUCKDB_MEMORY_LIMIT", "1GB")

from evaluation import config  # noqa: E402
from evaluation.metrics import error_breakdown, ex_accuracy, healing, latency  # noqa: E402
from evaluation.metrics import paylens_metrics as pm  # noqa: E402
from evaluation.metrics import ves  # noqa: E402

_write_lock = threading.Lock()
_stop = threading.Event()


# ─────────────────────────────────────────────────────────────────────────────
def load_bank(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"bank not found: {path}\n"
                         f"run: python -m evaluation.datasets.bank_build")
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def already_done(jsonl: Path) -> set[tuple[str, str]]:
    """(question_id, pipeline) pairs recorded in a previous run of this id."""
    done: set[tuple[str, str]] = set()
    if not jsonl.exists():
        return done
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # A half-written final line from a hard kill. Skipping it means the
            # record is simply re-run, which is the safe direction.
            continue
        done.add((row.get("question_id", ""), row.get("pipeline_name", "")))
    return done


def append_jsonl(path: Path, row: dict) -> None:
    with _write_lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def write_json(path: Path, data: dict) -> None:
    """Atomic: a checkpoint half-written by a kill is worse than none."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
def run_env(args, bank: list[dict]) -> dict:
    """Everything needed to say what produced these numbers."""
    import hashlib
    import subprocess

    from backend import config as base
    from backend.core import db
    from paylens2 import config as v2cfg
    from paylens2.core.model import get_model

    def sh(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=str(ROOT), timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    kb_count = None
    try:
        from backend.kb import store
        if store.available():
            kb_count = store.get_kb().count()
    except Exception:  # noqa: BLE001
        pass

    bank_sha = hashlib.sha256(
        config.OWN_QUERIES_PATH.read_bytes()).hexdigest()[:16]
    try:
        watermark = db.data_watermark()
    except Exception:  # noqa: BLE001
        watermark = ""

    return {
        "run_id": args.run_id,
        "started": datetime.now().isoformat(timespec="seconds"),
        "eval_today": args.today.isoformat(),
        "n_bank": len(bank),
        "bank_sha256_16": bank_sha,
        "git_sha": sh("git", "rev-parse", "--short", "HEAD"),
        "semantic_model": get_model().version,
        "data_watermark": watermark,
        "author_model": v2cfg.AUTHOR_MODEL,
        "synth_model": v2cfg.SYNTH_MODEL,
        "judge_model": config.JUDGE_MODEL,
        "v2_cache_enabled": v2cfg.USE_CACHE,
        "v2_use_kb_global": v2cfg.USE_KB,
        "write_back_enabled": False,
        "prompt_version": v2cfg.PROMPT_VERSION,
        "kb_count_before": kb_count,
        "max_heal_calls": v2cfg.MAX_HEAL_CALLS,
        "max_sql_rows": base.MAX_SQL_ROWS,
        "workers": args.workers,
        "pipelines": args.pipeline,
        "judge": not args.no_judge,
    }


# ─────────────────────────────────────────────────────────────────────────────
def build_pipelines(args):
    out = []
    if "v2" in args.pipeline:
        from evaluation.pipelines.v2_pipeline import V2Pipeline
        out.append(V2Pipeline(today=args.today, use_kb=False))
    if "baseline" in args.pipeline:
        from evaluation.pipelines.naive_baseline import NaiveBaselinePipeline
        out.append(NaiveBaselinePipeline(today=args.today))
    if not out:
        raise SystemExit(f"no known pipeline in {args.pipeline!r}")
    return out


def select(bank: list[dict], args) -> list[dict]:
    recs = bank
    if args.only:
        want = {b.strip() for b in args.only.split(",") if b.strip()}
        unknown = want - set(r["bucket"] for r in bank)
        if unknown:
            raise SystemExit(f"unknown bucket(s): {sorted(unknown)}")
        recs = [r for r in recs if r["bucket"] in want]
    if args.smoke:
        # One record per bucket, preferring a scoped role where the bucket has
        # one, so a smoke run exercises RBAC injection, a refusal, a
        # clarification and a decomposition rather than 20 easy measurements.
        picked, seen = [], set()
        for r in sorted(recs, key=lambda r: (r["bucket"], r["role"] == "cxo")):
            if r["bucket"] in seen:
                continue
            seen.add(r["bucket"])
            picked.append(r)
        recs = picked
    if args.limit:
        recs = recs[:args.limit]
    return recs


def partition_threads(recs: list[dict]) -> tuple[list[dict], list[list[dict]]]:
    """
    Multi-turn records cannot be fanned out.

    Turn 2's context is turn 1's SQL, so the thread has to run in order on one
    worker. Everything else is independent and parallelises freely.
    """
    singles = [r for r in recs if not r.get("conversation_id")]
    threads: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("conversation_id"):
            threads.setdefault(r["conversation_id"], []).append(r)
    grouped = [sorted(v, key=lambda r: r.get("turn") or 0)
               for v in threads.values()]
    return singles, grouped


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="PayLens v2 own-query evaluation")
    ap.add_argument("--pipeline", default="v2",
                    help="comma list: v2,baseline")
    ap.add_argument("--only", default="", help="comma list of buckets")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=config.MAX_WORKERS)
    ap.add_argument("--today", default=config.EVAL_TODAY.isoformat())
    ap.add_argument("--run-id", default="")
    ap.add_argument("--resume", default="")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-model", default=config.JUDGE_MODEL)
    ap.add_argument("--smoke", action="store_true",
                    help="one record per bucket, no judge")
    args = ap.parse_args()

    args.today = date.fromisoformat(args.today)
    if args.smoke:
        args.no_judge = True
    config.JUDGE_MODEL = args.judge_model
    args.run_id = (args.resume or args.run_id
                   or f"own_{datetime.now():%Y%m%d_%H%M%S}")

    out_dir = config.RESULTS_DIR / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "own_raw.jsonl"

    bank = load_bank(config.OWN_QUERIES_PATH)
    recs = select(bank, args)
    pipelines = build_pipelines(args)
    done = already_done(raw_path) if args.resume else set()

    env = run_env(args, bank)
    env["n_selected"] = len(recs)
    env["n_resumed"] = len(done)
    write_json(out_dir / "run_env.json", env)

    print(f"run {args.run_id}")
    print(f"  bank      {len(bank)} records, {len(recs)} selected")
    print(f"  pipelines {', '.join(p.name for p in pipelines)}")
    print(f"  today     {args.today}  (cache {'ON' if env['v2_cache_enabled'] else 'off'}"
          f", write-back off)")
    print(f"  workers   {args.workers}   judge: "
          f"{'off' if args.no_judge else config.JUDGE_MODEL}")
    if done:
        print(f"  resuming  {len(done)} already complete")
    print(f"  output    {out_dir}")
    print()

    results: dict[str, list] = {p.name: [] for p in pipelines}
    counter = {"n": 0}
    total = sum(1 for p in pipelines for r in recs
                if (r["id"], p.name) not in done)

    def record_done(pipe_name: str, res) -> None:
        results[pipe_name].append(res)
        append_jsonl(raw_path, res.to_json())
        counter["n"] += 1
        n = counter["n"]
        # `ok` is the wrong thing to print. A correct refusal of "how many
        # chargebacks did we have" sets ok=False — it is a success of the
        # system and the first smoke run displayed it as FAIL. What matters on
        # the console is whether the answer went where the bank expected.
        routed = res.action == res.expected_action
        flag = ("ok  " if res.success and routed else
                "REF " if routed else
                "MISS" if res.success else "FAIL")
        got = res.action or "-"
        arrow = got if routed else f"{res.expected_action}>{got}"
        print(f"  [{n:4}/{total}] {flag} {res.question_id} {res.bucket:19} "
              f"{arrow:22} {res.latency_ms:7.0f}ms  {res.question[:44]}")
        if n % config.CHECKPOINT_EVERY == 0:
            write_json(out_dir / "checkpoint.json",
                       {"completed": n, "of": total,
                        "at": datetime.now().isoformat(timespec="seconds")})

    def run_single(pipe, rec) -> None:
        if _stop.is_set() or (rec["id"], pipe.name) in done:
            return
        record_done(pipe.name, pipe.run(rec))

    def run_thread(pipe, group) -> None:
        """A multi-turn thread, in order, carrying its own history forward."""
        history: list[dict] = []
        for rec in group:
            if _stop.is_set():
                return
            if (rec["id"], pipe.name) in done:
                continue
            res = pipe.run({**rec, "conversation": list(history)})
            if res.sql:
                history.append({"question": rec["q"], "sql": res.sql})
            record_done(pipe.name, res)

    def on_sigint(signum, frame):  # noqa: ARG001
        if not _stop.is_set():
            print("\n  interrupted — finishing in-flight work and saving\n")
            _stop.set()

    signal.signal(signal.SIGINT, on_sigint)

    singles, threads = partition_threads(recs)
    t0 = time.time()
    for pipe in pipelines:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run_single, pipe, r) for r in singles]
            futures += [pool.submit(run_thread, pipe, g) for g in threads]
            for f in futures:
                try:
                    f.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"  [worker error] {type(exc).__name__}: {exc}")
    elapsed = time.time() - t0

    # ── judge, on runnable results only ─────────────────────────────────────
    if not args.no_judge and not _stop.is_set():
        from evaluation.metrics.llm_judge import aggregate_judge_scores, judge_result
        from paylens2.core.model import schema_card
        card = schema_card()
        for name, rs in results.items():
            judgeable = [r for r in rs if r.success and (r.sql or "").strip()]
            print(f"\n  judging {len(judgeable)} runnable results from {name} "
                  f"with {config.JUDGE_MODEL}")
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for r, j in zip(judgeable,
                                pool.map(lambda x: judge_result(x, card),
                                         judgeable)):
                    r.judge = j
                    append_jsonl(out_dir / "judge_raw.jsonl",
                                 {"question_id": r.question_id,
                                  "pipeline": name, **j})

    # ── metrics ─────────────────────────────────────────────────────────────
    metrics: dict = {}
    for name, rs in results.items():
        if not rs:
            continue
        measured = [r for r in rs if r.measured]
        m: dict = {}
        # Healing is scored over the runs that actually authored SQL. Its own
        # `n_total` key would otherwise overwrite the bank total with the
        # measured subset, and the report read "12 of 400" on a 15-record run.
        m.update(healing.compute_healing_metrics(measured or rs))
        m["n_measured"] = len(measured)
        m["n_total"] = len(rs)
        lat = latency.compute_latency_percentiles(rs)
        m.update({"p50_ms": lat["p50"], "p95_ms": lat["p95"],
                  "mean_ms": lat["mean"], "min_ms": lat["min"],
                  "max_ms": lat["max"]})
        m["latency_by_difficulty"] = latency.compute_latency_by_complexity(rs)
        vs = ves.compute_ves_static([r for r in rs if (r.sql or "").strip()])
        m.update({"mean_ves_static": vs["mean_ves"],
                  "n_ves_perfect": vs["n_perfect"],
                  "n_ves_penalised": vs["n_penalised"]})
        m["error_breakdown"] = error_breakdown.compute_error_breakdown(rs)
        m.update(ex_accuracy.compute_own_ex(rs))
        m.update(pm.compute_all(rs))
        judged = [r.judge for r in rs if r.judge]
        if judged:
            from evaluation.metrics.llm_judge import aggregate_judge_scores
            m.update(aggregate_judge_scores(judged))
        # Every metric again, split by whether the wording was one the KB was
        # seeded with. Reporting only the combined number flatters the
        # in-distribution half.
        m["by_phrasing"] = {
            ph: pm.compute_all([r for r in rs if r.phrasing == ph],
                               leak_test=False)
            for ph in ("seeded", "unseen")
            if any(r.phrasing == ph for r in rs)}
        m["by_bucket"] = {}
        for b in sorted({r.bucket for r in rs}):
            sub = [r for r in rs if r.bucket == b]
            m["by_bucket"][b] = {
                "n": len(sub),
                "success_rate": round(sum(1 for r in sub if r.success) / len(sub), 4),
                "action_routing_accuracy":
                    pm.action_routing(sub)["action_routing_accuracy"],
                "p50_ms": latency.compute_latency_percentiles(sub)["p50"],
            }
        metrics[name] = m

    env["ended"] = datetime.now().isoformat(timespec="seconds")
    env["elapsed_s"] = round(elapsed, 1)
    env["partial"] = _stop.is_set()
    try:
        from backend.kb import store
        if store.available():
            env["kb_count_after"] = store.get_kb().count()
            env["kb_unchanged"] = (env.get("kb_count_before")
                                   == env["kb_count_after"])
    except Exception:  # noqa: BLE001
        pass
    write_json(out_dir / "run_env.json", env)
    write_json(out_dir / "own_metrics.json", metrics)

    from evaluation.runners import report
    report.print_own_report(metrics)
    report.write_markdown(out_dir, env, own=metrics)
    report.write_csv(out_dir, results)
    print(f"\n{'PARTIAL — ' if _stop.is_set() else ''}"
          f"{counter['n']} results in {elapsed / 60:.1f} min -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
