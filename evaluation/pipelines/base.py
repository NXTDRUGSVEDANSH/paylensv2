"""
The shared result record and the pipeline contract.

`EvalResult` keeps the reference harness's field names so every ported metric
module works unchanged, and adds the v2 fields the new metrics read. The added
fields all default to something harmless, so a pipeline that cannot supply one
(the naive baseline has no guard, so no `checks_passed`) simply leaves it empty
and the metrics that need it report N/A rather than zero — an unmeasured thing
and a thing measured at zero are not the same claim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class EvalResult:
    # ── the reference harness's contract, unchanged ──────────────────────────
    question: str
    pipeline_name: str
    sql: Optional[str] = None
    answer: str = ""
    rows: list = field(default_factory=list)
    latency_ms: float = 0.0
    success: bool = False
    error: Optional[str] = None
    first_shot_success: bool = False
    heal_attempts: int = 0
    gold_rows: Optional[list] = None
    gold_exec_time_ms: float = 0.0
    complexity: Optional[str] = None

    # ── identity, so a run is resumable and joinable back to the bank ────────
    question_id: str = ""
    bucket: str = ""
    domain: str = ""
    difficulty: str = ""
    phrasing: str = ""
    role: str = "cxo"

    # ── what was expected, carried through for the routing metrics ───────────
    expected_action: str = ""
    expected_metrics: list = field(default_factory=list)
    expected_dimensions: list = field(default_factory=list)
    gold_sql_id: Optional[str] = None
    # The bank row verbatim. Carried rather than unpacked field by field so a
    # metric can read an authored label — `period`, `gold_scale`, `notes` —
    # without every one of them becoming a column here.
    record: dict = field(default_factory=dict)

    # ── what v2 actually did ────────────────────────────────────────────────
    # True when the run actually authored and validated SQL. The advise /
    # explain / clarify / refuse branches never do, so including them in the
    # healing denominators would award them first-shot wins they did not earn.
    measured: bool = False
    action: str = ""                      # the branch call 1 took
    sql_authored: str = ""                # before sqlfix and before the guard
    row_count: int = 0
    truncated: bool = False
    facts: list = field(default_factory=list)
    grounding: dict = field(default_factory=dict)
    checks_passed: list = field(default_factory=list)
    injected_predicates: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    sql_repairs: list = field(default_factory=list)
    recoveries: list = field(default_factory=list)
    llm_calls: list = field(default_factory=list)
    timings_ms: dict = field(default_factory=dict)
    cache_hit: str = ""
    data_quality: list = field(default_factory=list)
    caveats: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    composed_by: str = ""
    playbooks: list = field(default_factory=list)
    decomposition: dict = field(default_factory=dict)
    needs_clarification: bool = False
    refused: bool = False
    failure_kind: str = ""
    failure_detail: str = ""

    # ── the judge, filled in afterwards ─────────────────────────────────────
    judge: dict = field(default_factory=dict)

    # Rows kept in the log. 20 is plenty for inspecting an answer, but a
    # record with gold SQL is scored for execution accuracy from this log, and
    # a 21-row gold result truncated to 20 was reported as a wrong answer when
    # the query was exactly right. Gold-bearing records therefore keep enough
    # rows to be compared.
    LOG_ROWS = 20
    LOG_ROWS_GOLD = 500

    def to_json(self) -> dict:
        """
        Row for the append-only JSONL.

        Rows are capped — the log exists to make every number re-derivable,
        not to re-store the warehouse — but never below what scoring needs.
        """
        from dataclasses import asdict
        d = asdict(self)
        cap = self.LOG_ROWS_GOLD if self.gold_sql_id else self.LOG_ROWS
        d["rows"] = (self.rows or [])[:cap]
        d["rows_truncated"] = len(self.rows or []) > cap
        d["gold_rows"] = (self.gold_rows or [])[:cap] if self.gold_rows else None
        return d


class Pipeline:
    """A thing that turns a question into an EvalResult. Never raises."""

    name: str = "base"

    def run(self, record: dict, **kw: Any) -> EvalResult:
        raise NotImplementedError
