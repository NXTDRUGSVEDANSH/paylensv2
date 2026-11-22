# PayLens — Enterprise AI Business Analyst for Payments
## Build Workflow (48-hour vertical slice)

> **Assignment:** Pine Labs — Enterprise AI Business Analyst
> **Status:** **Superseded by the build.** This is the original plan, kept as a record of
> the reasoning. For what actually exists, read [`README.md`](README.md) and
> [`ASSUMPTIONS.md`](ASSUMPTIONS.md).
> **Reference implementation leveraged:** `Desktop/Projects/Fractal AI` — its NLQ pipeline,
> self-healing SQL layer, role personalisation and chart-metadata validation. See §10.

## What changed between this plan and the build

| § | Plan said | Built instead | Why |
|---|---|---|---|
| §12.4, §16.3 | "No vector DB — skip Vanna + ChromaDB entirely" | **Chroma retained**, but only as a tiered Q→SQL cache | The narrower claim is what survives: no vector retrieval **of schema** (the semantic model goes into the prompt whole). The cache is a different job. Vanna was never needed — the reference project does not use it either, despite the dependency. |
| §12 | LLM = Claude Sonnet | **OpenRouter: `qwen/qwen3-coder-next` + `google/gemini-3-flash-preview`** | The reference project's `readme.md` documents Gemini-direct, which is stale; its live config is OpenRouter with these two models. Mirroring it was the instruction. |
| §6 check 7 | "force `LIMIT 5000`" | **Sentinel `LIMIT 5001`** | With exactly the cap you cannot distinguish "at the cap" from "over the cap", and will ship a truncated aggregate as complete. |
| §6 | 8 checks | **9 checks** | Added `assert_rbac_present`: check 6 is satisfied by a predicate an `OR 1=1` has neutralised. |
| §3 | ~600k rows, generic synthetic | **777,757 rows calibrated to NPCI published decline rates**, real bank names | Synthetic-but-calibrated is a far stronger claim than synthetic. See `datasets/CALIBRATION.md`. |
| §7 | mix share ≈ 94% | **92.9%** | Recomputed against *real* UPI approval (88.9%, not the 96% assumed here), which narrows the UPI↔netbanking gap. The honest number. |
| §3.7 | incident starts 2026-11-10 | **2026-11-09 (ISO Monday)** | So that "last week" lands exactly on the incident with no date fudging in the demo. |
| §5 | self-healing a footnote | **First-class pipeline step**, healing the pre-RBAC template | The reference implementation's version can delete its own tenant filter during a "repair". |
| §11 | 30 golden questions | **15**, each with hand-written reference SQL | Reported honestly rather than inflated. |
| §13 | Next.js front end | **Static page served by FastAPI** | Reliable inside the time box; the API is identical either way. |
| §16 | — | Added: four defects found in the reference implementation, and closed | Listed in `ASSUMPTIONS.md`. |

---


---

## 1. What we are building

A business user types a question in plain English:

> *"Why did our success rate drop last week?"*

PayLens returns a single **Answer Card** containing:

| Section | Content |
|---|---|
| **Headline** | One-sentence direct answer |
| **Observed facts** | Only what the data shows, each line traceable to a query |
| **Segmented breakdown** | Table + chart, drilled by the dimensions that matter |
| **Driver analysis** | Mix-effect vs rate-effect decomposition, contribution-ranked |
| **Hypotheses** | Plausible causes, explicitly labelled as *not proven*, with confidence |
| **Recommendations** | Suggested next actions, explicitly labelled |
| **Evidence** | Datasets used, filters, time window, row counts, the exact SQL, runtime |
| **Caveats** | Data-quality warnings, incomplete windows, source disagreements |

The hard requirement that shapes every design decision: **an AI-generated conclusion is worthless unless the user can audit it.** Everything below exists to make the answer checkable.

---

## 2. Design principles (decide these once, never revisit)

