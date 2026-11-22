# Evaluation — metric glossary

Two tracks. **Track 1** benchmarks the authoring core against BIRD mini-dev, a
standard text-to-SQL dataset with gold SQL and SQLite databases. **Track 2**
evaluates the full pipeline on 400 domain questions against the PayLens
warehouse, with operational metrics and an LLM-as-judge assessment.

The Track 1 and Track 2A definitions are carried over verbatim from the harness
this ports from, so the numbers sit beside its published figures without a
translation step. Section 3 holds the metrics that harness had no fields to
compute.

---

## Track 1 — BIRD

### EX — Execution Accuracy
- **Formula:** `EX = (1/N) Σ 1[result_set(pred_sql) == result_set(gold_sql)]`
- **Range:** 0–1
- **Catches:** whether the predicted SQL returns the same rows as the gold SQL,
  regardless of syntactic form. The primary correctness signal.
- **Implementation:** `metrics/ex_accuracy.py` → `ex_score`, `compute_ex_metrics`
- **Comparison is values-only and order-insensitive**, so a different alias or
  column order is not a miss.

### First-shot EX vs Healed EX
- **Formula:** first-shot counts queries correct with no healing; healed counts
  those that needed healing and ultimately matched gold.
- **Catches:** separates generation quality from error-recovery quality.
- **Not comparable to the prior-art figure.** That harness passed the gold row
  count and gold column names into its pipeline and let it re-prompt against
  them, so its 44.4% is healing with an oracle. Here the heal loop sees only
  the rejection reason from check 10 and the guard, never anything derived from
  the answer.

### VES — Valid Efficiency Score
- **Formula:** `VES = (1/N) Σ [EX_i · √R_i]`, `R_i = min(T_gold / T_pred, 1)`
- **Range:** 0–1
- **Catches:** correct but slow queries.
- **Implementation:** `metrics/ves.py` → `compute_ves_bird`

### Error breakdown
Categorical counts over `failure_kind` and the rejecting check, not regex over a
driver message — see §3.0.

### What is *not* applied on this track
`c10a_views_only` (matches PayLens table names only, a no-op here),
`c10c_no_select_star` (right for a 60-column view, wrong for a benchmark whose
reference answers use `SELECT *`), `c10d_metric_formula` and
`c10e_failure_only_grouping` (the generated model declares no metrics, so both
are inert by construction). All nine guard checks run. RBAC passes with nothing
to inject rather than being skipped — a BIRD database has no tenant to scope to.
`c8_cost` is effectively inert because DuckDB's plan for a materialised BIRD
table prints `~1 row` and the estimate regex does not match; this is documented
rather than patched, because patching it mid-study would make the two tracks
incomparable.

---

## Track 2A — Own queries, operational

### ESR — Execution Success Rate
- **Formula:** `ESR = |{ok}| / N`
- **Scored over the measuring subset only.** The `advise`, `explain`, `clarify`
  and `refuse` branches never author SQL, so including them would measure
  something else.

### Healing efficiency
- **Formula:** `HE = healed_successfully / needed_healing`, where a query
  "needed healing" when it was not a first-shot success.
- **Range:** 0–1, `None` when nothing needed healing.

### P50 / P95 latency
- 50th and 95th percentile of `total_ms`, via `statistics.quantiles`.
- **Measured with the NLQ→SQL cache OFF.** A cache hit drops the author call
  from ~11,700 ms to ~15 ms; left on, these would be two distributions reported
  as one and the percentiles would depend on the order of the bank.

### VES-static
- **Formula:** starts at 1.0, deductions for `SELECT *` (−0.25), reading a raw
  `fact_*`/`dim_*` table (−0.25), no time predicate (−0.20), a non-aggregate
  with no `LIMIT` (−0.15), a rate whose denominator is a bare `count(*)`
  (−0.10), `JOIN` with no `ON` (−0.10). Floored at 0.
- **Catches:** structural SQL quality where no gold SQL exists. The deductions
  are re-aimed at this schema; the reference version penalised a table that
  does not exist here.

### EX on the gold subset
- `compute_own_ex` scores only records carrying gold SQL. Others have no
  reference to be right or wrong against, and counting them either way would
  be a lie. `n_scored` is always reported.
