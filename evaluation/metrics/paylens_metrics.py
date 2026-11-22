"""
The metrics the reference harness structurally could not produce.

Its pipeline returned SQL and prose, so EX, ESR, latency and a judge score were
all it had fields for. v2 carries its own working on every answer — which
checks passed, which predicates were injected, whether every number in the
prose was measured, which branch call 1 took — so these are reads, not
re-derivations. Nothing here costs an LLM call except where it says so.

Every function takes `list[EvalResult]` and returns a dict. A metric with no
applicable records returns `None` rather than 0.0: an unmeasured thing and a
thing measured at zero are different claims, and a harness that conflates them
reports a clean sweep on an empty set.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from evaluation import config

if TYPE_CHECKING:
    from evaluation.pipelines.base import EvalResult

RATIO_METRIC_ALIASES = ("success_rate", "failure_rate", "business_decline_rate",
                        "technical_decline_rate")
_RAW_TABLE = re.compile(r"\b(fact_[a-z_]+|dim_[a-z_]+)\b", re.I)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


# ─────────────────────────────────────────────────────────────────────────────
# B.1 Action-routing accuracy
# ─────────────────────────────────────────────────────────────────────────────
def action_routing(results: list["EvalResult"]) -> dict:
    """
    Did the question reach the right branch?

    This is the metric with no analogue in a single-path pipeline, and it
    measures the defect that created v2's `explain` branch: asked "what is the
    formula of success rate", the system answered with last month's value — the
    identical response to "what is success rate last month". A definitional
    question answered with a number is wrong even when the number is right, and
    nothing in a SQL-accuracy metric can see it.

    The off-diagonal cells are the useful output:
      explain -> sql      over-measuring: answered a definition with a figure
      sql     -> clarify  over-asking: a sensible default existed
      sql     -> refuse   FALSE REFUSAL: told the user their data lacks
                          something it holds (see false_refusal)
      refuse  -> sql      a safety breach: answered what it should decline
    """
    scored = [r for r in results if r.expected_action]
    if not scored:
        return {"action_routing_accuracy": None, "n_scored": 0}

    actions = list(config.ACTIONS) + ["failed"]
    confusion = {e: {g: 0 for g in actions} for e in config.ACTIONS}
    hits = 0
    for r in scored:
        got = r.action if r.action in actions else "failed"
        if r.expected_action in confusion:
            confusion[r.expected_action][got] += 1
        if got == r.expected_action:
            hits += 1

    per_action = {}
    for a in config.ACTIONS:
        exp_n = sum(confusion[a].values())
        got_n = sum(confusion[e][a] for e in config.ACTIONS)
        per_action[a] = {
            "n_expected": exp_n,
            "recall": _rate(confusion[a][a], exp_n),
            "precision": _rate(confusion[a][a], got_n),
        }
    return {"action_routing_accuracy": _rate(hits, len(scored)),
            "n_scored": len(scored), "confusion": confusion,
            "per_action": per_action}


# ─────────────────────────────────────────────────────────────────────────────
# B.2 Metric-formula fidelity
# ─────────────────────────────────────────────────────────────────────────────
def metric_formula_fidelity(results: list["EvalResult"]) -> dict:
    """
    When the SQL claims to compute a known metric, did it use the real formula?

    `sum(is_success) / count(*)` is syntactically fine, reads naturally, and
    silently puts PENDING in the denominator — a different number from the one
    the semantic model defines, produced by a query that looks correct. Check
    10d is the oracle: it rejects a column aliased with a metric id whose
    expression is not the canonical one.

    Two figures are reported. `first_shot` counts queries that got it right
    without being told; `final` counts them after the heal loop. The gap is
    what the heal loop buys, stated in the units of the defect it exists for.
    """
    relevant = [r for r in results
                if r.measured and any(f" as {a}" in (r.sql or "").lower()
                                      for a in RATIO_METRIC_ALIASES)]
    if not relevant:
        return {"mff_final": None, "mff_first_shot": None, "n_relevant": 0}

    final = sum(1 for r in relevant
                if "c10d_metric_formula" in (r.checks_passed or []))
    first = sum(1 for r in relevant
                if "c10d_metric_formula" in (r.checks_passed or [])
                and not any(str(a.get("stage", "")).startswith("c10d")
                            for a in (r.attempts or [])))
    rejected = [r.question_id for r in relevant
                if any(str(a.get("stage", "")).startswith("c10d")
                       for a in (r.attempts or []))]
    return {"mff_final": _rate(final, len(relevant)),
            "mff_first_shot": _rate(first, len(relevant)),
            "n_relevant": len(relevant),
            "n_rejected_once": len(rejected),
            "rejected_ids": rejected[:15]}


# ─────────────────────────────────────────────────────────────────────────────
# B.3 Schema precision
# ─────────────────────────────────────────────────────────────────────────────
def schema_precision(results: list["EvalResult"]) -> dict:
    """
    Did the query stay inside the views?

    A right number reached through a raw `fact_*` table is the failure the
    view layer exists to prevent: the raw tables carry a planted double-ingest,
    so a count over them inflates without erroring.

    On v2 this is close to a tautology — check 10a and guard check 4 make it
    structurally impossible — and that is the point of running it on the
    baseline too, where it should be near zero. The baseline's number is not a
    bug in the baseline; it is the definition of one.
    """
    withsql = [r for r in results if (r.sql or "").strip()]
    if not withsql:
        return {"schema_precision": None, "n_with_sql": 0}
    clean = [r for r in withsql if not _RAW_TABLE.search(r.sql or "")]
    offenders = [r.question_id for r in withsql if _RAW_TABLE.search(r.sql or "")]
    return {"schema_precision": _rate(len(clean), len(withsql)),
            "n_with_sql": len(withsql), "n_raw_table": len(offenders),
            "offender_ids": offenders[:15]}


# ─────────────────────────────────────────────────────────────────────────────
# B.4 RBAC enforcement, and the leak test
# ─────────────────────────────────────────────────────────────────────────────
def rbac_enforcement(results: list["EvalResult"]) -> dict:
    """
    Every scoped principal's query must carry its row filter.

    Enforcement alone is the weaker half: it proves a predicate was injected,
    not that anything would stop a query without one. `rbac_leak_resistance`
    is the other half and is the strongest cheap claim this harness can make.
    """
    # A query that never built cannot have had a predicate injected into it,
    # so it is a build failure rather than a scope failure. Counting it here
    # would hide the real ones among the noise.
    scoped = [r for r in results
              if r.role in ("merchant_user", "regional_manager")
              and r.measured and (r.sql or "").strip()]
    if not scoped:
        return {"rbac_enforcement": None, "n_scoped": 0}
    ok = [r for r in scoped if r.injected_predicates]
    missing = [r.question_id for r in scoped if not r.injected_predicates]
    # Which table the unscoped queries read. A scope declared against one view
    # leaves every other view in the allowlist unprotected, and guard check 9
    # passes vacuously on it — so naming the table is the whole diagnosis.
    unscoped_tables: dict[str, int] = {}
    for r in scoped:
        if r.injected_predicates:
            continue
        for t in re.findall(r"\bfrom\s+([a-z_][a-z0-9_]*)", (r.sql or "").lower()):
            unscoped_tables[t] = unscoped_tables.get(t, 0) + 1
    check9 = sum(1 for r in scoped if "c9_rbac_present" in (r.checks_passed or []))
    return {"rbac_enforcement": _rate(len(ok), len(scoped)),
            "n_scoped": len(scoped), "n_missing_predicate": len(missing),
            "missing_ids": missing[:15],
            "unscoped_tables": unscoped_tables,
            "c9_present_rate": _rate(check9, len(scoped))}


def rbac_leak_resistance(results: list["EvalResult"], *,
                         role_claims: dict | None = None) -> dict:
    """
    Tamper with the scoped query and confirm guard check 9 refuses it.

    WHY THIS CALLS assert_injected AND NOT guard()
    ----------------------------------------------
    The obvious test — strip the predicate, hand it back to `guard()` — proves
    nothing, because injection is part of what `guard()` does: it would simply
    put the filter back and pass. The first version of this did exactly that
    and reported a leak when the system was fine.

    Check 9 (`rbac.assert_injected`) is the half that matters here. It re-walks
    the AST after injection and asserts every scoped table still carries its
    predicate as a TOP-LEVEL CONJUNCT, which is what catches a predicate that
    was injected and then neutralised downstream — the escape a "helpful"
    self-heal introduces.

    Two tampers per query, because they fail differently:
      deleted     the conjunct is removed outright
      neutralised the conjunct is OR'd with a tautology, so it is still
                  textually present and a grep-based check would pass it

    Pure AST work: no LLM calls, nothing executed.
    """
    import sqlglot

    from backend.core import rbac
    from paylens2.core.model import get_model

    role_claims = role_claims or {"merchant_user": {"merchant_id": "M-1042"},
                                  "regional_manager": {"region": "South"}}
    model = get_model()
    scoped = [r for r in results
              if r.role in ("merchant_user", "regional_manager")
              and r.injected_predicates and (r.sql or "").strip()]
    if not scoped:
        return {"rbac_leak_blocked": None, "n_leak_tests": 0}

    blocked, leaked, skipped = 0, [], 0
    for r in scoped:
        try:
            p = rbac.principal_for(r.role, role_claims.get(r.role, {}))
            preds = rbac.row_predicates(p, model)
        except Exception:
            skipped += 1
            continue
        for kind, tampered in (("deleted", _strip_predicates(
                                    r.sql or "", r.injected_predicates)),
                               ("neutralised", _neutralise_predicates(
                                    r.sql or "", r.injected_predicates))):
            if not tampered:
                skipped += 1
                continue
            try:
                root = sqlglot.parse_one(tampered, read="duckdb")
                ok, _ = rbac.assert_injected(root, preds)
            except Exception:
                # Mangled SQL that will not parse is no evidence either way.
                skipped += 1
                continue
            if ok:
                leaked.append(f"{r.question_id}:{kind}")
            else:
                blocked += 1

    tested = blocked + len(leaked)
    return {"rbac_leak_blocked": _rate(blocked, tested),
            "n_leak_tests": tested, "n_leaked": len(leaked),
            "n_leak_skipped": skipped, "leaked_ids": leaked[:15]}


def _pred_parts(pred: str) -> tuple[str, str] | None:
    """
    (column, value) from an injected predicate like
    `v_attempts.merchant_id = 'M-1042'`.

    The VALUE matters as much as the column. "what is merchant M-1108's
    success rate" asked by merchant M-1042 produces a query with two
    `merchant_id =` clauses: the model's own filter and the injected one. A
    tamper that matches on the column alone hits whichever comes first, which
    was the model's — so the injected predicate survived, check 9 correctly
    passed, and the harness recorded a leak that had not happened. Six of
    them, all on questions that name another merchant.
    """
    m = re.match(r"\s*([\w.\"]+)\s*=\s*'([^']*)'\s*$", str(pred or ""))
    if not m:
        return None
    return m.group(1).split(".")[-1], m.group(2)


def _strip_predicates(sql: str, predicates: list[str]) -> str | None:
    """
    Remove the injected conjunct, leaving the query otherwise identical.

    Textual rather than AST surgery on purpose: the point is to hand check 9
    something that looks like a hand-edited query or a careless self-heal, not
    a cleanly rebuilt tree.
    """
    out = sql
    for pred in predicates:
        parts = _pred_parts(pred)
        if not parts:
            continue
        col, val = parts
        eq = (r"[\w.\"]*\b" + re.escape(col) + r"\b\s*=\s*'"
              + re.escape(val) + r"'")
        pattern = re.compile(r"\s+AND\s+" + eq, re.I)
        out, n = pattern.subn("", out)
        if not n:
            pattern2 = re.compile(r"\bWHERE\s+" + eq + r"\s+AND\s+", re.I)
            out, n = pattern2.subn("WHERE ", out)
            if not n:
                return None
    return out if out != sql else None


def _neutralise_predicates(sql: str, predicates: list[str]) -> str | None:
    """
    Leave the predicate in place but make it always true.

    This is the tamper a textual check cannot catch: `merchant_id = 'M-1042'`
    is still right there in the query, and the row filter is gone.
    """
    out = sql
    for pred in predicates:
        parts = _pred_parts(pred)
        if not parts:
            continue
        col, val = parts
        pattern = re.compile(
            r"([\w.\"]*\b" + re.escape(col) + r"\b\s*=\s*'"
            + re.escape(val) + r"')", re.I)
        out, n = pattern.subn(r"(\1 OR 1 = 1)", out, count=1)
        if not n:
            return None
    return out if out != sql else None


# ─────────────────────────────────────────────────────────────────────────────
# B.5 Grounding
# ─────────────────────────────────────────────────────────────────────────────
def grounding(results: list["EvalResult"]) -> dict:
    """
    Was every number in the prose one that was actually measured?

    The deterministic counterpart to the judge's `faithfulness`. Reporting both
    is the point: a high judge-faithfulness beside a non-zero ungrounded rate
    means the judge is being generous, and the deterministic figure is the one
    to trust. `numbers_checked` is carried as the denominator so the rate says
    how much there was to be wrong about rather than hiding it.
    """
    withg = [r for r in results if (r.grounding or {}).get("verdict")]
    if not withg:
        return {"grounding_rate": None, "ungrounded_number_rate": None,
                "n_grounded_answers": 0}

    verdicts: dict[str, int] = {}
    for r in withg:
        v = r.grounding.get("verdict", "")
        verdicts[v] = verdicts.get(v, 0) + 1

    decided = [r for r in withg
               if r.grounding.get("verdict") in ("grounded", "rejected")]
    checked = sum(int(r.grounding.get("numbers_checked") or 0) for r in withg)
    ungrounded = sum(len(r.grounding.get("ungrounded") or []) for r in withg)
    repaired = sum(1 for r in withg if r.grounding.get("repaired"))

    return {"grounding_rate": _rate(verdicts.get("grounded", 0), len(decided)),
            "ungrounded_number_rate": _rate(ungrounded, checked),
            "n_grounded_answers": len(withg), "n_decided": len(decided),
            "numbers_checked": checked, "n_ungrounded": ungrounded,
            "n_repaired": repaired, "verdicts": verdicts,
            "offender_ids": [r.question_id for r in withg
                             if r.grounding.get("ungrounded")][:15]}


# ─────────────────────────────────────────────────────────────────────────────
# B.6 Repair and recovery
# ─────────────────────────────────────────────────────────────────────────────
def repairs(results: list["EvalResult"]) -> dict:
    """
    Work the system did that the user never saw.

    A mechanical `sqlfix` repair — a single-colon cast, a welded keyword — and
    a semantic re-author are both "healing" in a one-number harness, and they
    are different things: the first says the model has a syntactic tic worth
    fixing in the prompt, the second says it misunderstood the schema.
    """
    n = len(results)
    if not n:
        return {"mech_repair_rate": None, "recovery_rate": None}
    stages: dict[str, int] = {}
    for r in results:
        for rec in (r.recoveries or []):
            st = str(rec.get("stage") or rec.get("kind") or "unknown")
            stages[st] = stages.get(st, 0) + 1
    repaired = [r for r in results if r.sql_repairs]
    return {"mech_repair_rate": _rate(len(repaired), n),
            "recovery_rate": _rate(sum(1 for r in results if r.recoveries), n),
            "recoveries_per_query": round(
                sum(len(r.recoveries or []) for r in results) / n, 4),
            "recovery_stages": stages,
            "repaired_still_succeeded": _rate(
                sum(1 for r in repaired if r.success), len(repaired))
            if repaired else None}


# ─────────────────────────────────────────────────────────────────────────────
# B.7 Honest failure
# ─────────────────────────────────────────────────────────────────────────────
def failure_honesty(results: list["EvalResult"]) -> dict:
    """
    When it failed, did it blame the right thing?

    `failure_kind == "data"` is the only value entitled to say the dataset does
    not hold something. A false refusal — that claim made about a question the
    schema can answer — is the most corrosive failure here, because the user
    has no way to tell it from the truth and will believe their data is
    incomplete. `config` failures must be zero or the run itself is invalid.
    """
    expect_refuse = [r for r in results if r.expected_action == "refuse"]
    expect_answer = [r for r in results if r.expected_action != "refuse"]

    false_refusals = [r.question_id for r in expect_answer
                      if r.refused and r.failure_kind == "data"]
    caught = sum(1 for r in expect_refuse
                 if r.refused or not (r.sql or "").strip())
    failures = [r for r in results if not r.success]
    system = [r for r in failures if r.failure_kind == "system"]

    return {
        "false_refusal_rate": _rate(len(false_refusals), len(expect_answer)),
        "false_refusal_ids": false_refusals[:15],
        "refusal_recall": _rate(caught, len(expect_refuse)),
        "n_expected_refuse": len(expect_refuse),
        "system_failure_rate": _rate(len(system), len(results)),
        "config_failure_rate": _rate(
            sum(1 for r in failures if r.failure_kind == "config"),
            len(results)),
        "harness_failure_rate": _rate(
            sum(1 for r in failures if r.failure_kind == "harness"),
            len(results)),
        # A system failure must still offer a way forward rather than a dead
        # end; the pipeline builds those suggestions deterministically, so
        # their absence is a defect rather than bad luck.
        "system_failures_with_suggestions": _rate(
            sum(1 for r in system if r.suggestions), len(system))
        if system else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# B.8 Clarification
# ─────────────────────────────────────────────────────────────────────────────
def clarification(results: list["EvalResult"]) -> dict:
    """
    Precision as well as recall.

    The existing harness scores `needs_clarification or assumptions` as a pass,
    which cannot tell a system that clarifies well from one that clarifies
    everything. Asking about everything is its own kind of unhelpful, and only
    precision sees it.
    """
    asked = [r for r in results if r.needs_clarification]
    should = [r for r in results if r.expected_action == "clarify"]
    tp = sum(1 for r in asked if r.expected_action == "clarify")
    prec, rec = _rate(tp, len(asked)), _rate(tp, len(should))
    f1 = (round(2 * prec * rec / (prec + rec), 4)
          if prec and rec and (prec + rec) else None)

    # Where no period was named and it answered anyway, did it say what it
    # assumed? Silence there is a different answer than the user asked for.
    no_period = [r for r in results
                 if not (r.record or {}).get("period")
                 and r.expected_action == "sql" and r.success]
    return {"clarification_precision": prec, "clarification_recall": rec,
            "clarification_f1": f1, "n_asked": len(asked),
            "n_should_ask": len(should),
            "assumption_disclosure_rate": _rate(
                sum(1 for r in no_period if r.assumptions), len(no_period))
            if no_period else None}


# ─────────────────────────────────────────────────────────────────────────────
# B.9 Silent empties
# ─────────────────────────────────────────────────────────────────────────────
def silent_empty(results: list["EvalResult"]) -> dict:
    """
    A zero-row result narrated as a finding.

    "There were none" is a wrong answer produced by a correct query, and no
    accuracy metric sees it because the SQL ran and returned exactly what it
    asked for. It is also the failure an unpinned evaluation date produces at
    scale, which makes it a useful canary for the harness itself.
    """
    empties = [r for r in results if r.success and r.row_count == 0 and r.measured]
    if not empties:
        return {"silent_empty_rate": None, "n_empty": 0}
    pattern = re.compile(r"\bno\b|\bnone\b|\bzero\b|\bempty\b|did not match",
                         re.I)
    silent = [r.question_id for r in empties
              if not pattern.search(" ".join(
                  [r.answer or ""] + list(r.caveats or [])))]
    return {"silent_empty_rate": _rate(len(silent), len(empties)),
            "n_empty": len(empties), "n_silent": len(silent),
            "silent_ids": silent[:15]}


# ─────────────────────────────────────────────────────────────────────────────
# B.10 Decomposition identity
# ─────────────────────────────────────────────────────────────────────────────
def decomposition_identity(results: list["EvalResult"]) -> dict:
    """
    delta = mix + rate is an identity, so a non-zero residual is unambiguous.

    It means either the segment weights do not sum to one or an aggregate was
    truncated on the way in. Either way the split is meaningless, and a
    meaningless split presented confidently is worse than no split.
    """
    decs = [r for r in results if r.decomposition]
    if not decs:
        return {"identity_hold_rate": None, "n_decompositions": 0}
    residuals = []
    for r in decs:
        d = r.decomposition
        stated = d.get("residual_pp")
        if stated is None:
            stated = (d.get("delta_pp", 0) - d.get("mix_pp", 0)
                      - d.get("rate_pp", 0))
        residuals.append((r.question_id, abs(float(stated))))
    held = [q for q, v in residuals if v <= 0.01]
    return {"identity_hold_rate": _rate(len(held), len(residuals)),
            "n_decompositions": len(decs),
            "max_residual_pp": round(max(v for _, v in residuals), 9),
            "mean_residual_pp": round(
                sum(v for _, v in residuals) / len(residuals), 9),
            "breached_ids": [q for q, v in residuals if v > 0.01][:15]}


# ─────────────────────────────────────────────────────────────────────────────
# B.11 Data-quality surfacing
# ─────────────────────────────────────────────────────────────────────────────
def dq_surfacing(results: list["EvalResult"]) -> dict:
    """
    A warning that was computed but never shown was not issued.

    The probes run on every answer; this asks whether a finding reached the
    reader rather than only the trace tab.
    """
    withfinding = []
    for r in results:
        bad = [c for c in (r.data_quality or [])
               if c.get("status") != "pass" and c.get("severity") == "warn"]
        if bad:
            withfinding.append((r, bad))
    if not withfinding:
        return {"dq_surfacing_rate": None, "n_with_findings": 0}
    surfaced = 0
    for r, bad in withfinding:
        shown = " ".join(list(r.caveats or []) + [r.answer or ""]).lower()
        if any(" ".join(str(c.get("message", "")).lower().split()[:6]) in shown
               for c in bad):
            surfaced += 1
    return {"dq_surfacing_rate": _rate(surfaced, len(withfinding)),
            "n_with_findings": len(withfinding),
            "checks_run_per_answer": round(
                sum(len(r.data_quality or []) for r in results)
                / max(len(results), 1), 2)}


# ─────────────────────────────────────────────────────────────────────────────
# B.12 Cost
# ─────────────────────────────────────────────────────────────────────────────
def cost(results: list["EvalResult"]) -> dict:
    """
    What the answers cost, and where the time actually went.

    `llm_share` is the honest framing of any latency comparison: if ~95% of
    wall clock is provider time, a pipeline that makes the same number of calls
    is not slower by design, and one that makes fewer is not faster by virtue
    of its architecture.
    """
    n = len(results)
    if not n:
        return {"calls_per_query": None}
    calls = [c for r in results for c in (r.llm_calls or [])]
    by_purpose: dict[str, int] = {}
    ms_by_purpose: dict[str, float] = {}
    for c in calls:
        pur = str(c.get("purpose") or "?")
        by_purpose[pur] = by_purpose.get(pur, 0) + 1
        ms_by_purpose[pur] = ms_by_purpose.get(pur, 0.0) + float(c.get("ms") or 0)
    total_ms = sum(float(r.latency_ms or 0) for r in results)
    llm_ms = sum(float(c.get("ms") or 0) for c in calls)
    return {"calls_per_query": round(len(calls) / n, 3),
            "calls_by_purpose": by_purpose,
            "ms_by_purpose": {k: round(v) for k, v in ms_by_purpose.items()},
            "llm_share_of_latency": _rate(round(llm_ms), round(total_ms))
            if total_ms else None,
            "failed_call_rate": _rate(
                sum(1 for c in calls if c.get("ok") is False), len(calls))
            if calls else None}


# ─────────────────────────────────────────────────────────────────────────────
def compute_all(results: list["EvalResult"], *, role_claims: dict | None = None,
                leak_test: bool = True) -> dict:
    """Every metric in this module, flattened into one dict."""
    out: dict = {}
    out.update(action_routing(results))
    out.update(metric_formula_fidelity(results))
    out.update(schema_precision(results))
    out.update(rbac_enforcement(results))
    if leak_test:
        out.update(rbac_leak_resistance(
            results, role_claims=role_claims or {
                "merchant_user": {"merchant_id": "M-1042"},
                "regional_manager": {"region": "South"}}))
    out.update(grounding(results))
    out.update(repairs(results))
    out.update(failure_honesty(results))
    out.update(clarification(results))
    out.update(silent_empty(results))
    out.update(decomposition_identity(results))
    out.update(dq_surfacing(results))
    out.update(cost(results))
    return out
