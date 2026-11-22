# PayLens — Enterprise AI Business Analyst for payments

Ask a question about payment performance in plain English. Get an answer whose every
number traces to a validated, access-scoped, read-only query shown alongside it.

```
Q: why did our overall success rate drop last week?

Success Rate moved -1.33pp (87.93% → 86.60%), and 93% of that is traffic mix
rather than degraded processing.

OBSERVED      UPI share 61.95% → 54.95%, own rate 88.87% → 88.79%
              NETBANKING share 8.01% → 15.02%, own rate 71.26% → 71.13%
              mix effect -1.23pp · rate effect -0.09pp
HYPOTHESIS    [medium] A routing or checkout change shifted traffic toward
              NETBANKING. This data shows the shift, not its cause.
ACTION        [Product/Routing] Audit checkout and routing config for this window.
CAVEATS       2.10% of attempts have no issuer attribution — segment rows will
              not sum to the total.
```

**Nothing in the payment stack got worse.** A tool that groups by rail and sorts
ascending reports *"netbanking is failing"* — wrong, netbanking was always ~71%. A
tool that plots per-rail trends reports *"nothing changed"* — also wrong, the overall
number really did fall. Only the mix-vs-rate decomposition produces the actionable
answer, and it points at a different team entirely.

> A language model writes the SQL. The guard decides whether it runs. Python computes
> every figure in the answer, so a number you can see is a number that traces back to
> a query you can read.

```
775,957 payment attempts   ·   2 model calls per answer
14 validation checks before execution   ·   6 answer shapes   ·   287 tests
```

Built for the Pine Labs *Enterprise AI Business Analyst* assignment.

---

## Run it

```bash
# Python 3.12 — 3.14 has no onnxruntime wheels for chromadb
python -m venv ~/.venvs/paylens && source ~/.venvs/paylens/bin/activate
pip install -r requirements.txt

cp .env.example .env              # add an OpenRouter key
python data/generate.py           # deterministic, seed 42, ~40s
python data/verify.py             # acceptance checks on the generated data
python -m paylens2.schema.build   # creates v_attempts and v_settlements

python -m paylens2.api.app        # http://localhost:8001
```

`GET /v2/health` reports whether the views are built and which model is wired up.
Sign-in is optional — it changes the row filter the guard injects into your SQL, and
you can watch it appear in the SQL tab.

```bash
pytest -q                       # 240 tests
pytest evaluation/tests -q      # 47 more; no LLM, no network needed
```

---

## What it does

Three kinds of question, six shapes of answer. The branch is chosen by the model and
then verified in Python — a definitional question answered with a number is wrong
even when the number is right, which is why `explain` exists as its own path.

| Ask | Branch | What comes back |
|---|---|---|
| "success rate by rail last week" | `sql` | a measured number with its denominator, a chart, the evidence |
| "why did success rate drop" | `decompose` | a mix-versus-rate split computed in Python, identity asserted |
| "how do I reduce technical declines" | `advise` | levers from the catalogue, each with an owner |
| "what is the formula for success rate" | `explain` | the definition from the semantic model, never a value |
| "what's our success rate" | `clarify` | the question that separates two readings |
| "how many chargebacks" | `refuse` | this dataset does not hold that, and the nearest field that does exist |

---

## How it works

```
question
  1  author       schema card + few-shots + catalogue  →  one of six actions   [LLM]
  2  check 10     views only · no SELECT * · metric formulas verbatim
  3  guard        the nine AST checks, incl. RBAC injection and a re-walk
  4  execute      read-only, with a timeout and a row cap
  5  profile      deterministic statistics → numbered fact pack E1…En     [no LLM]
  6  synthesise   prose from the pack only → verify → one repair           [LLM]
  7  write back   kept as an unverified few-shot
```

Steps 2–3 are a heal loop: a rejection goes back to the model with the reason, and
the repaired SQL re-enters **both** from the top. The check that rejected a query is
never relaxed to let its replacement through.

### Four decisions that carry the design

**The views carry the rules the model would otherwise forget.** `v_attempts` is
de-duplicated — the raw table contains a planted double-ingest — and pre-joined to
all five dimensions, with indicator columns holding the official metric semantics
(`is_resolved` excludes PENDING deliberately: an attempt that has not resolved is not
a failure, and counting it as one understates the rate). The raw `fact_*`/`dim_*`
tables are absent from the allowlist, so the model cannot reach them, and it cannot
forget a rule it never knew existed.

