# PayLens v2 — LLM-authored SQL

A second pipeline beside the classic one. Mounted at `/v2`; `/ask` is untouched.

```
POST /v2/ask         {"question": "...", "conversation": [...]}   role comes from the session
POST /v2/ask/stream  the same answer, preceded by the pipeline's own events
GET  /v2/health      views built? model configured? which tables are allowed?
GET  /v2/schema      the exact schema card sent to call 1
GET  /v2/catalogue   the advisory entries call 1 can select from
```

## Progress is measured, not animated

The UI used to animate a fixed list of stages on a 1100ms timer. It advanced
whether or not anything had happened, and the proportions were inverted — a
measured run spends 96% of its wall clock in two model calls:

```
author  13,006ms   validate 44ms   execute 591ms   profile 2ms   synthesise 15,776ms
```

`/v2/ask/stream` sends the events the pipeline emits as it emits them, and the
page draws what arrives. The same channel makes the **recoveries** visible:
things that went wrong and were repaired without failing the request. These
previously existed only as log lines on stdout, which meant the most interesting
thing the system does was also the least visible:

```
13006ms  recovery  author      the model's JSON was malformed — repaired
29421ms  recovery  synthesise  the first composition stated a number that was not
                               measured — repaired and re-checked
```

They now ride home on the answer as `recoveries[]`, so `/v2/ask` carries them too
and they survive a reload. `backend/core/events.py` holds the channel; it is a
ContextVar, so a request running in a worker thread gets its own and `emit()` is
a no-op when nobody is listening — which is why the emit calls added to
`provider.py` and `jsonx.py` leave the classic pipeline untouched.

## Setup

```bash
python -m paylens2.schema.build     # creates v_attempts and v_settlements
                                    # needs exclusive write access: stop the server first
```

`GET /v2/health` reports `views_built: false` until that has run, and `/v2/ask`
refuses with the same message rather than answering from the raw tables.

## The flow

```
question
  1  author       schema + few-shots + catalogue  →  sql | clarify | refuse   [LLM]
  2  check 10     views only · no SELECT * · metric formulas verbatim
  3  GUARD        the existing nine checks, unmodified
  4  execute      aggregates over the cap BLOCK; detail truncates
  5  profile      deterministic stats → numbered facts E1…En              [no LLM]
  6  synthesise   prose from the pack → verify → one repair                [LLM]
  7  write back   as an unverified few-shot
```

Steps 2–3 are a heal loop: a rejection goes back to the model with the reason,
and the repaired SQL re-enters **both** from the top. The check that rejected a
query is never relaxed to let its replacement through.

## Why this is safe

**The guard does not care who wrote the SQL.** Model-authored queries pass the
same nine AST checks as compiled ones, including the RBAC predicate injection and
the re-walk that confirms it survived. A hallucinated query gets rejected, not
executed.

**The views carry the rules the model would otherwise forget.** `v_attempts` is
de-duplicated and pre-joined, with indicator columns (`is_success`,
`is_resolved`, …) holding the official metric semantics. The raw `fact_*` and
`dim_*` tables are not in v2's allowlist, so the model cannot reach them.

**Python computes every number.** The profiler produces the fact pack; the model
may state only what is in it, and `verify()` discards the composition otherwise.
There is no calculator tool, because a number from a tool call has no provenance
to show beside the answer.

## The hole this design had to close

`rbac.assert_injected` skips tables the query does not name:

```python
targets = _selects_touching(root, pred.table)
if not targets: continue      # ← vacuously passes
```

The classic model anchors every role scope on `fact_transactions`. A v2 query
reads `v_attempts`, names no `fact_transactions`, and would therefore get **no row
filter while check 9 passed anyway** — a `merchant_user` reading every merchant's
rows.

Fix: v2 loads its own semantic YAML with `anchor_table: v_attempts`. `guard()` is
parameterised by the model it is handed, so enforcement is the same code with a
different target. `tests/test_rbac.py` asserts the hole exists under the classic
model and is closed under v2's — so nobody can "simplify" v2 back onto the
classic model without a test going red.

## Verified

```
classic compiled path : sr=0.866014  attempts=48,976
v2 view path          : sr=0.866014  attempts=48,976   IDENTICAL
```

Two pipelines disagreeing on one question would be worse than either being slow.
Check 10 (`c10d_metric_formula`) is what keeps that true: a query aliasing a
column `success_rate` must use the official expression, so the
`successes / count(*)` spelling — which silently includes PENDING — is rejected.

## Layout