1. **Plan before SQL.** The LLM first emits a structured JSON *analysis plan* (metric, dimensions, filters, window, comparison). SQL is then *compiled* from that plan, not free-written. Free-form text-to-SQL is the fallback, not the primary path.
2. **Metrics live in a semantic layer, not in prompts.** `success_rate` has exactly one definition — an explicit numerator and denominator — stored in YAML. The LLM picks metrics; it never invents them.
3. **Nothing executes unvalidated.** Every query passes a validation gate (parse → SELECT-only → table/column allowlist → RBAC filter injection → LIMIT → cost estimate) before touching the database.
4. **RBAC is enforced in SQL, not in the prompt.** Row filters are injected into the compiled AST. A prompt-injection attack cannot lift them.
5. **Fact / hypothesis / recommendation are structurally separate.** They are three distinct fields in the response schema, not three paragraphs the LLM might blend. The LLM is physically unable to present a guess as a fact.
6. **Data quality is part of the answer, not a separate screen.** Caveats are computed on the tables the query actually touched and attached automatically.
7. **Mocked is fine; undisclosed is not.** Every assumption and mock is listed in `ASSUMPTIONS.md`.

---

## 3. Data model

Five related datasets. Synthetic, generated by script, deterministic (fixed seed) so the demo is reproducible.

```
                 ┌──────────────────┐
                 │  dim_merchant    │
                 │  merchant_id PK  │
                 └────────┬─────────┘
                          │
  ┌───────────────┐       │        ┌──────────────────────┐
  │   dim_bank    │       │        │ dim_payment_method   │
  │   bank_id PK  │       │        │ pm_id PK             │
  └───────┬───────┘       │        └──────────┬───────────┘
          │               │                   │
          └───────────┬───┴───────────────────┘
                      ▼
          ┌───────────────────────────┐        ┌────────────────────┐
          │    fact_transactions      │───────▶│ dim_failure_code   │
          │    txn_id PK  (grain:     │        │ failure_code PK    │
          │    one payment attempt)   │        └────────────────────┘
          └─────────────┬─────────────┘
                        │ settlement_id
                        ▼
          ┌───────────────────────────┐
          │    fact_settlements       │
          │    settlement_id PK       │
          └───────────────────────────┘
```

### 3.1 `fact_transactions` — grain: one payment attempt
`txn_id`, `txn_ts`, `merchant_id`, `bank_id` (issuer), `acquirer_bank_id`, `pm_id`, `terminal_id`, `amount`, `currency`, `status` (`SUCCESS` / `FAILED` / `PENDING`), `failure_code`, `latency_ms`, `is_retry`, `original_txn_id`, `settlement_id`, `ingested_at`

Target volume: ~600k rows over 120 days. Large enough that cost controls and caching matter, small enough to ship in the repo.

### 3.2 `dim_merchant`
`merchant_id`, `merchant_name`, `mcc`, `category`, `city`, `state`, `region`, `tier` (`enterprise`/`mid`/`sme`), `onboarded_at`, `risk_band`

### 3.3 `dim_bank`
`bank_id`, `bank_name`, `bank_type` (`issuer`/`acquirer`), `ifsc_prefix`, `is_psp`

### 3.4 `dim_payment_method`
`pm_id`, `method` (`UPI`/`CARD`/`NETBANKING`/`WALLET`/`EMI`), `card_network`, `card_type` (`credit`/`debit`), `is_tokenized`

### 3.5 `fact_settlements` — grain: one settlement batch
`settlement_id`, `merchant_id`, `settlement_date`, `txn_count`, `gross_amount`, `fees`, `tax`, `net_amount`, `status`

### 3.6 `dim_failure_code`
`failure_code`, `category` (`issuer_decline`/`insufficient_funds`/`timeout`/`risk_block`/`technical`), `description`, `is_retriable`, `owner` (`issuer`/`acquirer`/`merchant`/`network`)

### 3.7 Defects planted deliberately

Each planted defect maps to a required failure scenario. **Document each one in `ASSUMPTIONS.md` so the reviewer knows it is intentional.**

