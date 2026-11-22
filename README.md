# PayLens

**An auditable business analyst for payments.** Ask a question in plain English and
get an answer whose every number traces back to a query you can read.

Built for the Pine Labs *Enterprise AI Business Analyst* assignment.

---

## The problem it was built for

Ask most analytics tools why your success rate fell last week and they will mislead
you in one of two ways.

A tool that breaks the rate down by payment rail and sorts ascending announces that
**netbanking is failing**. It isn't. Netbanking has always sat around 71%; it did not
get worse.

A tool that plots each rail's trend announces that **nothing changed**. Also wrong —
the overall number really did fall.

Both are looking at true data and drawing a false conclusion, because a ratio can
move for two completely different reasons. The segments can get worse, or traffic can
shift toward a segment that was always weaker. Here it was the second: UPI's share of
traffic fell from 62% to 55% while netbanking's rose from 8% to 15%, and no rail moved
its own rate by more than a tenth of a percentage point.

So the honest answer is that the success rate fell 1.33 percentage points, 93% of that
is traffic mix rather than degraded processing, nothing in the payment stack broke, and
the team to talk to is whoever changed the routing — not the payments team.

Separating those two causes, and refusing to answer until it can, is what this system
is for.

---

## How it works

A question goes through seven steps. Two of them involve a language model; the rest
exist to make sure the model cannot be confidently wrong.

**1 · Decide what is being asked.** A model reads the question alongside a description
of the data, examples of questions answered before, and a catalogue of the advice the
system is allowed to give. It picks one of six responses: write a query, give advice,
explain a definition, break down why a number moved, ask a clarifying question, or
refuse because the data genuinely cannot answer it.

Those six are separate on purpose. Asked *"what is the formula for success rate"*, an
earlier design answered with last month's value — the identical reply it gave to
*"what is success rate last month"*. A definitional question answered with a number is
wrong even when the number is right.

**2 · Check the query against the rules of the business.** Before anything runs, the
query is checked for the mistakes that are easy to make and hard to notice: reading a
raw table instead of the prepared view, selecting every column, or computing a metric
with the wrong formula. That last one matters most. Success rate deliberately excludes
payments that have not resolved yet, so a query that divides successes by *all*
attempts returns a plausible, readable, different number. It is rejected.

**3 · Run it past the guard.** Nine further checks, all performed on the parsed
structure of the query rather than its text — a system that searches the text for
dangerous words misses a comment placed in the middle of one. The guard confirms the
query only reads, only touches permitted tables and columns, carries the asker's
access restrictions, and cannot run away with the database.

If either check stage rejects a query, the reason goes back to the model and the
rewritten query starts again from the top. The check that rejected it is never relaxed
to let the replacement through.

**4 · Execute, read-only,** with a time limit and a cap on rows returned.

**5 · Measure the result in Python.** The system calculates the totals, shares, changes
and distributions itself and records them as a numbered list of facts. There is no
calculator the model can reach for, because a number produced by a tool call has
nothing to show a reader who asks where it came from.

**6 · Write the answer.** A second model call turns those measured facts into prose. It
is given the facts and nothing else. Afterwards, every figure in what it wrote is
checked against the list — and if a single number appears that was not measured, the
whole composition is discarded and the measured answer is shown instead. A reader
cannot tell which sentence was the invented one, so none of them are kept.

**7 · Remember the question** as an example for next time, but only when the run was
clean. A query that needed two corrections is not a good example to show the next
question.

---

## Why you can trust the answer

**The data the model sees already has the rules baked in.** The underlying transaction
table contains deliberate duplicate records — a double-ingest, the kind of thing that
happens in real pipelines. Rather than hoping the model remembers to remove them, the
system prepares a view where they are already gone, every related table is already
joined, and the official definitions of success, failure and decline type are stored as
ready-made columns. The raw tables are not reachable at all. The model cannot forget a
rule it was never asked to remember.

**Python does the arithmetic, the model chooses what to ask.** This holds for dates too.
Asked for "last week" on a Saturday, the model returned the trailing seven days despite
being told explicitly not to — finding the previous Monday is arithmetic, and models are
unreliable at arithmetic. So the dates are worked out first and handed over as fixed
values.

**Queries are reused, answers never are.** When the same question comes back, the system
reuses the query it wrote before, but runs it again. A stored answer is a number that was
true once, with no way of telling a reader whether it still is.

**It fails in four different ways, and only one of them blames your data.** If everything
worked, you get the answer. If some supporting evidence failed, you get the rest, told how
much is missing. If nothing ran, you are told plainly that this is a failure on the
system's side — not a gap in your data — and offered questions that do work. Only when a
concept is genuinely absent does it say so, and it names the closest thing that does exist.

This distinction came out of a real incident. Two questions came back as *"not answerable
from this data"* when nothing was missing: the model had simply written a malformed cast.
The system told the user something false about their own data and printed a parser error as
the response.

