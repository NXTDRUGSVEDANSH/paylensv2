"""
The G-Eval pass, as a separate step.

WHY IT IS NOT INSIDE THE MEASUREMENT RUN
----------------------------------------
Judging is re-runnable; measuring is not. The 800 pipeline records cost about
three hours of model calls, and the first judge pass failed on 26% of what it
was handed — if that pass had been welded to the measurement, fixing the judge
would have meant paying for the measurement again. Reading the finished JSONL
instead means the judge can be re-run, re-modelled or re-prompted for the cost
of the judge alone.

THE TOKEN BUDGET IS THE WHOLE FIX
---------------------------------
44 of the first 47 failures were the same thing: the judge model is a reasoner,
the G-Eval prompt asks for a chain-of-thought sentence per criterion, and it
spent its entire 6,000-token budget thinking and returned empty content.
`provider.call_text` takes `max_tokens` from the route, not from the caller,
so the budget is raised through the environment before `backend.config` is
imported — which is why the two `os.environ` lines below sit above the imports
and must stay there.

Both pipelines are judged with the same prompt and the same model, because the
comparison is the point.

    python -m evaluation.runners.run_judge --run full_400
    python -m evaluation.runners.run_judge --run full_400 --limit 4   # probe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Must precede any backend.config import — see the module docstring.
os.environ.setdefault("PLANNER_MAX_TOKENS", "20000")
os.environ.setdefault("LLM_MAX_TOKENS", "20000")
os.environ.setdefault("V2_CACHE", "false")

from evaluation import config  # noqa: E402

_lock = threading.Lock()


def load_rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="G-Eval over a finished run")
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", default=config.JUDGE_MODEL)
    ap.add_argument("--workers", type=int, default=config.MAX_WORKERS)
    ap.add_argument("--limit", type=int, default=0,
                    help="probe mode: judge only this many per pipeline")
    ap.add_argument("--pipeline", default="",
                    help="restrict to one pipeline name")
    ap.add_argument("--redo-errors", action="store_true",
                    help="re-judge entries that previously errored")
    args = ap.parse_args()

    config.JUDGE_MODEL = args.model
    out_dir = config.RESULTS_DIR / args.run
    raw = out_dir / "own_raw.jsonl"
    if not raw.exists():
        raise SystemExit(f"no results at {raw}")
    jpath = out_dir / "judge_raw.jsonl"

    # Resume: skip what already has a usable score. A previous error is
    # re-tried only when asked, so a genuine refusal is not retried forever.
    done: set[tuple] = set()
    if jpath.exists():
        for j in load_rows(jpath):
            key = (j.get("question_id"), j.get("pipeline"))
            if j.get("llm_judge_score") is not None or not args.redo_errors:
                done.add(key)

    rows = load_rows(raw)
    # Runnable queries only, as specified: a judgment of an answer that never
    # ran would be a judgment of an error message.
    todo = [r for r in rows
            if r.get("success") and (r.get("sql") or "").strip()
            and (r.get("question_id"), r.get("pipeline_name")) not in done
            and (not args.pipeline or r.get("pipeline_name") == args.pipeline)]
    if args.limit:
        byp: dict[str, list] = {}
        for r in todo:
            byp.setdefault(r["pipeline_name"], []).append(r)
        todo = [r for v in byp.values() for r in v[:args.limit]]

    counts: dict[str, int] = {}
    for r in todo:
        counts[r["pipeline_name"]] = counts.get(r["pipeline_name"], 0) + 1
    print(f"judge {args.model}")
    print(f"  max_tokens {os.environ['PLANNER_MAX_TOKENS']} · workers {args.workers}")
    print(f"  {len(todo)} to judge {counts}  ({len(done)} already done)\n")
    if not todo:
        return 0

    from evaluation.metrics.llm_judge import judge_result
    from evaluation.pipelines.base import EvalResult
    from evaluation.pipelines.naive_baseline import raw_ddl
    from paylens2.core.model import schema_card
    from dataclasses import fields as dc_fields

    keep = {f.name for f in dc_fields(EvalResult)}

    # THE SCHEMA HINT MUST MATCH WHAT EACH PIPELINE WAS ACTUALLY GIVEN.
    #
    # Handing PayLens's schema card to both looks like a fair comparison and
    # is not. That card states the raw fact_* and dim_* tables are blocked, so
    # the judge scored every baseline answer 0.0 on schema alignment, reason:
    # "uses the blocked fact_transactions table instead of the allowed
    # v_attempts view". The baseline was never shown the views — querying the
    # raw tables is its whole design. That measured obedience to PayLens's
    # allowlist, not whether the query fitted the question, and it would have
    # put a fabricated 0.0 in the report.
    #
    # Judged against its own schema the baseline can score honestly, and the
    # gap that remains is a real difference in query quality rather than an
    # artefact of which prompt each system was handed.
    cards = {"paylens_v2": schema_card(), "naive_baseline": raw_ddl()}
    default_card = schema_card()
    n = {"done": 0, "err": 0}
    t0 = time.time()

    def one(row: dict) -> None:
        # Nothing in here may raise. `pool.map` re-raises at iteration, which
        # kills the whole pass — a 217-judgment run died at 49 that way, with
        # no traceback in the log because the exception surfaced after the
        # worker had already moved on. One bad judgment is a null score, not
        # the end of the run.
        try:
            res = EvalResult(**{k: v for k, v in row.items() if k in keep})
            j = judge_result(res, cards.get(row.get("pipeline_name"),
                                            default_card))
        except Exception as exc:  # noqa: BLE001
            j = {"llm_judge_score": None,
                 "judge_error": f"harness: {type(exc).__name__}: {exc}"[:300]}
        j["question_id"] = row.get("question_id")
        j["pipeline"] = row.get("pipeline_name")
        with _lock:
            with jpath.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(j, ensure_ascii=False, default=str) + "\n")
                fh.flush()
            n["done"] += 1
            if j.get("judge_error"):
                n["err"] += 1
            if n["done"] % 25 == 0 or n["done"] == len(todo):
                rate = n["done"] / max(time.time() - t0, 1) * 60
                print(f"  {n['done']:4}/{len(todo)}  errors {n['err']:3}  "
                      f"{rate:.0f}/min")

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(one, todo))
    except KeyboardInterrupt:
        print("\n  interrupted — everything judged so far is already on disk")

    print(f"\n  {n['done']} judged, {n['err']} errors "
          f"({n['err']/max(n['done'],1)*100:.1f}%) in "
          f"{(time.time()-t0)/60:.1f} min")

    # Aggregate both pipelines so the result is visible without rebuilding.
    from evaluation.metrics.llm_judge import aggregate_judge_scores
    allj = load_rows(jpath)
    for pipe in sorted({j.get("pipeline") for j in allj if j.get("pipeline")}):
        agg = aggregate_judge_scores([j for j in allj if j.get("pipeline") == pipe])
        print(f"\n  {pipe}")
        for k in ("mean_temporal_precision", "mean_schema_alignment",
                  "mean_faithfulness", "mean_completeness",
                  "mean_llm_judge_score"):
            v = agg.get(k)
            print(f"    {k.replace('mean_',''):22} "
                  f"{'n/a' if v is None else f'{v*100:.1f}'}")
        print(f"    judged / errors        {agg['n_judged']} / {agg['n_judge_errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
