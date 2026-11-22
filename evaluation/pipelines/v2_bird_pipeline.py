"""
The BIRD subject: v2's authoring core, and nothing downstream of it.

WHAT IS UNDER TEST
------------------
Call 1, `sqlfix.repair`, the heal loop, check 10 and the nine guard checks.
Not the profiler, not the synthesiser, not the grounding check, not the data-
quality probes. BIRD measures whether a system turns a question into the right
SQL; the prose half has nothing to be scored against and running it would cost
a second model call per question to produce a number nobody reads.

WHAT IS REUSED, NOT REIMPLEMENTED
---------------------------------
`prompts.AUTHOR_SYSTEM`, `prompts.author_payload`, `llmjson.call`,
`author._coerce`, `author._or_salvage`, `author.validate`, `sqlfix.repair`,
`checks.check_v2` and `backend.core.guard.guard` — all exactly as production
calls them. A second copy of the heal loop would drift from the real one, and
the copy that drifted would be the one claiming the benchmark score.

`author.author()` itself cannot be used, because it hardcodes
`v2model.schema_card()` (the PayLens card) and `periods.card()` (meaningless
for BIRD). So the payload is built directly with the BIRD card, and everything
after the model call is the production path.

NO RESULT-AWARE HEALING, DELIBERATELY
-------------------------------------
The reference harness passed the gold row count and gold column names into its
pipeline and let it re-prompt against them, which is where its 44.4% healed-EX
comes from. That measures healing-with-an-oracle. Here the heal loop sees only
the rejection reason from check 10 and the guard — never anything derived from
the answer — so a lower healed EX here is a different protocol, not a worse
system, and the report says so.
"""
from __future__ import annotations

import time

from evaluation.pipelines.base import EvalResult, Pipeline

PERSONA = ("You are writing SQL for a data analyst. Return the query only, in "
           "the JSON shape described. Quote identifiers containing spaces or "
           "punctuation with double quotes.")