- Comparison is **tolerance- and scale-aware** (`ex_score_numeric`): v2 renders
  a ratio metric as a percent (86.60) while the hand-written references return
  the ratio (0.8660), and the two compute from different de-duplication paths
  so the last float places differ. 0.1% relative tolerance, and `gold_scale`
  states what the reference returns rather than guessing from magnitude.

---

## Track 2B — LLM judge (G-Eval, 4 criteria)

Prompt, criteria and weights are **byte-identical** to the reference harness, or
the columns would not be comparable.

| Criterion | Weight | What it measures |
|---|:---:|---|
| Temporal precision | 0.30 | does the SQL capture every time constraint the question implies |
| Schema alignment | 0.25 | right tables, columns and aggregations for the stated intent |
| Faithfulness | 0.30 | is every claim in the answer grounded in the returned rows |
| Completeness | 0.15 | does the answer address all parts of the question |

- **Composite:** `0.30·temporal + 0.25·schema + 0.30·faith + 0.15·complete`
- **Run on runnable queries only**, as specified: `judge_result` returns a null
  judgment when the SQL did not execute, and those are excluded from the means.
  `n_judged` and `n_judge_errors` are reported beside every figure.
- **The judge is a different model from the author.** Scoring a model's output
  with the same model shares the blind spots that produced it, and
  `schema_alignment` in particular would reward the conventions the author was
  trained on. The prior-art column was self-judged with its own author model,
  so a cross-model judge here makes the PayLens figure harsher, not kinder.

---

## 3 — Beyond the reference set

Metrics the earlier harness could not produce: its pipeline returned SQL and
prose, so there were no other fields to read. v2 carries its working on every
answer. All in `metrics/paylens_metrics.py`. **A metric with no applicable
records returns `None`, never 0.0** — an unmeasured thing and a thing measured
at zero are different claims.

### 3.0 Error breakdown, re-aimed
- **Categories:** `refused_data_absent`, `check10_rejected`, `guard_blocked`,
  `sql_parse_error`, `execute_error`, `timeout`, `row_cap_blocked`,
  `config_no_model`, `empty_result`.
- Read from `failure_kind` and `attempts[].stage` rather than guessed by regex
  from a driver message.
- **Two rates, not one.** `error_rate` counts every unsuccessful result;
  `our_fault_rate` excludes honest refusals, because a correct "this dataset
  holds no chargebacks" is a success of the system and counting it as an error
  would reward a pipeline that invents an answer instead.

### 3.1 Action-routing accuracy (+ 6×6 confusion matrix)
- **Formula:** `(1/N) Σ 1[derived_action == expected_action]`
- **Catches:** the failure with no analogue in a single-path pipeline. Asked
  "what is the formula of success rate", the system once answered with last
  month's value — the identical response to "what is success rate last month".
  A definitional question answered with a number is wrong even when the number
  is right, and no SQL-accuracy metric can see it.
- The off-diagonal cells are the output worth reading: `explain→sql` is
  over-measuring, `sql→clarify` is over-asking, `sql→refuse` is a false
  refusal, `refuse→sql` is a safety breach.

### 3.2 Metric-formula fidelity
- **Formula:** `|{aliases a metric id ∧ check 10d passed}| / |{aliases a metric id}|`
- **Catches:** `sum(is_success)/count(*)` — syntactically fine, reads naturally,
  silently includes PENDING in the denominator. A different number from a query
  that looks correct.
- Reported twice: `first_shot` (right unaided) and `final` (right after
  healing). The gap is what the heal loop buys, in the units of the defect it
  exists for.

### 3.3 Schema precision
- **Formula:** `|{sql names no fact_*/dim_*}| / |{has sql}|`
- **Catches:** a right number reached through a raw table, which carries the
  planted double-ingest and inflates counts without erroring.
- Near-tautological on v2 by construction; a real measurement on the baseline,
  where a low number is the definition of a baseline rather than a bug in one.

### 3.4 RBAC enforcement, and leak resistance
- **Enforcement:** `|{scoped ∧ injected_predicates ≠ ∅}| / |{scoped}|`
- **Leak resistance:** for each scoped query, two tampers — the predicate
  **deleted**, and the predicate **neutralised** with `OR 1 = 1` — each handed
  to `rbac.assert_injected`. Both must be refused.
