# PayLens v2 — LLM-authored SQL pipeline

A second, self-contained pipeline that lives beside the existing one and never
modifies it. Mounted at `/v2/*`; the classic pipeline keeps `/ask` untouched.

## Why a second pipeline

The classic pipeline resolves a question deterministically and *compiles* SQL from
the semantic model. It cannot get a metric definition wrong, and it cannot answer a
question whose vocabulary it does not already hold — the coverage check blocks and
offers a fixed five-option picker.

v2 inverts that: one LLM call authors the SQL, with the schema and retrieved
question→SQL examples in the prompt. Reach is no longer bounded by a hand-written
word list. The price is that the model can now get a definition wrong, which is what
sections 2 and 3 below exist to prevent.

## The flow

```
question
  └─ 1  CALL 1 · author          schema card + catalogue + few-shots      [LLM]
  │      └─ returns exactly one of: sql | clarify | refuse
  └─ 2  check 10                 dedupe present · metric formula verbatim
  └─ 3  GUARD                    the EXISTING 9 checks, unmodified
  └─ 4  execute + row cap        aggregate over cap blocks; detail truncates
  └─ 5  profile                  deterministic stats → named facts E1…En   [no LLM]
  └─ 6  CALL 2 · synthesise      sees only the profile, never raw rows     [LLM]
  └─ 7  verify                   every number in prose ∈ the fact pack
  └─ 8  heal                     on guard/SQL error, re-author with the error [LLM]
```

Worst case 4 calls: author, 2 heals, synthesise (+1 optional repair).

## 1 · What v2 reuses rather than re-implements

Imported from `backend/`, never copied:

| Module | Why it must not be duplicated |
|---|---|
| `core/guard.py` | 9 AST checks incl. RBAC injection; 325 tests cover this copy |
| `core/rbac.py` | row predicates, masking, scope violations |
| `core/executor.py` | timeout, row cap, serialisation |
| `core/db.py` | the read-only DuckDB connection |
| `llm/provider.py` | transport, retries, call budget |
| `semantic/loader.py` | the YAML → `SemanticModel` loader (v2 passes its own YAML) |

v2 writes only what is genuinely new: the two prompts, the view layer, check 10,
the profiler, the synthesiser and the routes.

## 2 · The views — why the LLM never sees a raw table

Two facts about this dataset defeat a naive text-to-SQL system:

1. `fact_transactions` contains deliberate duplicate `txn_id`s (a planted
   double-ingest). Every query must de-duplicate or counts inflate.
2. `success_rate` excludes PENDING from the denominator. `successes / count(*)` is
   a plausible, wrong, different number.

So `schema/views.sql` bakes **`v_attempts`**: one row per de-duplicated attempt,
every dimension pre-joined, plus boolean indicator columns (`is_success`,
`is_resolved`, `is_business_decline`, …). The model queries that and cannot forget
a rule it never knew existed. Joins disappear from the prompt too, which removes
the second largest class of text-to-SQL error.

`fact_transactions` and the `dim_*` tables are **absent from v2's table allowlist**,
so a query naming one is blocked by guard check 4.

## 3 · The security hole this design had to close

`rbac.assert_injected` (guard check 9) skips tables the query does not name:

```python
targets = _selects_touching(root, pred.table)
if not targets:
    continue          # ← vacuously passes
```

The role scopes in `semantic/semantic_model.yaml` anchor on `fact_transactions`.
A v2 query reads `v_attempts`, names no `fact_transactions`, and would therefore
have **no row filter injected and check 9 would pass anyway** — a merchant_user
would see every merchant's rows.

Fix: v2 loads its **own** semantic YAML (`schema/views.yaml`) through the existing
loader, declaring `anchor_table: v_attempts`. `guard()` is parameterised by the
model it is handed, so predicates are injected on the view and check 9 has a real
target. No backend code changes; the enforcement is genuinely the same code.

`tests/test_v2_rbac.py` asserts a merchant_user's generated SQL carries
`merchant_id = 'M-xxxx'` as a top-level conjunct, and that stripping it fails the
guard.

## 4 · Check 10 — the backstop

Runs before the guard, on v2 SQL only:

- the query reads only v2 views (raw `fact_*` / `dim_*` rejected)
- if it names a known metric by alias, the canonical expression appears verbatim
- no `SELECT *` on `v_attempts` (28 columns into a synthesis prompt is waste)

## 5 · The profiler — why Python computes and the model does not

Call 2 cannot receive 5,000 rows, and must not compute. `core/profile.py` produces
a deterministic summary: row count, column types, null counts, min/max/mean/median/
p95 per numeric column, top-k per categorical, plus derived facts (deltas, shares,
totals) as a numbered pack `E1 … En`. Call 2 may state a number only if it is in
that pack — enforced by reusing the existing grounding check's rules.

This is why there is **no calculator tool**. A number from a tool call has no
provenance to show in the Evidence tab; a number from the pack traces to SQL.

## 6 · Advisory questions

The catalogue (`semantic/playbooks.yaml`: 10 glossary entries, 9 playbooks, 29
levers with owners) is passed **in the prompt**, not retrieved by similarity. At 19
entries, retrieval costs an embedding round-trip and a threshold to tune in order to
choose among nineteen things that fit in a few hundred tokens — and a playbook
selected because it scored least badly is unauditable. Call 1 returns the ids it
used; Python verifies they exist. Chroma stays where its corpus is genuinely large:
the question→SQL few-shots.

## 7 · Routes

```
POST /v2/ask        question, role, claims → V2Answer
GET  /v2/health     view presence, model config, KB availability
GET  /v2/schema     the exact schema card sent to call 1 (for debugging)
```

`V2Answer` carries: answer prose, the SQL as authored and as executed, the fact
pack, the chart spec, the profile, grounding verdict, trace of every LLM call.

## 8 · KB write-back

v2 records are written as `source=v2, verified=false` — usable as few-shot
examples, never for tier-1 exact reuse, until a repeat success promotes them. One
bad query must not teach the next ten.
