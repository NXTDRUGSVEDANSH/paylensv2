"""
The baseline: two LLM calls over the raw tables, and nothing else.

WHAT IT DELIBERATELY DOES NOT HAVE
----------------------------------
No views, so it must de-duplicate `fact_transactions` itself or its counts
inflate — the planted double-ingest is still in there. No semantic model, so
nothing tells it that `success_rate` excludes PENDING. No check 10, no guard,
no RBAC injection, no profiler, no fact pack, no grounding check. Call 1 writes
SQL from the raw DDL, call 2 narrates whatever came back.

This is not a straw man; it is the shape most text-to-SQL systems ship in, and
it is the same baseline the prior-art report used ("raw DDL prompt only; call 1
generates SQL, call 2 narrates"), so the two baseline columns are comparable.

WHY IT NEEDS NO SAFETY NET
--------------------------
`backend/core/db.py` opens the warehouse `read_only=True`, so a `DROP` cannot
land however badly the model behaves, and `executor.execute()` adds a statement
timeout without calling `guard()`. So the baseline can be given the real
executor and still be a real baseline. It is *supposed* to be able to write a
wrong query and run it — that is the measurement.

The point of the comparison is not that the baseline is bad. It is that the
difference between the columns is exactly the value of the views, the guard and
the profiler, priced in the same units.
"""
from __future__ import annotations

import functools
import re
import time
from datetime import date

from evaluation.pipelines.base import EvalResult, Pipeline

RAW_TABLES = ("fact_transactions", "fact_settlements", "dim_merchant",
              "dim_bank", "dim_payment_method", "dim_terminal",
              "dim_failure_code")

SQL_SYSTEM = """You are a data analyst who writes DuckDB SQL.

Given a database schema and a question, return ONLY the SQL query. No markdown,
no explanation, no commentary. One SELECT statement.

If the question cannot be answered from the schema, return exactly:
CANNOT_ANSWER"""

ANSWER_SYSTEM = """You are a data analyst. Given a question, the SQL that was
run and the rows it returned, write a one to three sentence answer.

Be direct. Amounts are in Indian rupees. Return only the answer text."""


@functools.lru_cache(maxsize=1)
def raw_ddl() -> str:
    """
    The raw tables, and only the raw tables.

    The `v_*` views are excluded on purpose: handing the baseline the view
    layer would be handing it the thing under test.
    """
    from backend.core import db
    cols = db.table_columns()
    lines = []
    for t in RAW_TABLES:
        if t in cols:
            lines.append(f"TABLE {t} ({', '.join(sorted(cols[t]))})")
    return "\n".join(lines)


def _strip(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    text = re.sub(r"```(?:sql)?\s*", "", text).replace("```", "")
    return text.strip()


class NaiveBaselinePipeline(Pipeline):
    name = "naive_baseline"

    def __init__(self, *, today: date):
        self.today = today

    def run(self, record: dict, **kw) -> EvalResult:
        from backend.core.executor import execute
        from backend.llm.provider import call_text
        from paylens2 import config as v2cfg

        res = EvalResult(
            question=record["q"], pipeline_name=self.name,
            question_id=record.get("id", ""), bucket=record.get("bucket", ""),
            domain=record.get("domain", ""),
            difficulty=record.get("difficulty", ""),
            phrasing=record.get("phrasing", ""), role=record.get("role", "cxo"),
            expected_action=record.get("expected_action", ""),
            expected_metrics=record.get("expected_metrics") or [],
            expected_dimensions=record.get("expected_dimensions") or [],
            gold_sql_id=record.get("gold_sql_id"), record=dict(record),
        )
        t0 = time.time()
        prompt = (f"# SCHEMA\n{raw_ddl()}\n\n"
                  f"# TODAY\n{self.today.isoformat()}\n\n"
                  f"# QUESTION\n{record['q']}")

        try:
            sql = _strip(call_text(prompt, system=SQL_SYSTEM,
                                   model=v2cfg.AUTHOR_MODEL,
                                   purpose="baseline_sql", budget=None))
        except Exception as exc:  # noqa: BLE001
            res.latency_ms = (time.time() - t0) * 1000
            res.error = f"{type(exc).__name__}: {exc}"
            res.failure_kind = "system"
            res.action = "failed"
            return res

        res.llm_calls.append({"purpose": "baseline_sql",
                              "model": v2cfg.AUTHOR_MODEL,
                              "ms": round((time.time() - t0) * 1000), "ok": True})

        if not sql or sql.upper().startswith("CANNOT_ANSWER"):
            # The baseline's only refusal mechanism. It has no schema-grounded
            # way to tell "absent from the data" from "I could not write it",
            # so everything lands in one bucket — which is itself the finding.
            res.latency_ms = (time.time() - t0) * 1000
            res.action = "refuse"
            res.refused = True
            res.failure_kind = "data"
            res.error = "model returned CANNOT_ANSWER"
            return res

        res.sql = res.sql_authored = sql.rstrip(";")
        res.action = "sql"
        res.measured = True
        res.attempts = [{"sql": res.sql, "stage": "none", "ok": True}]

        try:
            out = execute(res.sql, timeout_s=10)
            res.rows = list(out.rows or [])
            res.row_count = len(res.rows)
            res.success = True
        except Exception as exc:  # noqa: BLE001
            res.latency_ms = (time.time() - t0) * 1000
            res.error = f"{type(exc).__name__}: {exc}"
            res.failure_kind = "system"
            res.attempts = [{"sql": res.sql, "stage": "execute", "ok": False,
                             "reason": str(exc)[:300]}]
            # No heal loop: the whole point of the baseline is that there is
            # nothing between a bad query and the user.
            res.first_shot_success = False
            return res

        t1 = time.time()
        try:
            rows_preview = str(res.rows[:20])
            res.answer = _strip(call_text(
                f"# QUESTION\n{record['q']}\n\n# SQL\n{res.sql}\n\n"
                f"# ROWS (first 20 of {res.row_count})\n{rows_preview}",
                system=ANSWER_SYSTEM, model=v2cfg.SYNTH_MODEL,
                purpose="baseline_answer", budget=None))
            res.llm_calls.append({"purpose": "baseline_answer",
                                  "model": v2cfg.SYNTH_MODEL,
                                  "ms": round((time.time() - t1) * 1000),
                                  "ok": True})
        except Exception as exc:  # noqa: BLE001
            res.answer = ""
            res.llm_calls.append({"purpose": "baseline_answer",
                                  "model": v2cfg.SYNTH_MODEL,
                                  "ms": round((time.time() - t1) * 1000),
                                  "ok": False, "error": str(exc)[:200]})

        res.latency_ms = (time.time() - t0) * 1000
        res.first_shot_success = True
        # There is no grounding check to record a verdict from, and that
        # absence is a measurement rather than a gap: `grounding_rate` reports
        # n/a for this pipeline, which is the honest answer.
        return res