- **Why it calls `assert_injected` and not `guard`:** injection is part of what
  `guard()` does, so stripping the filter and re-guarding would simply put it
  back and pass. The first version of this test did exactly that and reported a
  leak on a healthy system. Check 9 is the half that matters: it re-walks the
  AST and asserts the predicate survives as a top-level conjunct, which is what
  catches a filter that was injected and then neutralised downstream.
- The neutralised case is the one a textual check cannot catch: `merchant_id =
  'M-1042'` is still right there in the query and the row filter is gone.
- Pure AST work — no LLM calls, nothing executed.

### 3.5 Grounding rate and ungrounded-number rate
- **GR:** `grounded / (grounded + rejected)`; `not_applicable` is excluded from
  the denominator rather than counted as a pass.
- **UNR:** `Σ|ungrounded| / Σ numbers_checked` — per number, not per answer.
- **Catches:** invented figures, deterministically. The judge's `faithfulness`
  measures the same thing stochastically; reporting both is the point. A high
  judge-faithfulness beside a non-zero UNR means the judge is being generous,
  and the deterministic number is the one to trust.

### 3.6 Repair and recovery
- `mech_repair_rate`, `recovery_rate`, `recoveries_per_query`, and a histogram
  by stage.
- **Catches:** work the user never saw. A mechanical `sqlfix` repair (a
  single-colon cast) and a semantic re-author are both "healing" in a
  one-number harness and they are different things: the first says the model
  has a syntactic tic worth fixing in the prompt, the second says it
  misunderstood the schema.

### 3.7 False-refusal rate and honest failure
- **FRR:** `|{refused ∧ failure_kind=="data" ∧ expected ≠ refuse}| / |{expected ≠ refuse}|`
- **Catches:** the most corrosive failure available — telling a user their data
  lacks something it holds. They cannot tell it from the truth and will believe
  their warehouse is incomplete.
- Alongside: `refusal_recall` (out-of-scope and adversarial prompts correctly
  declined), `system_failure_rate`, `config_failure_rate` (**must be 0 or the
  run is invalid**), and `system_failures_with_suggestions` — a failure must
  offer a way forward rather than a dead end.

### 3.8 Clarification precision / recall / F1
- **Catches:** the existing harness scores "clarified or stated an assumption"
  as a pass, which cannot tell a system that clarifies well from one that
  clarifies everything. Asking about everything is its own kind of unhelpful,
  and only precision sees it.
- Alongside: `assumption_disclosure_rate` — where no period was named and it
  answered anyway, did it say what it assumed.

### 3.9 Silent-empty rate
- **Formula:** `|{ok ∧ row_count==0 ∧ prose does not say so}| / |{ok ∧ row_count==0}|`
- **Catches:** "there were none" when the filter was wrong — a wrong answer
  produced by a correct query, invisible to every accuracy metric because the
  SQL ran and returned exactly what it asked for. Also the failure an unpinned
  evaluation date produces at scale, which makes it a canary for the harness.

### 3.10 Decomposition identity residual
- **Formula:** `|delta_pp − (mix_pp + rate_pp)|`; `identity_hold_rate` at
  ≤ 0.01 pp.
- **Catches:** an exact identity, so a non-zero residual is unambiguous: either
  the segment weights do not sum to one, or an aggregate was truncated on the
  way in. Either way the split is meaningless, and a meaningless split
  presented confidently is worse than no split.

### 3.11 Data-quality surfacing rate
- **Formula:** `|{a failing probe reached the caveats}| / |{has a failing probe}|`
- **Catches:** a warning that was computed but never shown was not issued.

### 3.12 Cost
- `calls_per_query`, `calls_by_purpose`, `ms_by_purpose`,
  `llm_share_of_latency`, `failed_call_rate`.
- `llm_share_of_latency` is the honest framing of any latency comparison: when
  ~95% of wall clock is provider time, a pipeline making the same number of
  calls is not slower by design.

### 3.13 Cache speedup (measured separately)
- Author-stage and end-to-end speedup reported **separately**, because they
  differ by two orders of magnitude and quoting the stage figure as the
  end-to-end one would be the inflated claim. A hit skips call 1 and nothing
  else, so everything downstream still runs.

### 3.14 Seeded vs unseen
Every metric above is additionally emitted split by whether the wording was one
the KB was seeded with. Reporting only the combined number flatters the
in-distribution half.
