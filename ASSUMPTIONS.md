# Assumptions, mocked dependencies and limitations

The brief's scope note asks for these to be stated explicitly. This is the honest
list, including the things a reviewer would otherwise find.

---

## 1. The data

1. **The data is synthetic.** Row-level payment logs are private; nothing public
   exists at transaction grain. Rows are generated, with a fixed seed (42).
2. **It is calibrated, not invented.** UPI decline rates, the approval rate and the
   three named outlier banks reproduce NPCI's published September 2026 figures, and
   `data/verify.py` asserts the generated rows still match. Full provenance per
   constant: [`datasets/CALIBRATION.md`](datasets/CALIBRATION.md).
3. **Only UPI rates are NPCI-derived.** NPCI does not publish success rates for
   cards, netbanking, wallets or EMI. Those are industry-typical assumptions and are
   tagged `[MODELLED]`, not presented as published figures.
4. **The portfolio mix is assumed, not national.** By national NPCI volume UPI is
   ~97% of rails, which would make every other rail statistically invisible. The 62 /
   22 / 8 / 5 / 3 split is a mid-market merchant-acquirer checkout mix.
5. **Per-rail success rates are allocated deterministically, not sampled.** With ~42k
   attempts a week, Bernoulli sampling error (~0.15pp) exceeds the −0.10pp rate
   effect the dataset exists to demonstrate, so random draws would blur the very
   signal being measured. Row-level realism is unaffected; only the aggregate is
   pinned.
6. **Defects are planted on purpose**, all seven of them, each mapping to a required
   failure scenario. They are documented rather than hidden, and asserted by
   `data/verify.py` so they cannot silently disappear.
7. **Windows are aligned to ISO weeks** so that "last week" lands exactly on the
   planted incident. This is a convenience for the demo, stated rather than concealed.

## 1b. The payment journey, and every invented share

The failure model was rebuilt around the ten steps of a Pine Labs payment, so that
"where did it break" is a groupable question rather than a narrative. That required
inventing a code-level breakdown NPCI does not publish. Every invented number is
below. Nothing here is an NPCI figure.

### Why the technical decline rate is 1.5%, not NPCI's 0.24%

This is the one place where the journey model and the published figure genuinely
disagree, so it is resolved explicitly rather than averaged away.

NPCI's technical decline counts faults **at the bank or at NPCI** — issuer down,
issuer timeout, response lost. It does not count a logged-out terminal, a faulty
card reader, a dead SIM or a gateway bug, because none of those ever reach NPCI's
switch. A card-present estate therefore has strictly more technical failure than
NPCI's UPI number, and quoting 0.24% as the technical decline of a POS estate
would understate it roughly five-fold.

So the model is split:

| | |
|---|---|
| UPI | keeps the NPCI anchors exactly — BD 10.88%, TD 0.24% of UPI attempts, including the per-bank profile (ICICI's 0.73% TD, Fino's 24.26% BD) |
| Card-present rails | carry the terminal- and gateway-side technical codes inside their own already-modelled failure budget |
| `npci_td_scope` | a column on `dim_failure_code` marking the codes NPCI would actually count, so the comparable subset stays auditable |

Measured result: overall technical decline **1.47%** of attempts, of which
**0.47pp** is inside NPCI's definition. UPI alone still measures BD 10.89% / TD
0.247% against published 10.88% / 0.24%. `data/verify.py` asserts both separately,
and the `technical_decline_rate` metric says in its own `denominator_note` that it
is broader than the published figure.

### Invented shares — failure-code weights `[MODELLED]`

Relative weights within each class, in `data/npci_calibration.py`. They are ordered
so `INSUFFICIENT_FUNDS` and `WRONG_PIN` are the two largest codes, which is what
every published decline post-mortem reports. Measured in the generated data:
33.3% and 25.9% of all failures respectively.

