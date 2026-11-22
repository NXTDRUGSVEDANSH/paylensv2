"""
The naive baseline on BIRD: one call to write SQL, execute, done.

No guard, no checks, no heal loop. Same prompt shape as the own-query baseline
so the two baseline columns mean the same thing.
"""
from __future__ import annotations

import time

from evaluation.pipelines.base import EvalResult, Pipeline
from evaluation.pipelines.naive_baseline import SQL_SYSTEM, _strip


class BirdBaselinePipeline(Pipeline):
    name = "naive_baseline"

    def run(self, record: dict, **kw) -> EvalResult:
        from backend.llm.provider import call_text
        from paylens2 import config as v2cfg

        from evaluation.datasets import bird_adapter, bird_loader

        db_id = record["db_id"]
        res = EvalResult(question=record["question"], pipeline_name=self.name,
                         question_id=record.get("id", ""), bucket=db_id,
                         domain="bird", difficulty=record.get("difficulty", ""),
                         complexity=record.get("difficulty", ""),
                         expected_action="sql", record=dict(record))
        t0 = time.time()
        question = record["question"]
        if record.get("evidence"):
            question = f"{question}\n\nHint: {record['evidence']}"
        card = bird_adapter.schema_card_for(db_id)

        try:
            sql = _strip(call_text(f"{card}\n\n# QUESTION\n{question}",
                                   system=SQL_SYSTEM,
                                   model=v2cfg.AUTHOR_MODEL,
                                   purpose="baseline_sql", budget=None))
            res.llm_calls.append({"purpose": "baseline_sql",
                                  "model": v2cfg.AUTHOR_MODEL,
                                  "ms": round((time.time() - t0) * 1000),
                                  "ok": True})
        except Exception as exc:  # noqa: BLE001
            res.latency_ms = (time.time() - t0) * 1000
            res.error = f"{type(exc).__name__}: {exc}"
            res.failure_kind = "system"
            return res

        if not sql or sql.upper().startswith("CANNOT_ANSWER"):
            res.latency_ms = (time.time() - t0) * 1000
            res.error = "model returned CANNOT_ANSWER"
            res.failure_kind = "data"
            res.action = "refuse"
            return res

        res.sql = res.sql_authored = sql.rstrip(";")
        res.action = "sql"
        res.measured = True
        res.attempts = [{"sql": res.sql, "stage": "none", "ok": True}]

        conn = None
        try:
            conn = bird_loader.gold_conn(db_id)
            rows = conn.execute(res.sql).fetchall()
            res.rows = [dict(r) for r in rows]
            res.row_count = len(res.rows)
            res.success = True
            res.first_shot_success = True
        except Exception as exc:  # noqa: BLE001
            res.error = f"execute: {type(exc).__name__}: {exc}"
            res.failure_kind = "system"
        finally:
            if conn is not None:
                conn.close()
        res.latency_ms = (time.time() - t0) * 1000
        return res