```
schema/views.sql     the DDL: v_attempts (de-duplicated, pre-joined), v_settlements
schema/views.yaml    v2 semantic model — view datasets, metrics, RBAC anchored on the view
schema/build.py      creates the views; idempotent
core/model.py        loads views.yaml, builds the schema card sent to call 1
core/prompts.py      both prompt contracts
core/author.py       call 1 + heal: sql | clarify | refuse, each one validated
core/checks.py       check 10
core/profile.py      deterministic statistics and the fact pack
core/synth.py        call 2 + the grounding check + one repair
core/catalogue.py    playbooks.yaml, in-prompt (19 entries do not need a vector search)
core/retrieval.py    Chroma few-shots; v2 records written back unverified
core/pipeline.py     the orchestrator
api/routes.py        /v2/*
```

## Configuration

| Variable | Default | |
|---|---|---|
| `V2_AUTHOR_MODEL` | `PLANNER_MODEL` | writes SQL; a coding-capable model helps |
| `V2_SYNTH_MODEL` | `NARRATOR_MODEL` | writes prose |
| `V2_MAX_HEAL_CALLS` | 2 | re-authoring attempts after a rejection |
| `V2_USE_KB` | true | few-shot retrieval and write-back |
| `V2_PROFILE_FULL_ROWS` | 50 | rows shown to call 2 before summarising instead |

## Definitional questions

A fifth branch out of call 1, for a question that asks what something **is**
rather than what it is **right now**:

```json
{"action": "explain", "terms": ["success_rate"], "also_measure": "..."}
```

This exists because its absence was a defect. With only `sql | advise | clarify
| refuse` available, *"what is the formula of success rate"* had nowhere to go
and came back as a measurement of last month — the identical answer to *"what is
success rate for last month"*, which is how it was spotted. A definitional
question answered with a number is wrong even when the number is right.

No SQL runs on this path, so the guard is not involved. What replaces it is
`knowledge.verify_formula`: a composition that states a formula for a known
metric is checked against `views.yaml`, and discarded for the semantic model's
own wording if it does not match. The prose and the generated SQL have to agree
about what a metric IS, or the system contradicts itself depending on how you
ask.

### The knowledge corpus

`paylens_knowledge` — a **second Chroma collection**, sharing the client and the
embedder but never the few-shot corpus. A definition retrieved into the
question→SQL slot would teach the model to answer a measurement with prose.

Three tiers, built by `python -m paylens2.knowledge.build`:

| tier | source | authoritative |
|---|---|---|
| metric | generated from `schema/views.yaml` | yes |
| glossary | `semantic/playbooks.yaml` | yes |
| industry | `knowledge/industry.yaml`, with sources | **no** |

The metric tier is generated rather than written, so a metric whose definition
changes cannot leave a stale explanation behind — and a test asserts every
chunk's formula equals `canonical_formula(mid)`.

**The industry tier is context, never the definition**, and the denominator is
why. Checkout.com and PayPal define the authorisation rate as approvals ÷
attempts. Adyen reports *gross* by default and notes that one payment retried
three times is 100% net and 25% gross. Nuvei points out that fraud-blocked
transactions may or may not sit in the denominator. **None of them address
unresolved attempts.** PayLens excludes PENDING — a choice it has to own rather
than cite, and one that `c10d_metric_formula` enforces in every generated query.
So each industry entry carries a `contrast` field stating where PayLens differs,
and that difference is usually the most useful thing the answer can say.

## Business questions

A question that asks what to DO takes a fourth branch out of call 1:

```json
{"action": "advise",
 "playbook_ids": ["retry_strategy"],
 "evidence_queries": [{"id": "q1", "purpose": "the retriable split", "sql": "..."}],
 "assumptions": ["last 30 days, since no period was named"]}
```

The SQL is the **evidence**, not the answer. Two to four small queries each pass
check 10 and the full guard separately, and their results merge into one
numbered pack (`core/profile.py:combine`). Call 2 then writes a briefing:

```
SITUATION         one sentence a business reader can act on
WHAT'S HAPPENING  measured statements, each citing [E*]
WHAT TO DO        levers, each with its owner and the question that checks it
WHAT THIS CAN'T TELL YOU
```

**Advice is verified the way numbers are.** `verify()` cannot catch *"you should
enable network tokenisation"* — that sentence contains no figure. So
`verify_levers()` checks every recommendation against the levers of the selected
playbooks and drops the ones that are not there. An advisory answer therefore
cannot contain advice this system has no basis for, or advice with no owner.

A question with no catalogue match degrades to a measurement rather than
inventing counsel, and an advisory question is **never** asked which period it
means — the user is not withholding one.

## When something goes wrong