| Business code | weight | Technical code | weight |
|---|---|---|---|
| `INSUFFICIENT_FUNDS` | 0.34 | `NETWORK_TIMEOUT` | 0.22 |
| `WRONG_PIN` | 0.26 | `ISSUER_TIMEOUT` | 0.12 |
| `CHIP_READ_ERR` | 0.14 | `TERM_OFFLINE` | 0.12 |
| `EMI_INELIGIBLE` | 0.30 (EMI only) | `READER_FAULT` | 0.10 |
| `TAP_LIMIT` | 0.10 (tap only) | `GATEWAY_ERROR` | 0.10 |
| `LIMIT_EXCEEDED` | 0.09 | `ISSUER_DOWN` | 0.08 |
| `USER_ABANDONED` | 0.08 | `QR_GEN_FAIL` | 0.08 (QR only) |
| `CARD_BLOCKED` | 0.07 | `ACQUIRER_DOWN` | 0.06 |
| `PIN_TRIES_EXCEEDED` | 0.05 | `TERM_LOGGED_OUT` | 0.06 |
| `FRAUD_BLOCK` | 0.04 | `ROUTED_TO_DEGRADED` | 0.05 |
| `AMT_MISMATCH` | 0.03 | `NETWORK_OUTAGE` | 0.04 |
| `OFFER_INVALID` | 0.03 | `RESPONSE_LOST` | 0.04 |
| | | `DUPLICATE_CHARGE` | 0.02 |
| | | `BATCH_NOT_CLOSED` | 0.01 |
| | | `SETTLEMENT_MISMATCH` | 0.01 |

### Invented shares — technical share of failures per rail `[MODELLED]`

| Rail | technical share of its failures |
|---|---|
| UPI | **not invented** — derived per bank from the NPCI BD/TD ratio (~2.2%) |
| CARD | 0.25 |
| EMI | 0.22 |
| NETBANKING | 0.12 |
| WALLET | 0.10 |

### Invented shares — entry-mode mix per rail `[MODELLED]`

| Rail | tap | chip | swipe | qr | online |
|---|---|---|---|---|---|
| UPI | | | | 0.55 | 0.45 |
| CARD | 0.46 | 0.38 | 0.10 | | 0.06 |
| NETBANKING | | | | | 1.00 |
| WALLET | | | | 0.28 | 0.72 |
| EMI | | 0.62 | 0.13 | | 0.25 |

### Invented shares — the terminal estate `[ASSUMED]`

| Choice | Value | Reasoning |
|---|---|---|
| Terminals per merchant | 6 enterprise / 3 mid / 1 SME → 712 total | plausible estate shape |
| Wi-Fi share | 82% in metros, 38% elsewhere | broadband availability |
| Software version mix | 46 / 28 / 17 / 9% newest→oldest | a normal upgrade tail |
| Degraded probability | 10% base, +14 on SIM, +18 on stale firmware, +6 non-metro | the causal story an estate manager acts on |
| Degraded tech boost | ×2.2 on the technical share of failures | — |
| SIM tech boost | ×1.45 on the technical share of failures | — |

**Important:** terminal condition changes **why** a payment fails, not **how
often**. The success-rate surface is untouched, so the mix-vs-rate decomposition
stays exactly as calibrated and the headline claim is unaffected. The boosts apply
only to card-present modes — a reader fault is impossible on a QR scan, and
boosting UPI would break the NPCI anchor.

### Invented — the second planted story `[ASSUMED]`

Defect 7: a three-hour acquirer outage, 2026-10-28 14:00–17:00, acquirer B-53
(ICICI Acquiring). 45% of successes in the window flipped to failed and relabelled
`ACQUIRER_DOWN` / `ISSUER_TIMEOUT` (72/28). Pre-existing business declines in the
window are deliberately **left alone** — a customer still mistypes a PIN during an
outage, and removing them would make the incident cleaner than any real one.

It is placed **outside** the mix-shift comparison weeks (2026-11-02 … 11-15) on
purpose. The two stories must be independently diagnosable: inside the window it
would leak a rate effect into the mix decomposition, and verify.py's exact-identity
assertion would be measuring both at once. `verify.py` asserts the separation.

Effect in the data: success rate inside the window **47.0%** against 87.8%
elsewhere, and the daily technical decline rate rises from ~1.40% to **2.62%** on
that date — visible in the KPI Lab's trend and acquirer panels without being
pointed at.

## 1c. Conversation memory

1. **History is structured, not textual.** A follow-up is completed from the
   previous turn's metric, window, breakdown and filters — never by feeding prior
   text to a model. The LLM has no role in deciding what "what about UPI" means.
2. **Inheritance only fills gaps.** A question that stands on its own is answered
   as typed. History can never override what the user just wrote.
3. **Everything inherited is displayed.** An assumption the user cannot see is one
   they cannot correct — the same failure mode as the "priya" bug.
4. **Five threads per viewer**, pruned on write. There is no scheduler here, and a
   prune that only runs when the table grows is a prune that actually runs.
5. **Anonymous visitors get history too**, keyed by an httpOnly browser cookie in
   a namespace that cannot collide with a username. The key carries no role and
   grants no access; the access scope still comes from the session.
6. **A conversation id is not a capability.** Every read and write filters on the
   viewer key in SQL, and an id that belongs to someone else returns the same 404
   as one that does not exist.