class V2BirdPipeline(Pipeline):
    name = "paylens_v2_author_guard"

    def __init__(self, *, max_heal: int | None = None):
        from paylens2 import config as v2cfg
        self.max_heal = v2cfg.MAX_HEAL_CALLS if max_heal is None else max_heal

    def run(self, record: dict, **kw) -> EvalResult:
        from backend.core import rbac
        from backend.core.guard import GuardBlocked, guard
        from backend.llm.provider import CallBudget
        from paylens2.core import author as author_mod
        from paylens2.core import checks, llmjson, prompts, sqlfix

        from evaluation.datasets import bird_adapter

        db_id = record["db_id"]
        res = EvalResult(question=record["question"], pipeline_name=self.name,
                         question_id=record.get("id", ""),
                         bucket=db_id, domain="bird",
                         difficulty=record.get("difficulty", "unknown"),
                         complexity=record.get("difficulty", "unknown"),
                         expected_action="sql", record=dict(record))
        t0 = time.time()
        budget = CallBudget(limits={"author": 1, "heal": self.max_heal})

        try:
            card = bird_adapter.schema_card_for(db_id)
            model = bird_adapter.model_for(db_id)
            principal = rbac.principal_for("cxo", {})
        except Exception as exc:  # noqa: BLE001
            res.latency_ms = (time.time() - t0) * 1000
            res.error = f"setup: {type(exc).__name__}: {exc}"
            res.failure_kind = "config"
            return res

        question = record["question"]
        if record.get("evidence"):
            # BIRD ships a hint with many questions and the benchmark intends
            # it to be available. Withholding it would measure a harder task
            # than the published numbers were measured on.
            question = f"{question}\n\nHint: {record['evidence']}"

        payload = prompts.author_payload(
            question, card, role_persona=PERSONA, fewshots=[], catalogue=[],
            today=record.get("today", ""), conversation=None, periods_card="")

        sql, authored_ok = "", False
        try:
            raw, text = llmjson.call(payload, system=prompts.AUTHOR_SYSTEM,
                                     model=_author_model(), purpose="author",
                                     budget=budget)
            a = author_mod.validate(author_mod._coerce(
                author_mod._or_salvage(raw, text)))
            sql = a.sql or ""
            authored_ok = bool(sql)
            res.llm_calls.append({"purpose": "author", "model": _author_model(),
                                  "ms": round((time.time() - t0) * 1000),
                                  "ok": True})
        except Exception as exc:  # noqa: BLE001
            res.latency_ms = (time.time() - t0) * 1000
            res.error = f"author: {type(exc).__name__}: {exc}"
            res.failure_kind = "system"
            res.llm_calls.append({"purpose": "author", "model": _author_model(),
                                  "ms": round((time.time() - t0) * 1000),
                                  "ok": False})
            return res

        if not authored_ok:
            res.latency_ms = (time.time() - t0) * 1000
            res.error = "model returned no SQL"
            res.failure_kind = "system"
            res.action = a.action if "a" in dir() else "failed"
            return res

        res.sql_authored = sql
        res.action = "sql"
        res.measured = True

        # ── validate, with the production heal loop ─────────────────────────
        guarded = None
        with bird_adapter.bound(db_id):
            for attempt in range(self.max_heal + 1):
                sql, repairs = sqlfix.repair(sql)
                if repairs:
                    res.sql_repairs.extend(repairs)
                try:
                    passed = checks.check_v2(
                        sql, dialect=bird_adapter.BIRD_DIALECT,
                        skip=bird_adapter.BIRD_SKIP)
                    g = guard(sql, principal, model,
                              dialect=bird_adapter.BIRD_DIALECT)
                    guarded = g
                    res.checks_passed = passed + list(g.checks_passed)
                    res.attempts.append({"sql": sql, "stage": "ok", "ok": True})
                    break
                except (checks.CheckFailed, GuardBlocked, PermissionError) as exc:
                    stage = getattr(exc, "check", "guard")
                    reason = sqlfix.strip_ansi(
                        getattr(exc, "reason", str(exc)))
                    res.attempts.append({"sql": sql, "stage": stage,
                                         "ok": False, "reason": reason})
                    if attempt >= self.max_heal:
                        break
                    t1 = time.time()
                    try:
                        heal_payload = prompts.heal_payload(
                            question, sql, f"[{stage}] {reason}",
                            getattr(exc, "hint", ""), card)
                        raw2, text2 = llmjson.call(
                            heal_payload, system=prompts.HEAL_SYSTEM,
                            model=_author_model(), purpose="heal",
                            budget=budget)
                        a2 = author_mod.validate(author_mod._coerce(
                            author_mod._or_salvage(raw2, text2)))
                        res.llm_calls.append(
                            {"purpose": "heal", "model": _author_model(),
                             "ms": round((time.time() - t1) * 1000), "ok": True})
                        if a2.action != "sql" or not a2.sql:
                            break
                        sql = a2.sql
                    except Exception:  # noqa: BLE001
                        res.llm_calls.append(
                            {"purpose": "heal", "model": _author_model(),
                             "ms": round((time.time() - t1) * 1000),
                             "ok": False})
                        break

        failed = [x for x in res.attempts if not x.get("ok")]
        res.heal_attempts = len(failed)
        res.first_shot_success = not failed and not res.sql_repairs

        if guarded is None:
            res.latency_ms = (time.time() - t0) * 1000
            res.error = (f"[{res.attempts[-1]['stage']}] "
                         f"{res.attempts[-1].get('reason', '')}"
                         if res.attempts else "validation failed")
            res.failure_kind = "system"
            return res

        res.sql = guarded.sql

        # ── execute on SQLite, as the reference harness did ─────────────────
        # The guard needs DuckDB to parse and plan; the measurement runs in
        # BIRD's own engine so EX and VES sit beside the published figures
        # rather than being a different measurement with the same name.
        import sqlglot

        from evaluation.datasets import bird_loader
        try:
            exec_sql = sqlglot.transpile(
                guarded.sql, read=bird_adapter.BIRD_DIALECT,
                write="sqlite")[0]
        except Exception as exc:  # noqa: BLE001
            res.latency_ms = (time.time() - t0) * 1000
            res.error = f"transpile: {type(exc).__name__}: {exc}"
            # Its own category: a transpile failure is the harness's problem,
            # not a wrong answer from the model.
            res.failure_kind = "harness"
            return res

        conn = None
        try:
            conn = bird_loader.gold_conn(db_id)
            t2 = time.time()
            rows = conn.execute(exec_sql).fetchall()
            res.rows = [dict(r) for r in rows]
            res.row_count = len(res.rows)
            res.timings_ms["execute"] = round((time.time() - t2) * 1000)
            res.success = True
        except Exception as exc:  # noqa: BLE001
            res.error = f"execute: {type(exc).__name__}: {exc}"
            res.failure_kind = "system"
            res.attempts.append({"sql": exec_sql, "stage": "execute",
                                 "ok": False, "reason": str(exc)[:300]})
            res.first_shot_success = False
        finally:
            if conn is not None:
                conn.close()

        res.latency_ms = (time.time() - t0) * 1000
        return res


def _author_model() -> str:
    from paylens2 import config as v2cfg
    return v2cfg.AUTHOR_MODEL