**Python computes, the model picks.** There is no calculator tool, because a number
from a tool call has no provenance to show beside the answer. The profiler produces a
numbered fact pack; the composition may state only what is in it, and `verify()`
discards the whole thing otherwise. Periods are resolved in Python too — asked for
"last week" on a Saturday the model returned the trailing seven days despite an
explicit instruction, because finding the previous Monday is arithmetic.

**The query is cached; the answer never is.** A hit skips call 1 and nothing else, so
numbers are measured fresh and the guard still injects the caller's row filter. The
resolved window is part of the cache key, so replaying "last week" after the week
rolls over is a miss by construction rather than a confident wrong answer.

**Failing honestly — four rungs, and only the last may blame the data.** It worked →
the answer. Some evidence failed → the rest, saying how many did not run. Nothing ran
→ *"a failure on my side"*, plus questions that do work. The concept is absent →
*"this dataset does not hold chargebacks"*, with the nearest field.

### The hole this design had to close

`rbac.assert_injected` (guard check 9) skips tables a query does not name:

```python
targets = _selects_touching(root, pred.table)
if not targets:
    continue          # ← vacuously passes
```

The classic semantic model anchors every role scope on `fact_transactions`. A query
that reads `v_attempts` names no `fact_transactions`, so it would get **no row filter
while check 9 passed anyway** — a merchant reading every merchant's rows. PayLens
declares `anchor_table: v_attempts` instead, and `guard()` is parameterised by the
model it is handed, so enforcement is the same code pointed at a different target
rather than a second copy that could drift. A test asserts the hole exists under the
old model and is closed under this one.

> **Known gap, found by the evaluation harness and still open.** The scope is declared
> against `v_attempts` only, so a `merchant_user` query that reads `v_settlements`
> gets no predicate and check 9 passes vacuously on it — the same hole, one view
> across. Verified: a merchant scoped to `M-1042` reads 28,800 settlement rows
> spanning all 240 merchants. `row_scope` is a single dict and `rbac.row_predicates`
> returns at most one predicate, so the fix belongs there rather than in config.
> `evaluation/metrics/paylens_metrics.py` → `rbac_enforcement` reports the offending
> table by name.

---

## The data

Six related tables, generated deterministically (`seed=42`, ~40s), **calibrated to
NPCI's published September 2026 figures** rather than invented: technical decline
0.24%, business decline 10.88%, implied approval rate 88.88%, worst published Fino
24.26% BD and ICICI 0.73% TD.

Where it departs, it says so. Technical decline is set to **1.5%**, not 0.24% — at the
published rate the dataset holds ~1,800 technical declines across four months, too few
to decompose, rank or trend, so the product would have nothing to demonstrate on its
most interesting path. Every modelled share is labelled `[MODELLED]` or `[ASSUMED]` in
[`datasets/CALIBRATION.md`](datasets/CALIBRATION.md), with its source.

**Five faults are planted on purpose**, and five probes surface them against the window
each answer actually used:

| Probe | What it finds on this dataset |
|---|---|
| `duplicate_txn_ids` | verifies the view's de-duplication held |
| `null_issuer` | 2.09% of attempts have no issuer, so issuer rows will not sum |
| `partial_days` | the last 2 days are part-ingested; reported, never trimmed |
| `negative_amount` | refund corrections posted against the original rail |
| `settlement_mismatch` | 5 merchants where transactions and settlements disagree |

Findings are appended to the answer's caveats, because a warning nobody surfaces was
not issued. Every probe runs through the guard with the caller's own principal — a
background query is the easiest place to leak rows a role may not see, precisely
because it is not user-initiated and so never looks like the thing to check.

---

## Evaluation

A 400-question bank across 15 buckets — measurement, statistical, maths-derived,
advisory, definitional, causal, ambiguous, out-of-scope, adversarial, RBAC-scoped and
multi-turn — run against PayLens and against a deliberate baseline. Every formula and
what it catches is in [`evaluation/METRICS.md`](evaluation/METRICS.md).

```bash
python -m evaluation.datasets.bank_build --check    # validates the 400
python -m evaluation.runners.run_own_eval --smoke   # one per bucket, ~90s
python -m evaluation.runners.run_own_eval --pipeline v2,baseline --workers 3
python -m evaluation.runners.run_judge   --run <run_id>   # G-Eval, separately
python docs/build_deck.py --run <run_id>                  # rebuild the report
```

