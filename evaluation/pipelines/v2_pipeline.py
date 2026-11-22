"""
The system under test: the full v2 pipeline, driven in process.

NOT OVER HTTP, DELIBERATELY
---------------------------
`POST /v2/ask` takes the role from the authenticated session and not from the
request body, which is the right call for a product and the wrong one for a
harness: the bank runs the same question as four different principals, and over
HTTP that would mean logging in and out between queries. `pipeline.ask()` takes
the role and its claims as arguments, so the RBAC bucket can actually be
measured.

WHAT IS DERIVED RATHER THAN READ
--------------------------------
`action` is not a field on the answer — the pipeline branches on it and then
reports the result, so it has to be reconstructed from which payload came back.
Doing it here, once, keeps every metric reading the same reconstruction.
"""
from __future__ import annotations

import time
from datetime import date

from evaluation.pipelines.base import EvalResult, Pipeline


def derive_action(ans) -> str:
    """
    Which of the six branches call 1 took.

    Order matters. `refused` is checked before `sql`, because a refusal can
    carry the SQL of an attempt that was rejected; and `explanation` before
    `advice`, because the explain path sets neither rows nor levers and would
    otherwise fall through to the measurement case.
    """
    if getattr(ans, "needs_clarification", False):
        return "clarify"
    if getattr(ans, "explanation", None):
        return "explain"
    if getattr(ans, "decomposition", None):
        return "decompose"
    if getattr(ans, "advice", None):
        return "advise"
    if getattr(ans, "refused", False) and getattr(ans, "failure_kind", "") == "data":
        return "refuse"
    if getattr(ans, "sql_authored", "") or getattr(ans, "sql_executed", ""):
        return "sql"
    # A system/config failure never reached a branch at all. Reporting it as
    # one of the six would put our own breakage into the routing matrix.
    return "failed"


class V2Pipeline(Pipeline):
    name = "paylens_v2"

    def __init__(self, *, today: date, use_kb: bool = False):
        """
        `use_kb=False` freezes the few-shot corpus; it does NOT disable it.

        Verified against the pipeline: `ask(use_kb=...)` is read only at the
        write-back (pipeline.py:865 and :875), while `retrieval.fewshots()`
        gates on the GLOBAL `paylens2.config.USE_KB`. So retrieval stays on —
        it is a shipped feature and switching it off would understate the
        system — while nothing is written back during a run.

        That is what makes the run order-independent. With write-back live,
        question 300 can be answered using an example question 150 created,
        so the score would depend on the order the bank happened to be in and
        a re-run would not reproduce it.
        """
        self.today = today
        self.use_kb = use_kb

    def run(self, record: dict, **kw) -> EvalResult:
        from paylens2.core.pipeline import ask

        res = EvalResult(
            question=record["q"], pipeline_name=self.name,
            question_id=record.get("id", ""), bucket=record.get("bucket", ""),
            domain=record.get("domain", ""), difficulty=record.get("difficulty", ""),
            phrasing=record.get("phrasing", ""), role=record.get("role", "cxo"),
            expected_action=record.get("expected_action", ""),
            expected_metrics=record.get("expected_metrics") or [],
            expected_dimensions=record.get("expected_dimensions") or [],
            gold_sql_id=record.get("gold_sql_id"), record=dict(record),
        )

        t0 = time.time()
        try:
            ans = ask(record["q"],
                      role=record.get("role") or "cxo",
                      claims=record.get("claims") or {},
                      today=self.today,
                      conversation=record.get("conversation") or None,
                      use_kb=self.use_kb)
        except Exception as exc:  # noqa: BLE001
            # A harness that dies on one question loses the whole run. An
            # exception is a result: it is this pipeline's worst outcome and
            # belongs in the numbers, not in a traceback.
            res.latency_ms = (time.time() - t0) * 1000
            res.success = False
            res.error = f"{type(exc).__name__}: {exc}"
            res.failure_kind = "harness"
            res.action = "failed"
            return res

        res.latency_ms = float(ans.total_ms or (time.time() - t0) * 1000)
        res.action = derive_action(ans)
        res.sql = ans.sql_executed or ""
        res.sql_authored = ans.sql_authored or ""
        res.rows = list(ans.rows or [])
        res.row_count = int(ans.row_count or 0)
        res.truncated = bool(ans.truncated)

        # "Success" means the system produced the answer it was asked for.
        # A correct refusal of an impossible question is therefore a success
        # on the refuse bucket and a failure everywhere else — that judgement
        # belongs to the routing metric, so ESR here tracks `ok` alone.
        res.success = bool(ans.ok)
        if not ans.ok:
            res.error = ans.refusal_reason or ""
        res.failure_kind = ans.failure_kind or ""
        res.failure_detail = ans.failure_detail or ""

        answer_bits = [ans.headline or "", *(ans.observed or [])]
        if ans.advice:
            answer_bits += [ans.advice.get("situation", ""),
                            *(ans.advice.get("whats_happening") or [])]
        if ans.explanation:
            answer_bits += [ans.explanation.get("definition", ""),
                            ans.explanation.get("formula_plain", "")]
        res.answer = "\n".join(b for b in answer_bits if b)

        res.facts = list(ans.facts or [])
        res.grounding = dict(ans.grounding or {})
        res.checks_passed = list(ans.checks_passed or [])
        res.injected_predicates = list(ans.injected_predicates or [])
        res.attempts = list(ans.attempts or [])
        res.sql_repairs = list(ans.sql_repairs or [])
        res.recoveries = list(ans.recoveries or [])
        res.llm_calls = list(ans.llm_calls or [])
        res.timings_ms = dict(ans.timings_ms or {})
        res.cache_hit = ans.cache_hit or ""
        res.data_quality = list(ans.data_quality or [])
        res.caveats = list(ans.caveats or [])
        res.assumptions = list(ans.assumptions or [])
        res.suggestions = list(ans.suggestions or [])
        res.composed_by = ans.composed_by or ""
        res.playbooks = list(ans.playbooks or [])
        res.decomposition = dict(ans.decomposition or {})
        res.needs_clarification = bool(ans.needs_clarification)
        res.refused = bool(ans.refused)

        failed = [a for a in res.attempts if not a.get("ok")]
        res.heal_attempts = len(failed)

        # `measured` gates every healing metric, and it has to exist.
        #
        # The advise / explain / clarify / refuse branches reach finish() with
        # `attempts == []` — they never authored a query that could be
        # rejected. Without this flag they score as first-shot successes,
        # because `all([])` is vacuously True, and a bank carrying 90
        # definitional and refusal records would hand the healing numbers 90
        # free wins. Healing efficiency is only meaningful over the runs that
        # actually tried to write SQL.
        res.measured = bool(res.attempts)
        res.first_shot_success = (
            bool(ans.ok) and not failed and not res.sql_repairs)

        return res