## 1d. The health scan

7. **It is a fixed battery, not a search.** Seven declared checks, each with a
   written threshold. Ranking by embedding similarity was rejected: it has no
   account of why one finding outranked another, so it cannot be audited.
8. **Thresholds are judgement calls, stated in the code.** A finding must clear
   both a gap of 1.0pp and 150 recoverable attempts. A 9pp gap on 40 attempts is
   noise with a dramatic percentage.
9. **The default window ends on the last COMPLETE day**, not today. The dataset's
   final two days are deliberately part-loaded; a scan ending there would report a
   volume collapse that is an ingest artefact.
10. **Causes are labelled as inferences.** Measured gaps are facts, the reason for
    a gap is a hypothesis with a query that would settle it, and the fix is a
    recommendation with a named owner. Collapsing the three is how a report starts
    asserting causes it only inferred.

## 2. The language model

8. **The system runs fully without an API key.** With `LLM_API_KEY` unset, the
   deterministic resolver plans and template narration writes the answer. Every
   number, caveat, decomposition and refusal in the demo is produced this way — the
   measured scorecard below was produced with **zero LLM calls**. The model adds
   fluency and handles genuinely ambiguous questions; it is not load-bearing for
   correctness. That is a deliberate design property, not a fallback.
9. **Two models, mirroring the reference project**: `qwen/qwen3-coder-next` for
   planning/SQL/healing, `google/gemini-3-flash-preview` for narration, both over
   OpenRouter with one key.
10. **The model never computes a number.** It chooses a metric id and writes prose
    around values Python calculated. It cannot hallucinate a percentage because it is
    never asked for one.

## 3. Access control

11. **Signing in is OPTIONAL by default.** An unauthenticated visitor browses as
    `ANON_ROLE` (default `cxo`, unrestricted) — appropriate for a demo where the
    point is to look around, and wrong for anything else. Two switches change it:
    `REQUIRE_AUTH=true` restores a full-screen gate before any access, and
    `ANON_ROLE=merchant_user` would make anonymous browsing narrow instead of
    unrestricted. **The enforcement machinery is identical either way** — this
    setting only decides whether an unauthenticated visitor is turned away, not
    whether scope is enforced once they are identified.

12. **There is a real sign-in.** Username + password, PBKDF2-HMAC-SHA256 (600k
    iterations, per-user salt), server-issued session tokens stored hashed in a
    separate SQLite file. The role and its scope claims come from the stored user
    record — `/ask` no longer accepts `role` in the request body, so the access
    role can no longer be self-asserted. Four demo accounts are seeded and their
    password is shown on the sign-in screen **on purpose**; set
    `SEED_DEMO_USERS=false` for anything real and provision via an identity
    provider. `REQUIRE_AUTH=false` is a local-development mode that runs
    everything as an unrestricted CXO and labels itself in the header.