| Planted defect | Serves scenario |
|---|---|
| ~1,800 exact-duplicate `txn_id` rows (double-ingest window on day 63) | *Dataset contains missing or duplicated records* |
| `bank_id` NULL on ~2.3% of rows | *Missing records* / denominator ambiguity |
| `fact_settlements.txn_count` for 3 merchants disagrees with `COUNT(*)` from transactions by 4–9% | *Two sources disagree on the same metric* |
| **Mix shift:** in the decline window, the share of a low-SR issuer and of `NETBANKING` grows sharply while **per-segment SR is flat** | *A success-rate decline is explained by a shift in transaction mix* (Simpson's paradox — the showcase insight) |
| Last 2 days only ~40% ingested (`ingested_at` lag) | Incomplete-window warning |
| `amount` negative on a handful of refund-mislabelled rows | DQ range check |

---

## 4. Semantic layer (`semantic/semantic_model.yaml`)

This is the single most important file in the project. It is what makes text-to-SQL *safe* rather than *lucky*.

```yaml
datasets:
  fact_transactions:
    grain: payment_attempt
    primary_key: txn_id
    time_column: txn_ts
    dedupe: "qualify row_number() over (partition by txn_id order by ingested_at) = 1"
    freshness_lag_days: 2          # drives incomplete-window caveat

metrics:
  success_rate:
    label: "Success Rate"
    type: ratio
    numerator:   "count(*) filter (where status = 'SUCCESS')"
    denominator: "count(*) filter (where status in ('SUCCESS','FAILED'))"
    denominator_note: "PENDING excluded — see ASSUMPTIONS.md #4"
    format: percent
    decomposable: true             # eligible for mix/rate decomposition
  gmv: { type: sum, expr: "sum(amount) filter (where status='SUCCESS')" }
  txn_count: { type: count, expr: "count(*)" }
  avg_latency_ms: { type: avg, expr: "avg(latency_ms)" }
  failure_rate: { ... }

dimensions:
  payment_method: { table: dim_payment_method, column: method, synonyms: [mode, instrument, rail] }
  issuer_bank:    { table: dim_bank, column: bank_name, synonyms: [bank, issuer, card bank] }
  merchant_tier:  { table: dim_merchant, column: tier }
  state:          { table: dim_merchant, column: state, rbac_scope: true }
  failure_owner:  { table: dim_failure_code, column: owner }

joins:
  - { from: fact_transactions.bank_id, to: dim_bank.bank_id, type: left }
  - { from: fact_transactions.pm_id,  to: dim_payment_method.pm_id, type: left }
  # ... one entry per edge in §3's diagram

allowlist:
  tables:  [fact_transactions, fact_settlements, dim_merchant, dim_bank, dim_payment_method, dim_failure_code]
  functions_denied: [pg_read_file, read_csv, copy, system]
```

The YAML is rendered into the LLM prompt as a compact schema card. **The LLM never sees the raw database.**

---

## 5. The agent pipeline

```
          User question + role + session
                      │
   ┌──────────────────▼───────────────────┐
   │ 1. INTAKE & AMBIGUITY DETECTION      │  missing time window? ambiguous
   │    resolve "last week", pronouns,    │  denominator? → ask ONE clarifying
   │    follow-ups from session memory    │  question, or state the assumption
   └──────────────────┬───────────────────┘      explicitly and proceed
                      ▼
   ┌──────────────────────────────────────┐
   │ 1b. ROUTE                            │  not every question is a metric
   │     open      → health scan (§5a)    │  question. Routing is phrase-based
   │     theoretic → advisory   (§5b)     │  and deterministic; both branches
   │     otherwise → continue below       │  run AFTER the access-scope check
   └──────────────────┬───────────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 2. SEMANTIC RESOLUTION               │  map NL → {metric, dimensions,
   │    retrieve relevant metrics/dims    │  filters, grain} from YAML only
   └──────────────────┬───────────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 3. ANALYSIS PLAN  (LLM call #1)      │  JSON, schema-validated:
   │                                      │  intent | metric | window |
   │                                      │  baseline | segment_by[] |
   │                                      │  filters[] | needs_decomposition
   └──────────────────┬───────────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 4. SQL COMPILATION                   │  deterministic compiler from plan;
   │                                      │  LLM fallback only for off-plan Qs
   └──────────────────┬───────────────────┘
                      ▼
   ┌══════════════════════════════════════┐
   ║ 5. VALIDATION GATE  (hard blocker)   ║  → §6. Fails closed.
   └══════════════════╤═══════════════════┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 6. EXECUTION                         │  DuckDB, statement timeout,
   │    cache: hash(sql + role)           │  row cap, latency recorded
   └──────────────────┬───────────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 7. DATA QUALITY GATE                 │  run DQ checks for touched tables;
   │                                      │  reconcile txn vs settlement;
   │                                      │  emit caveat objects
   └──────────────────┬───────────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 8. DRIVER ENGINE  (pure Python)      │  mix-vs-rate decomposition,
   │                                      │  contribution ranking — §7.
   │                                      │  NO LLM. Deterministic maths.
   └──────────────────┬───────────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 9. NARRATION  (LLM call #2)          │  receives ONLY computed numbers;
   │                                      │  must fill facts[] / hypotheses[]
   │                                      │  / recommendations[] separately
   └──────────────────┬───────────────────┘
                      ▼
   ┌──────────────────────────────────────┐
   │ 10. EVIDENCE CARD + CONFIDENCE       │  datasets, filters, window, SQL,
   │                                      │  row counts, confidence score
   └──────────────────┬───────────────────┘
                      ▼
              Answer Card → UI
```

**Why two LLM calls and not one:** call #1 decides *what to compute*; Python *computes it*; call #2 only *narrates numbers it was handed*. The model never calculates a percentage, so it cannot hallucinate one. This is the same two-call split that worked in the Fractal AI narrator.

---

### 5b. The advisory path — questions with no computable answer

"How do I improve my success rate", "what is a technical decline", "should I
retry failed payments", "is 86% good". None names a measurement, so the metric
pipeline can only offer a picker.

Handing the sentence to the model is the obvious implementation and it fails in
one specific way: it produces invented benchmarks. So the model writes the prose
and supplies none of the substance.

| Stage | Who | What |
|---|---|---|
| 1. Classify | deterministic | kind of question + catalogue entries, phrase-matched and stem-tolerant |
| 2. Ground | deterministic | probes measured through the **same nine-check guard and row scope**, numbered `E1 … En` |
| 3. Compose | LLM call | allowed exactly two sources: the selected levers and the measured numbers |
| 4. Verify | deterministic | every number must be in the pack; every `[E3]` must resolve |

**One ungrounded number discards the whole composition**; the catalogue answer is
served instead. Substance lives in `semantic/playbooks.yaml` — 10 glossary
entries, 9 playbooks, 29 levers, each with an owner and a PayLens question that
checks it. No similarity ranking: no catalogue match means no answer from this
path, and the question falls through to the normal one.

Reported in the eval as a seventh measure, **advisory grounding**.

## 6. The validation gate (`core/guard.py`)

Every query passes all eight checks. Any failure → query is not executed and the user sees *why*.

| # | Check | Implementation |
|---|---|---|
| 1 | Parses as one statement | `sqlglot.parse()`, reject if `len != 1` |
| 2 | Read-only | AST root must be `Select`; reject `Insert/Update/Delete/Drop/Create/Attach/Copy/Pragma` anywhere in the tree |
| 3 | No multi-statement injection | reject `;` outside string literals |
| 4 | Table allowlist | every table ref ∈ `semantic_model.allowlist.tables` |
| 5 | Column allowlist | every column ref resolves in the semantic model |
| 6 | **RBAC filter injection** | append role predicate to the AST `WHERE` (not string concat) — §8 |
| 7 | Row cap | force `LIMIT 5000` if absent |
| 8 | Cost estimate | `EXPLAIN` → if estimated scan rows > threshold, block and suggest a narrower window; 10s statement timeout |

A failing query is also the **unsafe-query demo**: ask *"delete all failed transactions"* or *"show me everything"* and watch the gate refuse with a specific reason.

---

## 7. Driver engine — the analytical core

This is where the assignment is actually won. For a ratio metric like success rate, a decline has two possible explanations and conflating them is the classic analyst error.

**Mix-vs-rate decomposition.** For segments *i* with baseline volume share `w_i^B` and segment success rate `SR_i`:

```
ΔSR  =  Σ_i (w_i^T − w_i^B) · SR_i^B     ← MIX effect   (traffic moved to worse segments)
      + Σ_i  w_i^T · (SR_i^T − SR_i^B)   ← RATE effect  (segments themselves got worse)
```

Run this over every decomposable dimension (payment method, issuer bank, merchant tier, state), then rank dimensions by how much of `ΔSR` each explains. The planted mix shift (§3.7) means the honest answer to *"why did SR drop?"* is:

> **Observed:** overall SR fell 1.8pp. Per-segment SR is flat (rate effect = −0.1pp). 94% of the decline is mix effect: NETBANKING share rose from 8% to 19% and NETBANKING SR is structurally 71% vs UPI's 96%.
> **Hypothesis (medium confidence):** a checkout-page or routing change pushed traffic to netbanking.
> **Recommendation:** check routing config for the window; the issue is traffic composition, not degraded processing.

A naive system says "netbanking is failing." That distinction is the headline of the demo.

**Also implemented:** per-segment contribution ranking (`Δmetric × volume_weight`), cohort comparison (two filter sets side by side), drill-down (expand any segment row one level deeper).

---

## 8. Role-based access control

Four roles. Filters are injected into the AST; also used to mask columns and to scope which metrics are offered.

| Role | Row scope | Column restrictions |
|---|---|---|
| `cxo` | all rows | none |
| `regional_manager` | `dim_merchant.region = <assigned>` | none |
| `merchant_user` | `fact_transactions.merchant_id = <own>` | cannot see other merchants' names; `fees`/`net_amount` of others blocked |
| `analyst` | all rows | `terminal_id` and merchant contact fields masked |

**Access-control failure demo (required):** log in as `merchant_user` for merchant `M-1042`, ask *"compare my success rate to Merchant M-2201"*. The system must not silently return filtered-but-wrong numbers — it detects that a requested entity lies outside scope and returns an explicit refusal naming the out-of-scope entity, plus what the user *can* ask instead.

---

## 9. Data quality & reconciliation engine

Checks registered per dataset, run on the tables a query touched, results attached as typed caveats (`severity`: `info` / `warn` / `block`).

| Check | On trigger |
|---|---|
| Duplicate PK | *"1,804 duplicate `txn_id` rows found in this window; results are de-duplicated by latest `ingested_at`."* |
| Null dimension key | *"2.3% of rows have no issuer bank; these are excluded from the bank breakdown but included in the total — segment sums will not reconcile to the total."* |
| Incomplete window | *"The last 2 days are only ~40% ingested. Trend endpoints are unreliable; the window was trimmed to exclude them."* |
| Source disagreement | *"`fact_settlements.txn_count` reports 8,940 for M-1042; `fact_transactions` counts 9,615 (−7.0%). Transactions is the system of record at attempt grain; settlements excludes refunded batches. Reported figure uses transactions."* |
| Range violation | *"47 rows have negative amount (refund mislabelling); excluded from GMV."* |

The source-disagreement case is answered with a **documented precedence rule**, not by picking the bigger number. That rule lives in the semantic model.

---

## 10. Confidence scoring

Reported alongside every answer, computed not guessed:

```
confidence = base
  − 0.15  if a DQ warn-level caveat applies to the touched tables
  − 0.20  if the window is incomplete
  − 0.15  if the plan required assuming an unstated time window or denominator
  − 0.10  if sample size in any reported segment < 500 attempts
  − 0.25  if SQL needed self-healing to run
  + 0.10  if the question matched a golden eval question
```
Buckets: `high` ≥ 0.75, `medium` 0.5–0.75, `low` < 0.5. The UI shows the deductions, so "medium confidence" is explainable rather than decorative.

---

## 11. Evaluation harness (`eval/`)

30 golden questions with known answers, computed by hand-written reference SQL.

| Metric | Definition |
|---|---|
| **Execution accuracy** | result set equals reference result (primary metric) |
| **Metric-value accuracy** | headline number within 0.1% of reference |
| **Schema precision** | tables/columns used ⊆ reference set (catches wrong-join answers that happen to look right) |
| **Guard precision/recall** | on 10 adversarial prompts (injection, DDL, scope escape), fraction correctly blocked — zero false negatives required |
| **Ambiguity handling** | on 5 deliberately ambiguous questions, did it ask or state the assumption? |

`python -m eval.run` prints a scorecard. Run it before recording the demo and paste the table into the README — a measured system reads very differently from a demoed one.

---

## 12. Tech stack & what to reuse from Fractal AI

| Layer | Choice | Rationale |
|---|---|---|
| Database | **DuckDB** (single file in repo) | Zero setup for the reviewer, real analytical SQL, fast `EXPLAIN`. Postgres is a swap-in behind one connection module. |
| Backend | **FastAPI** | Directly mirrors Fractal AI's `backend/api` layout |
| SQL safety | **sqlglot** | AST-level validation and RBAC injection |
| LLM | Claude (Sonnet 5.5 for planning/narration) behind a provider shim | Mirrors `llm_config.py` — one swappable module |
| Frontend | **Next.js + Recharts** | Reuse Fractal AI's `dashboard_frontend` shell, chat page and chart components |
| Data gen | Python + numpy, fixed seed | Reproducible defects |

### Directly portable from `Desktop/Projects/Fractal AI`

| Fractal AI file | Reuse as |
|---|---|
| `backend/intelligence/nlq/self_healing.py` | SQL retry loop — **but re-validate the healed SQL through the guard before re-executing** (the original re-runs it directly; that is a hole for this assignment) |
| `backend/intelligence/nlq/role_config.py` | Role persona pattern → extend from *tone-shaping* to *access-enforcing* |
| `backend/intelligence/nlq/session_service.py` | Follow-up / multi-turn context |
| `backend/intelligence/nlq/context.py` | Live schema card rendering |
| `backend/api/main.py`, `deps.py` | App + connection-pool scaffold |
| `dashboard_frontend/app/query/` | NLQ chat page shell |
| `docs/nlq-pipeline.md` | Documentation template — match this depth |

### Deliberate departures from Fractal AI

1. **Plan-first, not SQL-first.** Fractal AI goes straight to Vanna RAG → SQL. We insert a validated JSON plan, which is what makes the analysis auditable and the SQL checkable.
2. **A real validation gate.** Fractal AI executes generated SQL directly. For a payments assignment that cites "unsafe query" as a failure scenario, the gate is non-negotiable.
3. **RBAC in SQL, not in the prompt.** Fractal AI's roles shape tone; ours shape rows.
4. **No vector DB / ChromaDB.** 6 tables and ~12 metrics fit in a prompt. Skip Vanna+Chroma entirely — it is the single biggest time sink avoided.
5. **No LIDA.** Chart type comes from the plan's metric/dimension shape via a 20-line rule table.

---

## 13. Repository layout

```
Pine Labs/
├── WORKFLOW.md                 ← this file
├── README.md                   setup, demo script, eval scorecard
├── ASSUMPTIONS.md              assumptions, mocks, limitations  ← graded
├── data/
│   ├── generate.py             synthetic generator (seeded, plants defects)
│   └── paylens.duckdb          generated artifact
├── semantic/
│   ├── semantic_model.yaml     metrics, dimensions, joins, allowlist
│   └── loader.py               YAML → prompt schema card + compiler input
├── backend/
│   ├── api/
│   │   ├── main.py             FastAPI app
│   │   └── routes/ask.py       POST /ask, /datasets, /dq, /eval
│   ├── core/
│   │   ├── planner.py          LLM call #1 → validated AnalysisPlan
│   │   ├── compiler.py         plan → SQL
│   │   ├── guard.py            the 8-check validation gate
│   │   ├── executor.py         DuckDB + timeout + cache
│   │   ├── rbac.py             roles, row filters, scope checks
│   │   ├── dq.py               quality checks + reconciliation
│   │   ├── drivers.py          mix/rate decomposition, contribution
│   │   ├── narrator.py         LLM call #2 → facts/hypotheses/recs
│   │   ├── confidence.py       scoring
│   │   └── schemas.py          Pydantic: AnalysisPlan, AnswerCard, Caveat
│   └── llm/provider.py         provider shim
├── frontend/                   Next.js — chat, charts, evidence panel,
│                               role switcher, DQ banner
└── eval/
    ├── golden.yaml             30 questions + reference SQL
    ├── adversarial.yaml        10 guard tests
    └── run.py                  scorecard
```

---

## 14. 48-hour phase plan

Each phase ends with something runnable. Never leave a phase with a broken tree.

| Phase | Hours | Deliverable | Done when |
|---|---|---|---|
| **0 — Scaffold** | 0–2 | Repo layout, `ASSUMPTIONS.md` started, FastAPI `/health`, DuckDB connection | `curl /health` returns 200 |
| **1 — Data** | 2–8 | `generate.py`, 6 tables loaded, all defects of §3.7 planted and documented | `SELECT` on each table; a script prints each planted defect's row count |
| **2 — Semantic layer** | 8–13 | `semantic_model.yaml` + loader; schema card renders | `GET /datasets` lists metrics and dimensions with definitions |
| **3 — Planner + compiler** | 13–19 | LLM → `AnalysisPlan` → SQL; 5 questions compile correctly | Plan JSON validates against Pydantic for all 5 |
| **4 — Guard + RBAC + executor** | 19–25 | All 8 checks; 4 roles; cache + timeout | All 10 adversarial prompts blocked; scope-escape refused |
| **5 — DQ engine** | 25–29 | Checks + reconciliation, typed caveats attached to answers | Every §9 caveat fires on a real question |
| **6 — Driver engine** | 29–34 | Mix/rate decomposition, contribution ranking, drill-down, cohorts | Decomposition on the planted mix shift attributes ≥90% to mix |
| **7 — Narrator + evidence** | 34–38 | LLM call #2, strict 3-way separation, confidence, evidence card | Answer card renders end-to-end as JSON |
| **8 — Frontend** | 38–44 | Chat UI, chart, table, evidence panel, caveat banner, role switcher | Full demo clickable in the browser |
| **9 — Eval + docs** | 44–48 | `eval/run.py` scorecard, README with demo script, `ASSUMPTIONS.md` finalised | Scorecard in README; demo rehearsed twice |

**If you fall behind, cut in this order:** frontend polish → cohort comparison → caching → drill-down. **Never cut:** the guard, the DQ caveats, the mix/rate decomposition, or `ASSUMPTIONS.md`. Those four are what is actually being evaluated.

---

## 15. Demo script (rehearse this; it is the deliverable)

Seven minutes, five questions. Covers the minimum demonstration plus every required failure scenario.

**1. The headline question — mix shift.**
> *"Why did our overall success rate drop last week?"*

Shows: plan → SQL → segmented table → mix-vs-rate chart → "94% of the decline is traffic mix, not degraded processing" → hypotheses labelled separately → caveats. **This single answer satisfies the minimum demonstration on its own.**

**2. Data quality failure.**
> *"How many transactions did merchant M-1042 process in October?"*

Hits duplicate rows, the null-bank gap, and the settlement disagreement. Shows all three caveats, the de-duplication rule, and the documented precedence rule picking transactions over settlements — with the gap quantified rather than hidden.

**3. Access-control failure.**
Switch role to `merchant_user (M-1042)`:
> *"Compare my success rate with Merchant M-2201."*

Explicit refusal naming the out-of-scope entity, plus what they *can* ask. Then show the generated SQL carries the injected `merchant_id` filter — enforcement is in the query, not the prompt.

**4. Ambiguity.**
> *"What's our success rate?"*

No time window, and the denominator is genuinely ambiguous (does PENDING count?). System states both assumptions explicitly, answers under them, and offers the alternative denominator with one click.

**5. Unsafe / expensive query.**
> *"Delete all failed transactions"* then *"show me every single transaction row ever"*

Guard refuses with the specific check that tripped, and for the second, suggests a narrower window and offers the aggregate instead.

Close on the eval scorecard: *"30 golden questions, 10 adversarial prompts, here is the measured accuracy."*

---

## 16. Known limitations to state up front (`ASSUMPTIONS.md`)

Writing these down scores better than hoping they go unnoticed.

1. Data is synthetic; defect rates were chosen to be demonstrable, not realistic.
2. Session memory is in-process — no multi-worker persistence.
3. No vector retrieval; schema fits the context window. Would not scale past ~50 tables without a retrieval layer.
4. `PENDING` is excluded from the success-rate denominator by default. This is a modelling choice; the alternative is one click away.
5. RBAC roles are hard-coded, not backed by an identity provider. JWT claims are the swap-in point.
6. The driver engine finds *statistical* contributors, not causes. Causal language appears only in the hypotheses field, with confidence.
7. Cost control uses DuckDB's `EXPLAIN` estimate — a heuristic, not a real workload manager.
8. Settlement reconciliation uses a documented precedence rule, not a true two-source resolution workflow.
9. No PII in the dataset by construction, so masking is demonstrated on proxies (`terminal_id`).

---

## 17. First actions

1. `mkdir` the §13 layout, `git init`, start `ASSUMPTIONS.md` as an append-only log from the first hour.
2. Write `semantic/semantic_model.yaml` **before** any pipeline code. Everything downstream is a consumer of it.
3. Write `data/generate.py` with the defects in from the start — do not add them later, or the DQ engine gets built against clean data and the caveats become theatre.
4. Copy `self_healing.py`, `session_service.py` and `context.py` out of Fractal AI early, then adapt — do not retype them.
