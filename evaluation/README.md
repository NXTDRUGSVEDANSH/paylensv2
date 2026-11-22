# PayLens v2 — evaluation harness

Produces the three tables the report needs: Track 1 (BIRD), Track 2A (own
queries, operational), Track 2B (LLM judge). Metric definitions are in
[METRICS.md](METRICS.md).

Ported from the harness at `~/Desktop/Projects/Fractal AI/evaluation/` so the
numbers sit beside its published figures. Those figures are frozen in
`runners/prior_art.py` and appear in the report as a prior-art column.

## Layout

```
config.py                  pinned date, judge model, workers, paths
METRICS.md                 every metric: formula, range, what it catches
datasets/
  own_queries.jsonl        the 400-question bank (committed)
  bank_build.py            builds it; 240 generated, 120 authored, 40 folded
  bird_loader.py           BIRD questions (offline-first) + sqlite access
  bird_adapter.py          points the real guard at a BIRD schema
  bird_dbs/                (gitignored) drop the Mini-Dev databases here
pipelines/
  base.py                  EvalResult + the Pipeline contract
  v2_pipeline.py           the full v2 pipeline, in process
  v2_bird_pipeline.py      author + sqlfix + heal + guard only
  naive_baseline.py        2 calls over the raw tables, no guard
  bird_baseline.py         the same baseline on BIRD
metrics/
  ex_accuracy.py ves.py healing.py latency.py error_breakdown.py llm_judge.py
  paylens_metrics.py       the metrics the reference harness could not compute
runners/
  run_own_eval.py  run_bird_eval.py  report.py  prior_art.py
tests/                     47 tests; run without any LLM or BIRD download
results/<run_id>/          jsonl, metrics, REPORT.md, per_query.csv
```

## Run it

```bash
# always from the repo root, with the project venv
cd "/mnt/c/Users/l/Desktop/Pine Labs"

# 1. the bank (committed, but rebuildable and self-validating)
python -m evaluation.datasets.bank_build --check

# 2. the harness's own tests — no LLM, no network, ~2s
python -m pytest evaluation/tests -q

# 3. smoke: one record per bucket, exercises all six action branches,
#    both scoped roles, and the whole write path. ~90s.
python -m evaluation.runners.run_own_eval --smoke

# 4. the real thing
python -m evaluation.runners.run_own_eval --pipeline v2,baseline --workers 3

# resume after an interruption — skips what is already recorded
python -m evaluation.runners.run_own_eval --resume <run_id>

# BIRD, once the databases are in place
python -m evaluation.runners.run_bird_eval --smoke
python -m evaluation.runners.run_bird_eval --pipeline v2,baseline --limit 500
```

## The three things that make the numbers mean something

**The evaluation date is pinned to 2026-11-20.** `periods.resolve()` bakes
literal dates into the SQL, so "last week" means something different on every
run. The generated data ends 2026-11-20; asked on 2026-11-22, "last week"
resolves to a window with almost no rows in it and every accuracy and latency
figure would be measured against an empty result. 2026-11-20 also reproduces
`eval/golden.yaml`'s windows exactly, so the 19 hand-written reference queries
already in that file are reusable as gold without being rewritten.

**The NLQ→SQL cache is off.** A hit drops the author call from ~11,700 ms to
~15 ms. Left on, the latency table would be two distributions reported as one
and the percentiles would depend on the order the bank happened to be in.
Cache behaviour is measured separately as its own metric.

**Write-back is off, retrieval is on.** `ask(use_kb=False)` controls only the
write-back; few-shot retrieval gates on the global `V2_USE_KB`. So the corpus
is frozen while the feature stays live — question 400 sees exactly what
question 1 saw, which is what makes a run reproducible and order-independent.
`run_env.json` records the KB count before and after; they must be equal.

## BIRD

The questions and gold SQL load without any download. The SQLite databases do
not: BIRD distributes them through a Google Drive link that cannot be scripted.
Get "Mini-Dev Databases" from <https://bird-bench.github.io/> and extract so
that each database sits at:

```
evaluation/datasets/bird_dbs/<db_id>/<db_id>.sqlite
```

The runner preflights, prints which `db_id`s it found, and evaluates only
those — so it is correct at 0, 3 or 11 databases, and the report header states
how many were used. A 3-database EX is not a 500-question EX.

Two findings decide whether that number is honest, both covered by
`tests/test_dialect_defaults.py`:

- **The dialect must be `sqlite`.** `SELECT count(*) FROM frpm WHERE \`Free
  Meal Count (K-12)\` > 100` does not parse as duckdb and parses fine as
  sqlite. BIRD schemas are full of spaced and parenthesised identifiers, and
  parsing them as duckdb would record a parser mismatch as the model writing a
  broken query.
- **`c10c_no_select_star` must be off.** It is right for a 60-column view and
  wrong for a benchmark whose reference answers use `SELECT *`.

`guard()` and `check_v2()` gained a `dialect` parameter and `check_v2()` a
`skip` set for this. Both default to today's behaviour exactly, and a test
asserts the defaults so a later change cannot quietly relax a production check.

## Relationship to `eval/`

`eval/golden.yaml` and `eval/adversarial.yaml` stay where they are — they are
inputs, and `bank_build.py` reads them from there. `eval/run.py` is superseded
for reporting: two harnesses reporting different numbers for the same thing is
how the repo ended up claiming 76, 325 and 49 tests in three places. Its four
sections are folded in as metrics:

| `eval/run.py` | becomes |
|---|---|
| `metric_value()` — scored 6 of 19 cases, skipped all 13 `set` ones | EX over the whole gold subset, `set` cases included (`compute_own_ex`) |
| its inline "schema discipline" check | Schema precision, over 400 records instead of 6 |
| `guard_recall()` — hardcoded `role="cxo"`, ignoring the `role`/`claim` on adversarial x08–x10, so the scope-escape prompts never ran as a merchant | RBAC enforcement + the two-tamper leak test, with those prompts run as the principal they name |
| `ambiguity()` — passed on "clarified or assumed" | Clarification precision, recall and F1 |
| `advisory()` | Grounding, plus lever-owner coverage |