240 of the 400 questions are **generated and self-labelling**: the metric × dimension
× period coordinates a question is built from *are* its expected labels, so they carry
no annotation risk. 60 carry hand-written gold SQL.

**The baseline** is two model calls and nothing else — call 1 writes SQL from the raw
`fact_*`/`dim_*` DDL, call 2 narrates the rows. No retrieval, no healing, no views, no
guard, no profiler, no grounding check. Same model, same temperature, same questions.
It is not a straw man; it is the shape most text-to-SQL systems ship in.

| | PayLens | Baseline |
|---|---|---|
| Execution success rate | **95.7%** | 83.1% |
| EX on the gold subset | **100%** (16 comparable refs) | — |
| Schema precision | **100%** | 0% |
| Metric-formula fidelity | **100%** | 0% |
| False-refusal rate | **1.1%** | 30.1% |
| VES-static | **99.8%** | 70.0% |
| Action routing | **90.5%** | 65.8% |
| RBAC leak resistance | **100%** (40/40 tampers refused) | — |
| P50 / P95 latency | 14.6s / 32.6s | 3.5s / 8.1s |

G-Eval — judge `ernie-5.5-flash`, deliberately **not** the author model, since
self-judging shares the blind spots that produced the answer — puts the two within a
few points on prose quality (composite **82.5 vs 79.9**), and the baseline wins on
completeness. That is the honest result: both are driven by the same model, so both
write readable answers. The separation is in the deterministic checks a judge cannot
fake, and the latency those checks cost is the price.

Each pipeline is judged against **the schema it was actually given**. Handing
PayLens's view card to both scored every baseline answer 0.0 on schema alignment — for
using tables only PayLens had been told were blocked — which measured obedience, not
quality.

### Reproducibility

The evaluation date is pinned to **2026-11-20**: `periods.resolve()` bakes literal
dates into the SQL, the data ends there, and unpinned "last week" resolves to a
near-empty window. The cache is off for the headline run — a hit drops the author call
from ~11,700ms to ~15ms and would turn the latency table into two distributions
reported as one. Write-back is off so the few-shot corpus is frozen and question 400
sees exactly what question 1 saw.

**BIRD** (`evaluation/runners/run_bird_eval.py`) is built and tested but needs the
Mini-Dev SQLite databases, distributed only via a Google Drive link on
<https://bird-bench.github.io/>. Extract to
`evaluation/datasets/bird_dbs/<db_id>/<db_id>.sqlite`; the 500 questions and gold SQL
load from HuggingFace automatically. The runner preflights and evaluates only the
databases present, so it is correct at 0, 3 or 11 of them.

---

## Layout

```
paylens2/            the pipeline
  api/               FastAPI app and the /v2 routes
  core/              author · checks · profile · synth · drivers · dq · sqlcache
  schema/            views.sql, views.yaml (the semantic model), build.py
  knowledge/         the definitional corpus and its builder
  frontend/          the chat UI and 797 lines of hand-rolled SVG charts
  tests/             185 tests
backend/             shared runtime: guard, rbac, executor, db, llm, kb
semantic/            playbooks.yaml (9 playbooks, 29 levers), seeds.yaml
evaluation/          the harness — see evaluation/README.md
data/                generate.py, verify.py, npci_calibration.py
datasets/            the generated tables and CALIBRATION.md
docs/                the architecture slide, the 7-page deck, and their generator
eval/                golden.yaml and adversarial.yaml — inputs to the bank
```

## Limits, stated first

- **It needs a language model to author.** With no key it refuses rather than guessing.
  The definitional and decomposition paths have offline templates, so those two degrade
  rather than fail.
- **Conversation state lives in the browser.** Clearing site data loses the threads, and
  they do not follow you to another device.
- **Advisory coverage is exactly as wide as `playbooks.yaml`** and no wider. A question
  no playbook covers degrades to a measurement rather than inventing counsel.
- **The settlements RBAC gap above is real and open.**
- **Numbers move between runs.** `LLM_TEMPERATURE` is 0.2, not 0, and the provider does
  not guarantee determinism. The *protocol* is reproducible — pinned date, committed
  bank, frozen corpus, cache off — but expect EX to move a point or two.

**Read [`ASSUMPTIONS.md`](ASSUMPTIONS.md) next** for the assumptions, mocks and
limitations in full. Note that `ASSUMPTIONS.md`, `SUBMISSION.md` and `WORKFLOW.md`
still describe the retired compiled-plan engine in places and are being brought up to
date; this README and `evaluation/METRICS.md` are current.