A user asked two questions and got *"NOT ANSWERABLE FROM THIS DATA"* on both.
Nothing was missing: the model had written `:DOUBLE` with one colon. The answer
told them something false about their own data, and printed sqlglot's error —
ANSI escape codes and all — as the response.

Four rungs now, and only the last may blame the data:

| rung | when | what the user sees |
|---|---|---|
| 1 | it worked | the answer |
| 2 | some evidence queries failed | the answer from the rest, saying how many did not run |
| 3 | nothing ran | *"I could not build a query for that — this is a failure on my side"*, plus clickable questions that do work |
| 4 | the concept is absent | *"this dataset does not hold chargebacks"*, with the nearest field |

Supporting that:

- **`core/sqlfix.py`** repairs the mechanical flaws a code model makes — single-colon
  casts, welded keywords, smart quotes, stray fences. String literals are masked
  first so `'TIME:OUT'` is never touched, every repair is re-parsed before being
  accepted, and an unrepairable query is returned unchanged rather than mangled.
  Repairs appear in the Trace tab: silently rewriting someone's query is its own defect.
- **`jsonx.loads()`** now parse-checks the SQL inside a truncation-recovered
  object. A half-written `WITH … UNION` used to pass as a recovery.
- **ANSI is stripped** from every error before it reaches a prompt or a response.

## Numbers a business reader can read

- Ratios render as `86.60%`, money as `INR 1,234,567` — driven by the `format`
  declared in `schema/views.yaml`, not by the model.
- A two-row result gets its **change computed in Python**: `-1.33pp`, labelled
  *percentage points, not percent*. Without it the composer either omits the
  point of a "why did it drop" question or subtracts the numbers itself and is
  rejected for an ungrounded figure.
- **Check 10e** blocks a rate grouped by a failure-only column. A real answer
  read *"the 'customer' failure owner has a 0% success rate on 19,784
  attempts"* — true of the rows and meaningless, since every row with a
  `failure_owner` is a failure. Correct arithmetic, nonsense sentence, and it
  reads as a finding.

## Charts

The Result tab leads with a chart when there is one to draw. Advice cards get a
**Charts** tab with one small chart per evidence query.

The renderer is the classic UI's `frontend/charts.js` — 797 lines of hand-rolled
SVG, no libraries — **served, not copied**: the v2 app mounts the classic
frontend read-only at `/shared`, so a fix lands in both UIs and the palette
cannot drift between them. v2's stylesheet defines the `--s1 … --s7` series
tokens the renderer reads off `:root`, and the `#tip` host it writes tooltips
into.

**The model proposes, the result decides.** Call 1 writes the chart spec before
a single row exists, so every column it names is a guess. `core/chart.py`
honours the proposal where the real result allows it and derives one where it
does not — one row becomes a KPI, a time column a line, a label plus a measure
a bar, more than eight categories a horizontal bar. Every override is listed
under the chart:

```
Adjusted: x column 'txn_week' is not in the result · 16 categories: switched to a horizontal bar
```

A waterfall is drawn only on the decomposition path, never from ordinary
rows. It is a picture of mix versus rate, and one drawn from a result that
never had that split computed would be a picture of an analysis that did not
happen.

## Why a rate moved

A ratio can fall while every segment holds steady, because traffic moved
toward one that was always weaker. Calling that segment "the problem" is the
classic error, and it sends the wrong team to fix a routing change.

```
delta = Σ (w_t − w_b)·v_b     mix   traffic moved between segments
      + Σ  w_t·(v_t − v_b)    rate  segments changed their own rate
```

Measured here: **−1.33pp overall, of which −1.23pp is mix and −0.09pp is rate
— 92.9% traffic mix.** UPI's share fell 61.9% → 55.0% while netbanking's rose
8.0% → 15.0%, and neither moved its own rate by more than 0.1pp. Nothing
broke; the routing changed.

**The query is built, not authored.** The arithmetic needs one exact shape —
both periods, per segment, numerator and denominator — and free-form SQL
produces that shape by luck, which is why this was out of reach before. Call 1
returns only the metric, the dimension and the two periods; `drivers.py` emits
the SQL from the semantic model, and it still passes check 10 and all nine
guard checks. The ratio is deliberately not aliased as a metric: only the
numerator and denominator are selected and Python divides them, so there is no
column for check 10d to police and no arithmetic outside Python.

`residual_pp` is asserted at ~0 before anything is shown. If the identity does
not close, the weights do not sum to one — and a split that does not balance
is reported as a failure, not as an answer.

## Data quality

Four probes against the window the answer actually used, plus a reconciliation
when the question touches money. They run on every answer and the findings are
appended to the caveats, because a warning nobody surfaces was not issued.