13. **There is no PII in the dataset by construction**, so column masking is
    demonstrated on proxies (`terminal_id`, other merchants' names) rather than on
    real personal data.
14. **Scope enforcement is SQL-level and verified** (guard check 9 re-walks the AST
    to confirm the predicate survives as a top-level conjunct). It is not a prompt
    instruction. Prompt injection cannot lift it, and the adversarial suite tests
    exactly that.

## 4. Analysis

15. **The driver engine finds statistical contributors, not causes.** Mix-vs-rate
    decomposition is arithmetic and exact (`residual < 1e-9`). Causal language
    appears only in the `hypotheses` field, with a confidence level and a stated way
    to test it.
15. **`PENDING` is excluded from the success-rate denominator.** This is a modelling
    choice, surfaced as an assumption on every answer, with
    `success_rate_incl_pending` available as the alternative.
16. **Settlement/transaction disagreement is resolved by a documented precedence
    rule**, not by picking a number. The gap is quantified in the caveat and the
    reason given. It is not a true two-source reconciliation workflow.
17. **Dimensions that exist only on a row subset carry an implied filter**
    (`failure_category` implies `status = 'FAILED'`). Without this, "failures by
    category" silently includes a NULL bucket of every successful attempt.

## 5. Caching

18. **Three caches, different jobs.** L0 (exact question hash) ~10ms; L1 (semantic
    Q→SQL in Chroma) skips the planner but still pays ~200ms to embed; L2 (answer
    card keyed on post-RBAC SQL). The published "sub-100ms repeat" refers to L0 only.
19. **The embedding is local** (ONNX MiniLM-L6-v2 via Chroma), so the cache costs no
    API calls — but the first call after boot pays a ~25s model load, which the
    server does at startup rather than inside a request.
20. **Cache reuse requires more than similarity.** Tier 1 needs cosine ≥0.95 AND an
    equal shape key AND an exact parameter match AND a lexical floor AND no
    unresolved literals. Similarity alone would answer "last month" with last week's
    SQL; the regression test for that is in `tests/test_normalize.py`.
21. **Learned records are gated and resettable.** Healed queries are quarantined and
    never tier-1 eligible; zero-row results are never stored at all.

## 6. Scale and infrastructure

22. **DuckDB, single file, ~778k rows.** Postgres is a swap behind `core/db.py`.
    Nothing in the design depends on DuckDB except the `EXPLAIN` cost parser.
23. **Caches are in-process**, so they do not survive a restart or span workers.
    Redis is the stated swap-in.
24. **No vector retrieval of schema.** The semantic model (9 metrics, 13 dimensions)
    fits in a prompt. Past ~50 tables this would need retrieval.
25. **Cost control uses DuckDB's `EXPLAIN` estimate**, a heuristic that is routinely
    an order of magnitude out in both directions. It is therefore set generously and
    used only to block the catastrophic case; the real protection is the sentinel row
    cap, which is measured after execution.
26. **The statement timeout is a watchdog thread** calling `interrupt()`, because
    DuckDB has no native statement timeout.

## 7. Scope deliberately not built

27. **No proactive monitoring / insight feed.** The brief is question-driven; this
    was planned (detectors, suppression state machine) but cut as stretch work that
    competes with the required scenarios.
28. **Three action preconditions are declared but never fire**
    (`timeout_rising_with_latency`, `acquirer_underperforms_on_same_mix`,
    `risk_block_share_rising_low_risk_tier`) because they need signals the current
    query does not fetch. They are left in `semantic/actions.yaml` as the honest
    shape of the catalogue rather than deleted to make the demo look complete.
29. **The front end is a static page served by FastAPI**, not the Next.js app the
    reference project uses. It was the reliable choice inside the time box; the API
    is unchanged either way.
30. **The evaluation set is 15 golden questions, not 30.** Each has hand-written
    reference SQL. A larger set would be better; the number is reported honestly
    rather than inflated.
31. **Multi-turn follow-ups are not implemented.** `session_service`-style context
    was designed for but not built; each question is independent.

## 8. Known rough edges

32. Sweeps/DQ probes re-query per request rather than sharing a plan — correct but
    not optimal.
33. The `compile_freeform` escape hatch exists for questions the planner cannot
    express as a plan, but no question in the demo set uses it, so it is lightly
    exercised.
34. Chart series fold to "Other" beyond 7 categories; for a 15-bank breakdown that
    means the tail is aggregated in the chart (the table shows all rows).

---

## Defects found in the reference implementation and closed here

The reference project (`Fractal AI`, a prior NLQ analytics platform) was used as the
architectural starting point. Four defects were found while porting and are fixed
here — listed because "we reused this" should come with "and here is what we
checked".

| # | Defect in the reference | Consequence | Closed by |
|---|---|---|---|
| 1 | Chroma collection created without `hnsw:space` | Default is squared **L2**, so `query()` returns distances; a "≥0.95 similarity" gate is meaningless *and inverted*. `get_or_create` silently ignores conflicting metadata, so the error is invisible. | Cosine declared **and asserted on open** (`kb/store.py`) |
| 2 | Self-healed SQL re-executed with no revalidation, after the tenant filter was already wrapped around it | Deleting the wrapper is a *plausible repair* for the most likely error, so a correct-looking fix produces a cross-tenant leak | Healing operates on the **pre-RBAC template**; every candidate re-enters the guard; check 9 asserts the predicate survives |
| 3 | Heal prompt instructs "if the data does not exist, return `WHERE 1=0`" | Turns an unresolvable error into a valid zero-row query narrated as a factual "there were none" | Instruction removed; unresolvable → `HealExhausted`, reported as failure |
| 4 | Write-back stores the post-`_secure_sql` string | Bakes in a tenant id, absolute dates and `LIMIT 500`; the stored example then *teaches* the model to emit `LIMIT 500`, and the guard declines to add its own — the row cap is silently replaced by 500 | Stores the **parameterised, pre-RBAC, pre-LIMIT template** plus the plan |

**Answering a different question, confidently.** Asked *"how many times the name
priya appeared in transactions last month"*, the resolver matched `transactions` →
`txn_count` and `last month` → a window, declared itself confident, silently
discarded "the name priya", and reported a total attempt count **at 0.8
confidence**. There is no person-name field in this data, so the question was
unanswerable — but nothing downstream could tell, because nothing downstream knew
anything had been dropped. This is the worst failure mode the system can have.
Fixed with a coverage check: every content word must be accounted for by a metric,
dimension, filter, time phrase or stopword, and anything left over blocks the
deterministic path. With a model configured such questions go to the planner;
without one the system says which words it could not account for and offers what it
*can* measure. 49 tests now cover it, including a no-false-positive sweep over every
known-good question.

**And then the coverage check went too far the other way.** It answered *"why did
our overall success rate drop last week"* exactly right and refused *"why is
success rate dropping"* with a metric picker — on the word `dropping`. The
stopword list held `drop`, `dropped`, `drops` and `falling`; nobody had typed the
`-ing` form. Same hole for `causing` (we had `cause`, `caused`, `causes`) and
`failures` (we had `failure`). The effect was that only the demo phrasings worked,
which is precisely what a reviewer would suspect of a tool like this, and it was
true for a boring reason.

Enumerating harder does not close a class of defect, so tokens are now tested
against the word lists in their base forms as well as as typed — a crude suffix
peel, no stemmer, and one that can only ever make a word *more* likely to be
considered generic. It cannot invent meaning: `priya` has no base form in any
list, and the regression test for it still passes. Two related narrowings went
with it: a question whose subject is failures and which names no measure resolves
to `failure_rate` (stated as an assumption, and not for a "how many" question,
since this model has no failed-count metric and the unit may not be swapped
silently); and a *why* question no longer asks which period you meant, because the
most recent complete week against the one before it is what that question already
means. `tests/test_phrasing.py` asserts the property rather than a list of
blessed phrasings, with four counter-cases that must still be refused.

**Letting a model write prose about data.** The advisory path exists because
"how do I improve my success rate" has no computable answer, and the obvious
implementation — hand the sentence to the model — fails in a specific, testable
way: it produces *"most estates see 2-3% technical declines"*. That sentence is
fluent, useful-sounding, indistinguishable from a measured figure to the reader,
and completely invented. It is the priya bug in a consultant's jacket.

The split adopted instead is that the model writes the PROSE and supplies none of
the SUBSTANCE. Recommendations come from `semantic/playbooks.yaml`, exactly as
the Action Engine already selects from `semantic/actions.yaml`. Numbers come from
ten probes that run through the same nine-check guard under the same principal,
and reach the model already formatted, numbered `E1 … En`. Then every number in
what comes back is matched against that pack, with tolerance for rounding and an
exemption for small counting integers ("three levers" is English, not a
statistic). One number that is not in the pack discards the whole composition and
the catalogue answer is served.

Discarding everything over one bad number is deliberate and is the only
defensible rule: a reader cannot tell which sentence was the invented one, so a
partially-trustworthy answer costs more to use than a plainer answer that is
trustworthy throughout. `tests/test_advisory.py` tests this with a model that
lies on purpose, because testing it against the real one only proves the real one
behaved today. The eval reports it as a seventh measure, and a run that scores
100% everywhere else and less than 100% there has shipped the worst defect on the
list.

Two scoping decisions went with it. The advisory path sits AFTER the access-scope
pre-check, so an advisory answer cannot become a side channel around row-level
security. And there is no similarity ranking anywhere in it: a playbook selected
because it scored least badly could not say why it won, so no catalogue match
means no answer from this path at all.

A fifth and a sixth, both found in our own implementation by testing rather than by
reading, and both worth recording for the same reason:

**Scope leak when the scoped dimension's table was not joined.** `regional_manager`
is scoped by `region`, which lives on `dim_merchant`. The injector only added the
predicate to SELECTs that referenced that table — so "success rate by payment
method", which joins only `dim_payment_method`, ran completely unscoped and returned
every region. The existing test passed because its SQL happened to join
`dim_merchant`. Fixed by anchoring a **semi-join on the fact table**
(`fact_transactions.merchant_id IN (SELECT merchant_id FROM dim_merchant WHERE
region = ...)`), which applies whether or not the dimension is joined; check 9 was
extended to recognise the `IN (SELECT ...)` form, and five regression tests now use
queries that deliberately do not join the scoping table. A scope that cannot be
enforced now raises rather than running unscoped.

A fifth, found in our own first implementation and worth recording for the same
reason: storing the JSON payload as the Chroma *document* means the embedding is of
the JSON blob, so an identical question scored 0.43 instead of 1.0 and the fast path
could never fire. The document is now the canonical question; the payload rides in
metadata.
