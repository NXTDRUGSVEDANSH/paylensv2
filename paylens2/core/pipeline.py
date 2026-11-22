"""
The v2 pipeline.

    question
      1  author        schema + few-shots + catalogue -> sql | clarify | refuse
      2  check 10      views only, no SELECT *, metric formulas verbatim
      3  GUARD         the existing nine checks, unmodified, on the v2 model
      4  execute       + row cap: aggregates BLOCK over the cap, detail truncates
      5  profile       deterministic stats -> the numbered fact pack
      6  synthesise    prose from the pack, then verify, then one repair
      7  write back    as an unverified few-shot

Steps 2 and 3 are the heal loop: a rejection goes back to the model with the
reason attached, and the repaired SQL re-enters BOTH from the top. The check that
rejected a query is never relaxed to let its replacement through.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

from backend.core import events, rbac
from backend.core.executor import QueryTimeout, execute
from backend.core.guard import GuardBlocked, guard
from backend.llm.provider import CallBudget, LLMUnavailable
from paylens2 import config
from paylens2.core import author as author_mod
from paylens2.core import (catalogue, chart as chartmod, checks, dq as dqmod,
                           drivers, knowledge, model as v2model, periods,
                           retrieval, sqlcache, sqlfix, synth)
from paylens2.core.profile import Profile, combine, profile

log = logging.getLogger("paylens2.pipeline")


@dataclass
class Attempt:
    sql: str
    stage: str                  # check10 | guard | execute
    ok: bool
    reason: str = ""
    hint: str = ""


@dataclass
class V2Answer:
    ok: bool = True
    question: str = ""
    role: str = "cxo"
    engine: str = "v2"

    # the answer
    headline: str = ""
    observed: list[str] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    # the evidence
    sql_authored: str = ""
    sql_executed: str = ""
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    facts: list[dict] = field(default_factory=list)
    profile_text: str = ""
    chart: dict = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    playbooks: list[str] = field(default_factory=list)

    # the other two outcomes
    needs_clarification: bool = False
    clarification: dict = field(default_factory=dict)
    refused: bool = False
    refusal_reason: str = ""
    # "data"   — the concept genuinely is not in this schema (an honest refusal)
    # "system" — WE failed: a query would not parse, build or run
    # "config" — the deployment is wrong (views missing, no model configured)
    # The distinction is the point. A parse error reported as "the field is not
    # here" tells the user something false about their own data.
    failure_kind: str = ""
    failure_detail: str = ""          # raw check + reason; Trace tab only
    suggestions: list[str] = field(default_factory=list)
    sql_repairs: list[str] = field(default_factory=list)

    # advisory
    advice: dict = field(default_factory=dict)
    evidence_queries: list[dict] = field(default_factory=list)
    # definitional — "what IS this", as opposed to "what is it right now"
    explanation: dict = field(default_factory=dict)

    # provenance
    composed_by: str = "template"
    grounding: dict = field(default_factory=dict)
    checks_passed: list[str] = field(default_factory=list)
    injected_predicates: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    # "" | "l0" | "l1" — which cache layer supplied the query, if any. The
    # authoring call is the only thing a hit skips; everything downstream ran.
    cache_hit: str = ""
    cache_state: str = ""            # pending | promoted | off
    # Every data-quality check run against this answer's own window, passed and
    # failed. A number with a problem nobody mentioned is worse than no number.
    data_quality: list[dict] = field(default_factory=list)
    # Mix-vs-rate split: did traffic move, or did the segments get worse?
    decomposition: dict = field(default_factory=dict)
    # Things that went wrong and were fixed without failing the request: a
    # repaired JSON response, a truncated one recovered, a provider retry. These
    # used to exist only as log lines on the server's stdout, which meant the
    # most interesting thing the pipeline does was also the least visible.
    recoveries: list[dict] = field(default_factory=list)
    timings_ms: dict = field(default_factory=dict)
    total_ms: int = 0


def _suggestions(question: str, model) -> list[str]:
    """
    Concrete questions this system can answer, for when the one asked failed.

    Rung 3 of the ladder. Built deterministically from the semantic model by
    matching the user's own words against metric and dimension synonyms — not
    generated by a model, because a suggested question that then fails is worse
    than no suggestion at all. Each one is a complete question that re-enters
    the pipeline unchanged.
    """
    try:
        metrics, dims = model.match_all(question or "")
    except Exception:  # noqa: BLE001
        metrics, dims = [], []

    def label(mid: str) -> str:
        return re.sub(r"\s*\([^)]*\)", "", model.metric(mid).label).strip().lower()

    mids = [m for m in metrics if m in model.metrics][:2] or ["success_rate", "txn_count"]
    dids = [d for d in dims if d in model.dimensions][:2] or \
        ["payment_method", "issuer_bank"]

    out: list[str] = []
    for mid in mids[:2]:
        out.append(f"{label(mid)} last week")
        for did in dids[:1]:
            out.append(f"{label(mid)} by "
                       f"{model.dimension(did).label.lower()} last week")
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:3]


def _formats() -> dict[str, str]:
    """Result column -> declared format, so the pack speaks percent and rupees."""
    return {mid: m.format for mid, m in v2model.get_model().metrics.items()}


def _metric_columns(sql: str) -> list[str]:
    """Which result columns are the answer rather than context."""
    ids = set(v2model.get_model().metrics)
    low = (sql or "").lower()
    return [m for m in ids if f" as {m}" in low or f"as {m}\n" in low]


def _run_one(sql: str, p, model, ans: V2Answer) -> tuple[list[dict], str, str]:
    """
    (rows, guarded_sql, error). One evidence query, through the same gates.

    An error is returned rather than raised: an advisory answer built on three
    of four queries is still worth having, and saying which one could not be
    measured is more useful than failing the whole briefing.
    """
    sql, repairs = sqlfix.repair(sql)
    if repairs:
        ans.sql_repairs.extend(repairs)
    try:
        checks.check_v2(sql)
        g = guard(sql, p, model)
    except (checks.CheckFailed, GuardBlocked, PermissionError) as exc:
        stage = getattr(exc, "check", "guard")
        return [], "", f"[{stage}] {sqlfix.strip_ansi(getattr(exc, 'reason', str(exc)))}"
    try:
        result = execute(g.sql, timeout_s=config.SQL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        return [], g.sql, f"{type(exc).__name__}: {sqlfix.strip_ansi(str(exc))}"
    ans.checks_passed = sorted(set(ans.checks_passed) | set(g.checks_passed))
    ans.injected_predicates = sorted(set(ans.injected_predicates)
                                     | set(g.injected_predicates))
    return result.rows[:config.MAX_SQL_ROWS], g.sql, ""


def _advise(ans: V2Answer, a, question: str, p, model, persona: str,
            budget, finish, fail) -> V2Answer:
    """The advisory path: several evidence queries, one briefing."""
    levers = catalogue.levers(a.playbook_ids)
    framing = catalogue.framing(a.playbook_ids)
    limits = catalogue.limits(a.playbook_ids)

    parts: list[tuple[str, Profile]] = []
    events.emit("stage", "evidence", status="start",
                message=f"{len(a.evidence_queries)} evidence queries to run")
    for q in a.evidence_queries:
        rows, guarded_sql, err = _run_one(q["sql"], p, model, ans)
        events.emit("stage", "evidence",
                    status="fail" if err else "done",
                    message=f"{q['id']}: {q.get('purpose', '')}"
                            + ("" if err else f" — {len(rows):,} rows"),
                    detail=err)
        rec = {"id": q["id"], "purpose": q["purpose"], "sql": q["sql"],
               "sql_executed": guarded_sql, "rows": len(rows), "error": err,
               "data": rows[:300], "chart": {}}
        ans.evidence_queries.append(rec)
        ans.attempts.append({"sql": q["sql"], "stage": q["id"],
                             "ok": not err, "reason": err})
        if err or not rows:
            continue
        rec["chart"] = chartmod.validate(None, rows, _metric_columns(q["sql"]))
        parts.append((q["purpose"],
                      profile(rows, metric_columns=_metric_columns(q["sql"]),
                              formats=_formats())))

    if not parts:
        # Rung 3: the advice exists but nothing could be measured to ground it.
        failed = [r["error"] for r in ans.evidence_queries if r["error"]]
        return fail("I know what to look at for that question, but none of the "
                    "queries I wrote would run, so there is nothing measured to "
                    "base the advice on.",
                    detail=" | ".join(failed)[:400])

    prof = combine(parts)
    ans.profile_text = prof.text
    ans.facts = [{"id": f.id, "label": f.label, "value": f.value, "detail": f.detail}
                 for f in prof.facts]
    ans.rows = parts[0][1].sample
    ans.row_count = sum(len(pr.sample) for _, pr in parts)

    events.emit("stage", "synthesise", status="start",
                message=f"asking {config.SYNTH_MODEL} for the briefing")
    out = synth.advise(question, a.evidence_queries, prof, persona=persona,
                       levers=levers, framing=framing, limits=limits,
                       assumptions=a.assumptions, budget=budget)

    events.emit("stage", "synthesise", status="done",
                message=f"grounding: {out.grounding.verdict}")
    if out.grounding.invented_levers:
        events.recovery("synthesise",
                        f"{len(out.grounding.invented_levers)} recommendation(s) "
                        f"had no matching playbook lever and were dropped")
    ans.advice = {"situation": out.situation, "whats_happening": out.whats_happening,
                  "what_to_do": out.what_to_do, "cannot_tell": out.cannot_tell}
    ans.headline = out.situation
    ans.observed = out.whats_happening
    ans.caveats = list(out.cannot_tell)
    ans.composed_by = out.composed_by
    ans.playbooks = a.playbook_ids
    ans.assumptions = a.assumptions
    ans.grounding = {"verdict": out.grounding.verdict, "note": out.grounding.note,
                     "numbers_checked": out.grounding.numbers_checked,
                     "ungrounded": out.grounding.ungrounded,
                     "wrong_superlatives": out.grounding.wrong_superlatives,
                     "levers_checked": out.grounding.levers_checked,
                     "invented_levers": out.grounding.invented_levers,
                     "repaired": out.grounding.repaired}
    failed = [r for r in ans.evidence_queries if r["error"]]
    if failed:
        # Rung 2, stated rather than hidden.
        ans.caveats.append(
            f"{len(failed)} of {len(ans.evidence_queries)} supporting queries could "
            f"not be measured, so this briefing rests on the rest.")
    return finish(ans)


def _explain(ans: V2Answer, a, question: str, persona: str, budget, finish,
             timings: dict) -> V2Answer:
    """
    A definitional question: what something IS, not what it is right now.

    This branch exists because its absence was a real defect. Call 1 could
    return only sql, advise, clarify or refuse, so "what is the formula of
    success rate" had nowhere to go and came back as a measurement of last
    month — the identical answer to "what is success rate for last month",
    which is how it was noticed.

    No SQL runs here and no number is produced. The guard is not involved
    because nothing is executed; what replaces it is `verify_formula`, which
    asserts that a formula stated in prose is the one check 10d enforces in
    generated SQL. Those two have to agree or the system contradicts itself
    depending on which way you ask.
    """
    t = time.time()
    query = " ".join([question] + list(a.terms or []))
    chunks = knowledge.search(query)
    timings["retrieve"] = int((time.time() - t) * 1000)
    events.emit("stage", "retrieve", status="done", ms=timings["retrieve"],
                message=f"{len(chunks)} definitions retrieved"
                        + (f", {sum(1 for c in chunks if c['authoritative'])} "
                           f"authoritative" if chunks else ""))

    if not chunks:
        # Rung 4, and the one place this path may say something is absent: the
        # corpus is generated from the semantic model, so a term missing from
        # it really is a term this system does not define.
        ans.ok, ans.refused = False, True
        ans.failure_kind = "data"
        ans.refusal_reason = (
            "I have no definition for that. The glossary is generated from this "
            "system's own semantic model, so a term missing from it is one this "
            "system does not measure.")
        ans.suggestions = _suggestions(question, v2model.get_model())
        return finish(ans)

    t = time.time()
    events.emit("stage", "explain", status="start",
                message=f"asking {config.SYNTH_MODEL} to write the definition")
    e = synth.explain(question, chunks, persona=persona, budget=budget)
    timings["explain"] = int((time.time() - t) * 1000)
    events.emit("stage", "explain", status="done", ms=timings["explain"],
                message=f"formula check: {e.grounding.verdict}")

    ans.explanation = {
        "definition": e.definition, "formula": e.formula,
        "formula_plain": e.formula_plain, "notes": e.notes,
        "industry": e.industry, "sources": e.sources, "terms": e.terms,
        "retrieved": [{"tier": c["tier"], "term": c["term"], "ref": c["ref"],
                       "similarity": c["similarity"],
                       "authoritative": c["authoritative"]} for c in chunks],
    }
    ans.headline = e.definition
    ans.composed_by = e.composed_by
    ans.grounding = {"verdict": e.grounding.verdict, "note": e.grounding.note,
                     "numbers_checked": e.grounding.numbers_checked,
                     "ungrounded": e.grounding.ungrounded,
                     "wrong_superlatives": e.grounding.wrong_superlatives,
                     "repaired": e.grounding.repaired}
    # The obvious follow-up is offered, never answered. "What is the formula"
    # does not become "and here is last month's value" unless the user asks.
    #
    # Built deterministically when the model does not supply one, which it
    # often does not: a button that is sometimes there is worse than one that
    # always is, and the metric label is enough to write the question.
    if a.also_measure:
        ans.suggestions = [a.also_measure]
    else:
        mm = v2model.get_model().metrics
        # Deduplicated: the metric tier and the glossary tier can both carry the
        # same ref, which offered the identical button twice.
        seen, out = set(), []
        for t in (e.terms or []):
            if t not in mm or t in seen:
                continue
            seen.add(t)
            out.append(f"{re.sub(r'\s*\([^)]*\)', '', mm[t].label).strip().lower()}"
                       f" last month")
        ans.suggestions = out[:2]
    ans.assumptions = a.assumptions
    return finish(ans)


def _decompose(ans: V2Answer, a, question: str, p, model, persona: str,
               budget, finish, fail, timings: dict, today: date) -> V2Answer:
    """
    Why a rate moved: traffic mix, or the segments themselves.

    The query is BUILT, not authored. The arithmetic needs one exact shape —
    both periods, per segment, numerator and denominator — and free-form SQL
    produces that shape by luck. `drivers.decompose_sql()` emits it from the
    semantic model, so the model chooses only the metric, the dimension and
    the two windows. It still passes check 10 and all nine guard checks: being
    generated by us is not a reason to trust it less than anything else.
    """
    wins = {phrase: (s.isoformat(), e.isoformat())
            for phrase, s, e in periods.resolve(today)}
    tgt, base = wins.get(a.window.lower()), wins.get(a.baseline.lower())
    if not (tgt and base):
        known = ", ".join(sorted(wins)[:8])
        return fail(f"I could not resolve both periods for that comparison "
                    f"({a.window!r} and {a.baseline!r}). Periods I understand "
                    f"include: {known}.")

    mid = (a.terms or [""])[0]
    sql = drivers.decompose_sql(mid, a.dimension, base, tgt)
    ans.sql_authored = sql

    t = time.time()
    events.emit("stage", "validate", status="start",
                message="check 10, then the nine guard checks")
    try:
        checks.check_v2(sql)
        g = guard(sql, p, model)
    except (checks.CheckFailed, GuardBlocked, PermissionError) as exc:
        reason = sqlfix.strip_ansi(getattr(exc, "reason", str(exc)))
        ans.attempts.append({"sql": sql, "stage": getattr(exc, "check", "guard"),
                             "ok": False, "reason": reason})
        return fail("I could not build the comparison query for that question.",
                    detail=reason)
    ans.checks_passed = list(g.checks_passed)
    ans.injected_predicates = list(g.injected_predicates)
    ans.sql_executed = g.sql
    ans.attempts.append({"sql": sql, "stage": "ok", "ok": True})
    timings["validate"] = int((time.time() - t) * 1000)
    events.emit("stage", "validate", status="done", ms=timings["validate"],
                message=f"{len(ans.checks_passed)} checks passed")

    t = time.time()
    events.emit("stage", "execute", status="start", message="running it, read-only")
    try:
        rows = execute(g.sql, timeout_s=config.SQL_TIMEOUT_S).rows
    except Exception as exc:  # noqa: BLE001
        return fail("The comparison query failed to run.",
                    detail=f"{type(exc).__name__}: {sqlfix.strip_ansi(str(exc))}")
    timings["execute"] = int((time.time() - t) * 1000)
    events.emit("stage", "execute", status="done", ms=timings["execute"],
                message=f"{len(rows):,} rows")

    b = drivers.segments_from_rows(rows, "baseline")
    tg = drivers.segments_from_rows(rows, "target")
    if not (b and tg):
        return fail("One of the two periods returned no rows, so there is "
                    "nothing to compare.")
    dec = drivers.decompose(a.dimension, b, tg)

    # The identity is the correctness test. If the weights do not sum to one
    # the split is meaningless, and a meaningless split presented confidently
    # is worse than no split at all.
    if abs(dec.residual_pp) > 1e-6:
        return fail("The decomposition did not balance, so I will not report "
                    "it.", detail=f"residual {dec.residual_pp:.6f}pp")

    ans.rows = rows
    ans.row_count = len(rows)
    ans.decomposition = dec.as_dict()
    ans.assumptions = list(a.assumptions) + [
        f"compared {a.window} ({tgt[0]} to {tgt[1]}) against "
        f"{a.baseline} ({base[0]} to {base[1]})"]
    events.emit("stage", "decompose", status="done",
                message=f"{dec.dominant} effect dominates — "
                        f"{abs(dec.mix_share_pct):.0f}% of the move is traffic mix")

    t = time.time()
    events.emit("stage", "synthesise", status="start",
                message=f"asking {config.SYNTH_MODEL} to write it up")
    s = synth.narrate_decomposition(question, dec, persona=persona, budget=budget)
    timings["synthesise"] = int((time.time() - t) * 1000)
    events.emit("stage", "synthesise", status="done", ms=timings["synthesise"],
                message=f"grounding: {s.grounding.verdict}")

    ans.headline = s.headline
    ans.observed = s.observed
    ans.hypotheses = s.hypotheses
    ans.caveats = list(s.caveats)
    ans.composed_by = s.composed_by
    ans.facts = [{"id": f.id, "label": f.label, "value": f.value,
                  "detail": f.detail} for f in s.facts]
    ans.grounding = {"verdict": s.grounding.verdict, "note": s.grounding.note,
                     "numbers_checked": s.grounding.numbers_checked,
                     "ungrounded": s.grounding.ungrounded,
                     "wrong_superlatives": s.grounding.wrong_superlatives,
                     "repaired": s.grounding.repaired}
    ans.chart = {"chart_type": "waterfall", "title":
                 f"{dec.dimension}: mix versus rate"}
    return finish(ans)


def _replay(question: str, *, role: str, today: date, p, model, ans: V2Answer):
    """
    A cached query, re-validated before it is allowed anywhere near the data.

    Returns an `Authored` to use in place of call 1, or None to author
    normally. A stored query that no longer passes is a MISS and a demotion,
    never an error — the question simply goes to the model as if the cache had
    been empty.
    """
    hit = sqlcache.lookup(question, role=role, today=today)
    if hit is None:
        return None

    sql, repairs = sqlfix.repair(hit.sql)
    try:
        checks.check_v2(sql)
        guard(sql, p, model)
    except (checks.CheckFailed, GuardBlocked, PermissionError) as exc:
        reason = sqlfix.strip_ansi(getattr(exc, "reason", str(exc)))
        sqlcache.demote(hit, reason)
        events.recovery("cache", "a cached query no longer passes validation — "
                                 "dropped it and asked the model again",
                        detail=reason)
        return None

    if repairs:
        ans.sql_repairs.extend(repairs)
    sqlcache.touch(hit)
    ans.cache_hit = hit.layer
    events.emit("stage", "cache", status="done",
                message=f"reused a verified query from {hit.layer} — "
                        f"skipping the author call")
    a = author_mod.Authored(action="sql", sql=sql, intent=hit.intent,
                            chart=hit.chart, assumptions=list(hit.assumptions),
                            playbook_ids=list(hit.playbook_ids))
    a.notes.append(f"query reused from the {hit.layer} cache")
    return a


def ask(question: str, *, role: str = "cxo", claims: dict | None = None,
        today: date | None = None, conversation: list[dict] | None = None,
        use_kb: bool = True, on_event=None) -> V2Answer:
    """
    Run a question end to end.

    `on_event` receives each stage and recovery as it happens, which is what
    /v2/ask/stream forwards to the browser. It is optional and the pipeline does
    not depend on it: with no listener the channel collects the same events and
    they arrive with the answer as `recoveries`.
    """
    ch = events.Channel(sink=on_event)
    with events.bind(ch):
        ans = _ask(question, role=role, claims=claims, today=today,
                   conversation=conversation, use_kb=use_kb)
    ans.recoveries = ch.of_kind("recovery")
    return ans


def _ask(question: str, *, role: str = "cxo", claims: dict | None = None,
         today: date | None = None, conversation: list[dict] | None = None,
         use_kb: bool = True) -> V2Answer:
    t0 = time.time()
    timings: dict = {}
    budget = CallBudget(limits={
        "author": config.MAX_AUTHOR_CALLS,
        "heal": config.MAX_HEAL_CALLS,
        "synth": config.MAX_SYNTH_CALLS,
        "repair": config.MAX_REPAIR_CALLS,
    })
    ans = V2Answer(question=question, role=role)
    model = v2model.get_model()

    def finish(a: V2Answer) -> V2Answer:
        a.total_ms = int((time.time() - t0) * 1000)
        a.timings_ms = timings
        a.llm_calls = [{"purpose": r.purpose, "model": r.model, "ms": r.ms, "ok": r.ok}
                       for r in budget.records]
        return a

    def fail(reason: str, *, kind: str = "system", detail: str = "") -> V2Answer:
        """
        Rungs 3 and 4 of the ladder.

        `kind="data"` is the only one that may say the dataset does not hold
        something. Everything else is our failure and says so, and offers
        questions that do work rather than leaving the user at a dead end.
        """
        ans.ok, ans.refused, ans.refusal_reason = False, True, reason
        ans.failure_kind = kind
        ans.failure_detail = sqlfix.strip_ansi(detail)
        if kind != "data":
            ans.suggestions = _suggestions(question, model)
        return finish(ans)

    if not v2model.views_present():
        return fail("The v2 views are not built in this database. Run "
                    "`python -m paylens2.schema.build` and restart.", kind="config")

    # A principal is required before anything else: an unscoped query must never
    # be the fallback when a claim is missing.
    try:
        p = rbac.principal_for(role, claims or {})
        rbac.row_predicates(p, model)        # raises if a required claim is absent
    except PermissionError as exc:
        return fail(str(exc), kind="config")
    except KeyError:
        return fail(f"Unknown role '{role}'.", kind="config")

    persona = rbac.persona(p, model)
    today_s = (today or date.today()).isoformat()
    events.emit("stage", "prepare", status="done",
                message=f"role {role} resolved, views present")

    # ── 1. author — unless a verified query for this exact key already exists ─
    t = time.time()
    a = _replay(question, role=role, today=(today or date.today()), p=p,
                model=model, ans=ans)
    if a is not None:
        timings["author"] = int((time.time() - t) * 1000)
        return _measure(ans, a, question, p, model, persona, budget, finish,
                        fail, timings, use_kb, today or date.today())

    events.emit("stage", "author", status="start",
                message=f"asking {config.AUTHOR_MODEL} for the SQL")
    try:
        a = author_mod.author(question, persona=persona, today=today_s,
                              conversation=conversation, budget=budget)
    except LLMUnavailable as exc:
        return fail("v2 needs a language model to write its queries and none is "
                    "available right now.", kind="config", detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("authoring failed")
        return fail("I could not turn that into a query. This is a failure on my "
                    "side, not a gap in your data.",
                    detail=f"{type(exc).__name__}: {exc}")
    timings["author"] = int((time.time() - t) * 1000)
    events.emit("stage", "author", status="done", ms=timings["author"],
                message={"sql": "wrote a query", "advise": "chose the advisory path",
                         "clarify": "asked a question back",
                         "refuse": "declined"}.get(a.action, a.action))

    # ── 1b. one re-author when the response was not usable ───────────────────
    # A malformed clarification, advice with no catalogue entry, an empty query,
    # or a refusal naming something the schema holds. None of these is a fact
    # about the user's data, so none of them may be reported as one. The model
    # gets one more go, told exactly what was wrong, with the full schema and
    # catalogue still in front of it.
    if author_mod.needs_reauthor(a):
        why = a.reauthor_reason
        ans.attempts.append({"sql": a.sql, "stage": "reauthor", "ok": False,
                             "reason": why})
        events.recovery("author", "the first response was not usable — "
                                  "asking again with the reason attached",
                        detail=why)
        try:
            a = author_mod.author(question, persona=persona, today=today_s,
                                  conversation=conversation, budget=budget,
                                  complaint=why, purpose="heal")
        except Exception as exc:  # noqa: BLE001
            log.warning("re-author failed: %s", exc)
        if author_mod.needs_reauthor(a):
            # Rung 3. Our failure, said plainly, with questions that do work.
            return fail("I could not turn that into something I can run. This is "
                        "a failure on my side, not a gap in your data.",
                        detail=f"re-author still unusable: {a.reauthor_reason or why}")

    if a.action == "advise":
        return _advise(ans, a, question, p, model, persona, budget, finish, fail)

    if a.action == "explain":
        return _explain(ans, a, question, persona, budget, finish, timings)

    if a.action == "decompose" and config.USE_DECOMPOSE:
        return _decompose(ans, a, question, p, model, persona, budget, finish,
                          fail, timings, today or date.today())

    if a.action == "clarify":
        ans.needs_clarification = True
        ans.clarification = {"question": a.clarify_question, "options": a.options}
        ans.assumptions = a.assumptions
        return finish(ans)

    if a.action == "refuse":
        # Rung 4 — the only place entitled to say the data does not hold it.
        # `ok` goes false here as it does in fail(): a caller checking one flag
        # must not get a different answer depending on which rung refused.
        ans.ok = False
        ans.refused = True
        ans.failure_kind = "data"
        ans.refusal_reason = (
            f"This dataset does not hold {a.missing_concept or 'what was asked'}."
            + (f" What it does hold that is closest: {', '.join(a.nearest)}."
               if a.nearest else ""))
        ans.suggestions = _suggestions(question, model)
        return finish(ans)

    return _measure(ans, a, question, p, model, persona, budget, finish, fail,
                    timings, use_kb, today or date.today())


def _measure(ans: V2Answer, a, question: str, p, model, persona: str, budget,
             finish, fail, timings: dict, use_kb: bool,
             _today: date | None = None) -> V2Answer:
    """
    The measurement path: validate, execute, profile, compose, learn.

    Extracted so a cache hit and a fresh authoring run go through exactly the
    same code. If the two had separate paths they would drift, and the one
    that drifted would be the one nobody watches — a cached answer is the
    answer you look at least closely.
    """
    _today = _today or date.today()
    # ── 2–3. check 10 + guard, with the heal loop ────────────────────────────
    sql = a.sql
    ans.sql_authored = sql
    guarded = None
    t = time.time()
    events.emit("stage", "validate", status="start",
                message="check 10, then the nine guard checks")
    for attempt in range(config.MAX_HEAL_CALLS + 1):
        try:
            # Mechanical flaws are repaired before the checks rather than sent
            # back to the model: a single-colon cast is not a reasoning error,
            # and a round trip to fix one is waste. Every repair is recorded.
            sql, repairs = sqlfix.repair(sql)
            if repairs:
                ans.sql_repairs.extend(repairs)
            passed = checks.check_v2(sql)
            g = guard(sql, p, model)
            guarded = g
            ans.checks_passed = passed + list(g.checks_passed)
            ans.injected_predicates = list(g.injected_predicates)
            ans.attempts.append({"sql": sql, "stage": "ok", "ok": True})
            if repairs:
                events.recovery("validate", f"repaired the SQL before checking it: "
                                            f"{'; '.join(repairs)}")
            events.emit("stage", "validate", status="done",
                        ms=int((time.time() - t) * 1000),
                        message=f"{len(ans.checks_passed)} checks passed"
                                + (f", {len(g.injected_predicates)} row filter(s) "
                                   f"injected" if g.injected_predicates else ""))
            break
        except (checks.CheckFailed, GuardBlocked, PermissionError) as exc:
            stage = getattr(exc, "check", "guard")
            reason = sqlfix.strip_ansi(getattr(exc, "reason", str(exc)))
            hint = getattr(exc, "hint", "")
            ans.attempts.append({"sql": sql, "stage": stage, "ok": False,
                                 "reason": reason})
            events.recovery(
                "validate",
                f"{stage} rejected the query — re-authoring "
                f"({attempt + 1} of {config.MAX_HEAL_CALLS})", detail=reason)
            if attempt >= config.MAX_HEAL_CALLS:
                return fail(
                    "I could not build a query for that question. This is a "
                    "failure on my side, not a gap in your data — the query I "
                    "wrote would not pass validation.",
                    detail=f"[{stage}] {reason}")
            try:
                a2 = author_mod.heal(question, sql, f"[{stage}] {reason}", hint,
                                     budget=budget)
            except Exception as exc2:  # noqa: BLE001
                return fail("I could not build a query for that question, and the "
                            "repair attempt failed too.",
                            detail=f"[{stage}] {reason} | heal: {exc2}")
            if a2.action != "sql" or not a2.sql:
                return fail("I could not build a query for that question.",
                            detail=f"[{stage}] {reason}")
            sql = a2.sql
            a.chart = a2.chart or a.chart
            a.assumptions += a2.assumptions
    timings["validate"] = int((time.time() - t) * 1000)

    if guarded is None:
        return fail("I could not build a query for that question.")
    ans.sql_executed = guarded.sql

    # ── 4. execute + row cap ─────────────────────────────────────────────────
    t = time.time()
    events.emit("stage", "execute", status="start", message="running it, read-only")
    try:
        result = execute(guarded.sql, timeout_s=config.SQL_TIMEOUT_S)
    except QueryTimeout:
        return fail(f"That query took longer than the {config.SQL_TIMEOUT_S}s limit. "
                    f"Narrowing the period usually fixes it.")
    except Exception as exc:  # noqa: BLE001
        return fail("The query I built failed to run.",
                    detail=f"{type(exc).__name__}: {sqlfix.strip_ansi(str(exc))}")
    timings["execute"] = int((time.time() - t) * 1000)
    events.emit("stage", "execute", status="done", ms=timings["execute"],
                message=f"{len(result.rows):,} rows")

    rows = result.rows
    over_cap = len(rows) > config.MAX_SQL_ROWS
    aggregate = checks.is_aggregate(sql)
    if over_cap and aggregate:
        # A truncated aggregate is a wrong number wearing a complete one's
        # clothes. Blocking is the only honest option.
        return fail(
            f"That breakdown produced more than {config.MAX_SQL_ROWS:,} groups. "
            f"Truncating an aggregate would silently change the numbers, so it is "
            f"blocked rather than cut short. Narrow the breakdown or the period.")
    if over_cap:
        rows = rows[:config.MAX_SQL_ROWS]
        ans.truncated = True

    ans.rows = rows
    ans.row_count = len(rows)

    # ── 4b. data quality, against the window this answer actually used ───────
    t = time.time()
    cols = list(rows[0].keys()) if rows else []
    touches_money = bool(re.search(r"v_settlements|amount|gmv|settled", sql, re.I))
    ans.data_quality = dqmod.run_checks(
        guarded.sql, cols, p=p, model=model,
        with_settlements=touches_money or getattr(p, "role", "") == "merchant_user")
    timings["dq"] = int((time.time() - t) * 1000)
    if ans.data_quality:
        bad = [c for c in ans.data_quality if c["status"] != "pass"]
        events.emit("stage", "quality", status="done", ms=timings["dq"],
                    message=f"{len(ans.data_quality)} checks, "
                            f"{len(bad)} finding(s)" if bad
                            else f"{len(ans.data_quality)} checks, all clear")

    # ── 5. profile ───────────────────────────────────────────────────────────
    t = time.time()
    prof: Profile = profile(rows, truncated=ans.truncated,
                            metric_columns=_metric_columns(sql),
                            formats=_formats())
    timings["profile"] = int((time.time() - t) * 1000)
    events.emit("stage", "profile", status="done", ms=timings["profile"],
                message=f"{len(prof.facts)} facts measured in Python")
    ans.profile_text = prof.text
    ans.facts = [{"id": f.id, "label": f.label, "value": f.value, "detail": f.detail}
                 for f in prof.facts]

    # ── 6. synthesise ────────────────────────────────────────────────────────
    t = time.time()
    events.emit("stage", "synthesise", status="start",
                message=f"asking {config.SYNTH_MODEL} to write it up")
    advisory_text = catalogue.expand(a.playbook_ids) if a.playbook_ids else ""
    s = synth.synthesise(question, guarded.sql, prof, persona=persona,
                         assumptions=a.assumptions, advisory=advisory_text,
                         budget=budget)
    timings["synthesise"] = int((time.time() - t) * 1000)
    events.emit("stage", "synthesise", status="done", ms=timings["synthesise"],
                message=f"grounding: {s.grounding.verdict}")
    if s.grounding.repaired:
        events.recovery("synthesise", "the first composition stated a number that "
                                      "was not measured — repaired and re-checked")
    elif s.grounding.verdict == "rejected":
        events.recovery("synthesise", "the composition was discarded for an "
                                      "ungrounded figure — showing the measured "
                                      "answer instead", detail=s.grounding.note)

    ans.headline = s.headline
    ans.observed = s.observed
    ans.hypotheses = s.hypotheses
    ans.caveats = list(s.caveats)
    # Findings reach the reader, not just the tab. A warning nobody surfaces is
    # a warning that was not issued. Appended after the composition so they
    # cannot be overwritten, and deduplicated against what it already said.
    seen = {" ".join(c.lower().split()) for c in ans.caveats}
    for c in ans.data_quality:
        if c["status"] == "pass" or c["severity"] != "warn":
            continue
        if " ".join(c["message"].lower().split()) not in seen:
            ans.caveats.append(c["message"])
    ans.composed_by = s.composed_by
    ans.grounding = {"verdict": s.grounding.verdict, "note": s.grounding.note,
                     "numbers_checked": s.grounding.numbers_checked,
                     "ungrounded": s.grounding.ungrounded,
                     "wrong_superlatives": s.grounding.wrong_superlatives,
                     "repaired": s.grounding.repaired}
    # The proposal was written before a single row existed, so every column it
    # names is a guess. Honour it where the result allows; override where it
    # does not, and say which.
    ans.chart = chartmod.validate(a.chart, rows, _metric_columns(sql))
    ans.assumptions = a.assumptions
    ans.playbooks = a.playbook_ids
    if ans.truncated:
        ans.caveats.append(
            f"Showing {len(rows):,} of {result.row_count:,} rows — the detail "
            f"listing is truncated at the row cap.")

    # ── 7. write back ────────────────────────────────────────────────────────
    # Learn only from a CLEAN run: no rejected attempt, and the prose grounded.
    # A query that needed two repairs is not an example worth showing the next
    # question, and a composition that had to be discarded is evidence the shape
    # confused the model rather than helped it.
    clean = all(a.get("ok") for a in ans.attempts) and \
        s.grounding.verdict in ("grounded", "not_applicable")
    if clean:
        # The cache records the same runs the KB learns from. "Worth showing
        # the next question" and "safe to replay" are the same judgement, and
        # splitting them would mean two gates to keep in step.
        #
        # ORDER MATTERS. Both of these upsert the SAME Chroma record, keyed on
        # the canonical question. writeback() writes a fixed metadata block
        # with verified=False; remember() reads the record and merges into it.
        # Run them the other way round and the write-back silently clobbers
        # the promotion, so the cache fills up and never serves anything —
        # which is exactly what happened the first time.
        if use_kb:
            retrieval.writeback(question, sql, ok=True)
        state = sqlcache.remember(question, role=ans.role, today=_today,
                                  payload=sqlcache.payload_of(a))
        ans.cache_state = state
        events.emit("stage", "learn", status="done",
                    message="kept as an unverified example"
                            + (" · query promoted for reuse" if state == "promoted"
                               else " · one more clean run promotes it for reuse"
                               if state == "pending" else ""))
    elif use_kb:
        events.emit("stage", "learn", status="done",
                    message="not kept — the run was not clean")
    return finish(ans)
