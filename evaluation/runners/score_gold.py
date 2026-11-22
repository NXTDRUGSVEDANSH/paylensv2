"""
Execution accuracy on the gold subset, scored after the fact.

Runs against a completed run's `own_raw.jsonl` rather than inside the hot loop,
so EX can be added, corrected or re-scored without re-spending two hours of
model calls. The gold queries are executed once each and their result sets
cached for the comparison.

The 19 references already in `eval/golden.yaml` are reused as-is: they are
hand-written, were verified against the warehouse, and the pinned evaluation
date reproduces exactly the windows they assume. Anything beyond those goes in
`datasets/gold_sql.yaml` in the same shape.

Usage:
    python -m evaluation.runners.score_gold --run-id full_400
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation import config  # noqa: E402
from evaluation.metrics.ex_accuracy import ex_score_numeric  # noqa: E402


# References whose definition genuinely differs from v2's semantic model.
#
# These are not model errors and must not be scored as such: v2 reproduced its
# OWN declared formula exactly. They are disagreements between the two semantic
# models, which is a finding about the repo rather than about the pipeline, and
# burying it inside an EX number would hide it.
#
# EX is reported twice — including and excluding these — so neither reading is
# the only one on offer.
KNOWN_DIVERGENCES: dict[str, str] = {
    "g13": (
        "avg_ticket is defined differently in the two semantic models. "
        "semantic/semantic_model.yaml:185 (v1) filters to SUCCESS and "
        "amount > 0; paylens2/schema/views.yaml:145 (v2) is a plain "
        "avg(amount) over every attempt, so failed and pending attempts and "
        "the negative refund corrections are averaged in. v2 reproduced its "
        "own formula correctly; the two formulas disagree."),
    "g05": (
        "null handling, not arithmetic. Every value agrees exactly; the "
        "reference labels the 2.09% of attempts with no issuer as "
        "'(unknown issuer)' and v2 leaves the group NULL. Presentation, not "
        "a different number."),
}


def load_gold() -> dict[str, dict]:
    """
    {gold_sql_id: {sql, kind, key, value, scale}}

    `scale` is what the REFERENCE returns, not what v2 returns. golden.yaml's
    queries return ratios (0.8660) while v2 renders percent (86.60), and
    stating the reference's scale is what lets the comparison be exact instead
    of guessing from magnitude the way eval/run.py did.
    """
    out: dict[str, dict] = {}
    g = yaml.safe_load(config.GOLDEN_YAML.read_text(encoding="utf-8")) or {}
    for q in g.get("questions", []) or []:
        sql = (q.get("reference_sql") or "").strip()
        if not sql:
            continue
        out[q["id"]] = {
            "sql": sql, "kind": q.get("kind", "scalar"),
            "key": q.get("key"), "value": q.get("value"),
            # A ratio metric's reference returns 0..1; a count or an amount
            # returns itself.
            "scale": "ratio", "source": "golden.yaml"}
    if config.GOLD_SQL_PATH.exists():
        extra = yaml.safe_load(
            config.GOLD_SQL_PATH.read_text(encoding="utf-8")) or {}
        for gid, spec in (extra.get("gold") or {}).items():
            out[gid] = {"sql": spec["sql"].strip(),
                        "kind": spec.get("kind", "scalar"),
                        "key": spec.get("key"), "value": spec.get("value"),
                        "scale": spec.get("scale", "ratio"),
                        "source": "gold_sql.yaml"}
    return out


def run_gold(gold: dict[str, dict]) -> dict[str, dict]:
    """Execute each reference once. A query that fails is excluded, loudly."""
    from backend.core import db

    cache: dict[str, dict] = {}
    for gid, spec in sorted(gold.items()):
        try:
            rows = db.fetch_dicts(spec["sql"])
        except Exception as exc:  # noqa: BLE001
            cache[gid] = {"error": f"{type(exc).__name__}: {exc}", "rows": None}
            continue
        if not rows:
            # A reference returning nothing cannot establish what the right
            # answer is, so it is reported rather than scored as agreement
            # with whatever the system happened to return.
            cache[gid] = {"error": "reference returned no rows", "rows": None}
            continue
        cache[gid] = {"error": "", "rows": rows}
    return cache


def main() -> int:
    ap = argparse.ArgumentParser(description="EX on the gold subset")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--pipeline", default="")
    args = ap.parse_args()

    out_dir = config.RESULTS_DIR / args.run_id
    raw = out_dir / "own_raw.jsonl"
    if not raw.exists():
        raise SystemExit(f"no results at {raw}")

    gold = load_gold()
    print(f"{len(gold)} gold references "
          f"({sum(1 for g in gold.values() if g['source'] == 'golden.yaml')} "
          f"reused from eval/golden.yaml)")
    cache = run_gold(gold)
    broken = {g: c["error"] for g, c in cache.items() if c["error"]}
    if broken:
        print(f"  {len(broken)} reference(s) could not be used:")
        for g, e in list(broken.items())[:10]:
            print(f"    {g}: {e}")

    rows = [json.loads(l) for l in raw.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    by_pipeline: dict[str, list] = {}
    for r in rows:
        if args.pipeline and r.get("pipeline_name") != args.pipeline:
            continue
        by_pipeline.setdefault(r.get("pipeline_name", "?"), []).append(r)

    report: dict = {"n_gold": len(gold), "n_gold_usable": len(gold) - len(broken),
                    "unusable": broken}
    for name, rs in by_pipeline.items():
        scored, passes, first, misses = 0, 0, 0, []
        truncated: list[str] = []
        diverged: list[str] = []
        for r in rs:
            gid = r.get("gold_sql_id")
            if not gid or gid not in cache or cache[gid]["error"]:
                continue
            if not r.get("success"):
                scored += 1
                misses.append(f"{r['question_id']}({gid}):did-not-run")
                continue
            if r.get("row_count", 0) > len(r.get("rows") or []):
                # The stored rows were truncated, so this record cannot be
                # scored from the log. Excluded and counted, never guessed.
                truncated.append(f"{r['question_id']}({gid})")
                continue
            scored += 1
            got = ex_score_numeric(r.get("rows") or [], cache[gid]["rows"],
                                   gold_scale=gold[gid]["scale"])
            if got == 1.0:
                passes += 1
                if r.get("first_shot_success"):
                    first += 1
            elif gid in KNOWN_DIVERGENCES:
                diverged.append(f"{r['question_id']}({gid})")
            else:
                misses.append(f"{r['question_id']}({gid})")
        comparable = scored - len(diverged)
        report[name] = {
            # Every gold record that ran, divergent references included.
            "own_ex": round(passes / scored, 4) if scored else None,
            # Excluding references whose definition v2 never agreed to.
            "own_ex_comparable": (round(passes / comparable, 4)
                                  if comparable else None),
            "own_first_shot_ex": round(first / scored, 4) if scored else None,
            "n_scored": scored, "n_comparable": comparable, "n_pass": passes,
            "misses": misses[:25],
            "n_reference_divergence": len(diverged),
            "reference_divergence": diverged[:25],
            "divergence_reasons": {g: KNOWN_DIVERGENCES[g]
                                   for g in KNOWN_DIVERGENCES},
            "n_unscorable_truncated": len(truncated),
            "truncated": truncated[:25]}
        print(f"\n  {name}")
        print(f"    EX, all gold      : "
              f"{report[name]['own_ex'] if scored else 'n/a'}  "
              f"({passes}/{scored})")
        print(f"    EX, comparable    : "
              f"{report[name]['own_ex_comparable'] if comparable else 'n/a'}  "
              f"({passes}/{comparable})   "
              f"[{len(diverged)} reference(s) define the metric differently]")
        if misses:
            print(f"    missed            : {', '.join(misses[:12])}")
        if truncated:
            print(f"    unscorable (log row cap, re-run to score): "
                  f"{', '.join(truncated[:8])}")

    path = out_dir / "gold_ex.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