**Advice is checked the same way numbers are.** A sentence like *"you should enable network
tokenisation"* contains no figure, so the number-checking step waves it through — and it is
advice the system has no basis for, aimed at nobody in particular. So every recommendation
is matched against a catalogue of levers, each of which carries an owner and a question that
would verify it. A recommendation with no entry behind it is dropped. If no playbook covers
the question, the system answers it as a measurement rather than inventing counsel.

**It never states an industry benchmark as fact.** Asked whether 86% is good, the honest
answer is that no benchmark is held here, followed by a comparison against this estate's own
best segment.

---

## Who can see what

Four kinds of user: leadership, analysts, regional managers and individual merchants. The
role does not merely change the tone of the answer — it changes which rows the query is
allowed to touch. A merchant's restriction is added to their query automatically, after the
model has written it, from their verified identity rather than anything they typed.

The guard then re-reads the finished query to confirm the restriction actually survived. That
second look is the part that matters: a filter can be added and then neutralised further down,
and a system that only checks it was added would not notice.

If a required piece of identity is missing, the question is refused. An unrestricted query is
never the fallback.

> **One gap is open and known.** The restriction is currently declared against the transaction
> view only. A merchant asking about *settlements* gets no restriction, and the check that
> should catch this passes anyway because it skips tables a query does not mention. Verified:
> a merchant scoped to one account can read settlement rows for all 240. The evaluation harness
> found this and names the table responsible. The fix belongs in the access-control layer rather
> than in configuration, and is not yet made.

---

## The data

Roughly 776,000 payment attempts across four months, spread over six related tables covering
merchants, banks, payment methods, terminals and failure codes. It is generated, deterministic,
and reproducible from a single seed.

It is **calibrated against NPCI's published September 2026 figures** rather than invented —
their technical decline rate, business decline rate, implied approval rate, and the
best and worst performing banks.

Where it deliberately departs, it says so. Technical declines are set higher than the published
national rate, because at the real rate four months of data contains too few of them to break
down or trend, and the most interesting capability would have nothing to demonstrate on. Every
modelled assumption is labelled as such, with its source, in the calibration notes.

**Five faults are planted on purpose**, because a dataset without them cannot show whether the
system is honest:

| Planted fault | What an unprepared tool does with it |
|---|---|
| Duplicate transaction records | counts inflate silently |
| 2% of attempts have no bank recorded | a breakdown by bank quietly fails to sum to the total |
| The last two days are partly loaded | a trend appears to fall off a cliff for no real reason |
| Negative amounts from refund corrections | totals drift without explanation |
| Transactions and settlements disagree for five merchants | two reports show two different numbers |

Five checks run against the exact window each answer used, and anything they find is added to
the answer the reader sees — not filed in a tab. A warning nobody surfaces was not issued.
When two sources disagree, the answer states which one is authoritative and how large the gap
is. It never averages them and never quietly serves the larger.

---

## How well it works

Four hundred questions, spanning plain measurement, statistics, derived arithmetic, advice,
definitions, cause analysis, deliberately ambiguous phrasing, out-of-scope requests, attempted
attacks, access-restricted users and multi-turn conversations.

Each was also put to a **baseline**: two model calls and nothing else, one to write the query
and one to describe the result, with no prepared views, no guard, no checking and no
correction. Same model, same questions. It is not a straw man — it is the shape most
natural-language-to-SQL systems ship in, and the gap between the columns is what the rest of
this architecture buys.

| | PayLens | Baseline |
|---|---|---|
| Questions answered successfully | **95.7%** | 83.1% |
| Agreement with hand-written reference answers | **100%** | — |
| Never reached past the prepared views | **100%** | 0% |
| Used the official metric definition | **100%** | 0% |
| Wrongly claimed the data lacks something | **1.1%** | 30.1% |
| Sent the question to the right kind of answer | **90.5%** | 65.8% |
| Access restriction survived tampering | **100%** | — |
| Typical response time | 14.6s | 3.5s |

A separate language model was then asked to grade the written answers on four criteria. It
scored the two systems within a few points of each other, and the baseline actually won on
completeness.

That is the honest result and it is worth stating plainly: both systems are driven by the same
model, so both write perfectly readable prose. A grader reading the answer cannot tell which
number came from a correct query. The separation shows up in the checks that can be verified
rather than judged — and the response time is what those checks cost.

---

## What it cannot do

- It needs a language model to write queries. Without one it refuses rather than guessing,
  though definitions and cause analysis still work from stored knowledge.
- Conversation history lives in your browser. Clearing site data loses it, and it does not
  follow you to another device.
- Advice is exactly as broad as the catalogue behind it. A question no playbook covers becomes
  a measurement, not improvised counsel.
- The settlements access gap described above is real and still open.
- Answers vary slightly between runs. The method is reproducible — fixed evaluation date, fixed
  question set, caching disabled — but the model is not deterministic, so expect small movement.

---

Setup, commands and the API are in [SETUP.md](SETUP.md). The evaluation method and every metric
definition are in [evaluation/METRICS.md](evaluation/METRICS.md).