| check | on this dataset |
|---|---|
| `duplicate_txn_ids` | verifies the view's de-duplication held — no txn_id twice |
| `null_issuer` | 2.09% of attempts have no issuer, so issuer rows will not sum |
| `partial_days` | the last 2 days are part-ingested; reported, never trimmed |
| `negative_amount` | refund corrections posted against the original rail |
| `settlement_mismatch` | 5 merchants where transactions and settlements disagree |

Every probe is an ordinary guarded query with the caller's own principal — a
background query is the easiest place to leak rows a role may not see, because
it is not user-initiated and so never looks like the thing to check.

Two differences from how the retired v1 engine did this. Its duplicate check
counted rows in `fact_transactions`; v2's allowlist blocks the raw tables and
that is not relaxed for a probe, so the check verifies the guarantee on the
view instead — which fails loudly if de-duplication ever breaks. And v1
*trimmed* a part-ingested window and re-planned; v2 reports it and leaves the
query alone, because silently answering a different window than the one asked
for is the kind of invisible edit this pipeline avoids everywhere else.

When two sources disagree the answer states the documented precedence and
quantifies the gap. It never averages them and never quietly serves the
larger: a reader handed one number is entitled to know the other existed.

## The NLQ → SQL cache

A repeat question skips **call 1 only**. Everything after it still runs, so the
numbers are measured fresh and the guard still injects the caller's row filter.

```
first ask    author 9,478ms  →  total 14,033ms
second ask   author     6ms  →  total  4,591ms     3.2× faster, same answer
```

The answer itself is never cached. A stored answer is a number that was true
once, with no way to show a reader which of those two it is.

**Two traps this design exists to avoid.** `periods.resolve()` bakes literal
dates into the SQL, so replaying "last week" after the week rolls over would
answer a different question with no error anywhere — the resolved window is
therefore part of the key, and the same words asked later are a miss by
construction. And what is stored is the query *as authored*, never
`sql_executed`, because that text has one role's predicate welded into it;
replay hands the query back to `guard()`, so a cached entry cannot leak a
scope. A merchant account asking a question the CXO cached gets its own filter
injected, and its own number.

Nothing is served the first time it works. A query must produce an equivalent
query twice before it is marked verified — the compiled engine could reuse SQL
immediately because its queries could not be wrong, and a model-authored one
can be plausible and wrong. A replay that fails validation is a miss, not an
error: the record is demoted and the question goes to the model as if the
cache had been empty.

Two layers: an in-process LRU (~1ms) and the Chroma record itself, matched on
an exact metadata key rather than by similarity, so there is no embedding
round-trip. The second survives a restart.

## Conversations

Five threads are kept, matching the classic pipeline's limit. They live in the
**browser**, not on the server: v2's pipeline is stateless by design so a thread
can be replayed exactly when debugging, which means the caller holds the history
and sends it with each question. `+ new` starts a fresh thread; signing in or out
starts one too, because nothing carries across a change of role.

Only the question and the SQL go back as context — what was asked and what ran,
not what was then said about it. A follow-up fills a gap; it never overrides
something the new question states itself:

```
success rate by payment method last week   WALLET 95.02% … NETBANKING 71.15%
what about just UPI                        88.79%   ← period carried over
and CARD instead                           90.06%   ← UPI filter REPLACED, not stacked
```

Periods are resolved in Python (`core/periods.py`) and pasted into the prompt as
literal dates. Telling the model the convention did not work: asked for "last
week" on a Saturday it returned the trailing seven days despite an explicit
instruction not to, because finding the previous Monday is arithmetic. The
window it chose differed from the classic pipeline's by INR 1.25M on the same
question.

## Known limits

- v2 requires a language model. There is no deterministic fallback for
  authoring; with no key it refuses rather than guessing. The decomposition and
  the definitions both have offline templates, so those two paths degrade to
  the arithmetic and the semantic model rather than failing.
- Conversation state is held by the browser. Clearing site data loses the
  threads, and they do not follow you to another device.
- Advisory answers rest on the catalogue, so a question whose shape no playbook
  covers degrades to a measurement rather than inventing counsel. That is the
  intended behaviour, but it means advisory coverage is exactly as wide as
  `playbooks.yaml` and no wider.

## Running it

```bash
python -m paylens2.api.app          # http://localhost:8001  (V2_PORT to change)
```

A separate process on a separate port, not a tab inside the classic app: one
origin serving two products means two front ends fighting over `/` and no way to
restart one without the other. The classic UI stays on :8000 exactly as it was,
and keeps serving if this one is down.

The v2 routes are *also* mounted inside the classic app at `/v2/*` for API use;
the UI is only on :8001.
