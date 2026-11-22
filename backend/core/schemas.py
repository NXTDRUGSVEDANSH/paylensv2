"""
Typed contracts for the whole pipeline.

The important design choice is in AnswerCard: `observed`, `hypotheses` and
`recommendations` are three separate FIELDS, not three paragraphs of prose. The
model is structurally unable to present a guess as a fact.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Intent = Literal["single_value", "trend", "breakdown", "compare",
                 "explain_change", "rank", "detail"]
Severity = Literal["info", "warn", "block"]


class CacheTier(str, Enum):
    EXACT_REUSE = "exact_reuse"     # >= T1: reuse stored SQL, no planner call
    FEWSHOT = "fewshot"             # T2..T1: one planner call, match as few-shot
    COLD = "cold"                   # < T2: one planner call, schema card only


class TimeWindow(BaseModel):
    start: date
    end: date                       # inclusive

    @field_validator("end")
    @classmethod
    def _ordered(cls, v: date, info):
        s = info.data.get("start")
        if s and v < s:
            raise ValueError(f"window end {v} precedes start {s}")
        return v

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def label(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


class Filter(BaseModel):
    dimension: str
    op: Literal["=", "!=", "in", "not in", ">", "<", ">=", "<="] = "in"
    values: list[Any]


class Assumption(BaseModel):
    kind: Literal["time_window", "denominator", "grain", "metric", "segment", "other"]
    statement: str
    alternative: str | None = None


class ChartSpec(BaseModel):
    chart_type: Literal["line", "bar", "horizontal_bar", "stacked_bar", "stacked_area_100",
                        "waterfall", "pie", "scatter", "kpi", "table"] = "table"
    x: str | None = None
    y: str | None = None
    series: str | None = None
    title: str = ""
    overrides: list[str] = Field(default_factory=list)   # what the validator changed, and why


class AnswerStrategy(BaseModel):
    type: Literal["time_series_trend", "top_n_ranked", "distribution", "single_aggregate",
                  "detail_sample", "comparison", "filtered_ranking",
                  "driver_decomposition"] = "single_aggregate"
    focus: list[str] = Field(default_factory=list)


class AnalysisPlan(BaseModel):
    """What to compute. Produced by the planner (or rehydrated from cache)."""
    intent: Intent
    metric: str
    secondary_metrics: list[str] = Field(default_factory=list)
    window: TimeWindow
    baseline: TimeWindow | None = None
    segment_by: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time_grain: Literal["hour", "day", "week", "month"] | None = None
    top_n: int | None = None
    needs_decomposition: bool = False
    chart: ChartSpec | None = None
    answer_strategy: AnswerStrategy | None = None
    assumptions: list[Assumption] = Field(default_factory=list)


class Param(BaseModel):
    name: str
    type: Literal["timestamp", "date", "string", "int", "float", "list"]
    value: Any


class CompiledQuery(BaseModel):
    """Pre-RBAC, pre-LIMIT, parameterised. This is what the KB stores."""
    template_sql: str
    params: dict[str, Param] = Field(default_factory=dict)
    tables: list[str] = Field(default_factory=list)
    is_aggregate: bool = True
    value_columns: list[str] = Field(default_factory=list)
    segment_columns: list[str] = Field(default_factory=list)


class GuardResult(BaseModel):
    ok: bool
    sql: str = ""                   # post-RBAC, post-sentinel-limit
    checks_passed: list[str] = Field(default_factory=list)
    failed_check: str | None = None
    reason: str | None = None
    injected_predicates: list[str] = Field(default_factory=list)
    estimated_rows: int | None = None
    sentinel_limit: int | None = None


class Caveat(BaseModel):
    id: str
    severity: Severity
    message: str
    dataset: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class Segment(BaseModel):
    key: str                        # the dimension value, e.g. "NETBANKING"
    weight: float                   # volume share within the period
    value: float                    # the metric itself
    numerator: float | None = None
    denominator: float | None = None


class SegmentEffect(BaseModel):
    key: str
    mix_pp: float
    rate_pp: float
    total_pp: float
    weight_base: float
    weight_target: float
    value_base: float
    value_target: float


class Decomposition(BaseModel):
    """
    delta = mix + rate, exactly.
        mix  = sum (w_target - w_base) * value_base    traffic moved
        rate = sum  w_target * (value_target - value_base)  segments moved
    """
    dimension: str
    baseline_value: float
    target_value: float
    delta_pp: float
    mix_pp: float
    rate_pp: float
    residual_pp: float
    mix_share_pct: float
    segments: list[SegmentEffect] = Field(default_factory=list)

    @property
    def dominant(self) -> Literal["mix", "rate", "mixed"]:
        if abs(self.delta_pp) < 1e-9:
            return "mixed"
        s = abs(self.mix_pp) / (abs(self.mix_pp) + abs(self.rate_pp) + 1e-12)
        return "mix" if s >= 0.65 else "rate" if s <= 0.35 else "mixed"


class Fact(BaseModel):
    text: str
    numbers: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    text: str
    confidence: Literal["low", "medium", "high"]
    testable_with: str | None = None


class Recommendation(BaseModel):
    action: str
    owner: str
    rationale: str
    precondition: str | None = None


class Narration(BaseModel):
    headline: str
    observed: list[Fact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)


class Confidence(BaseModel):
    score: float
    bucket: Literal["low", "medium", "high"]
    deductions: list[dict[str, Any]] = Field(default_factory=list)


class Evidence(BaseModel):
    datasets: list[str] = Field(default_factory=list)
    sql: str = ""
    template_sql: str = ""
    filters: list[str] = Field(default_factory=list)
    window: str = ""
    baseline_window: str | None = None
    row_count: int = 0
    truncated: bool = False
    total_rows: int | None = None
    metric_definition: dict[str, str] = Field(default_factory=dict)
    guard_checks: list[str] = Field(default_factory=list)
    injected_predicates: list[str] = Field(default_factory=list)
    exec_ms: int = 0


class LLMCall(BaseModel):
    purpose: str
    model: str
    ms: int
    ok: bool


class RunTrace(BaseModel):
    followup: bool = False
    cache_tier: CacheTier = CacheTier.COLD
    cache_similarity: float | None = None
    cache_demotion_reason: str | None = None
    # Which knowledge-base record was reused, and what was offered as few-shots.
    # Without these the cache is a black box: you can see THAT it hit, but not
    # what it hit, which makes a wrong reuse impossible to spot by eye.
    cache_matched_id: str | None = None
    cache_matched_question: str | None = None
    cache_matched_canonical: str | None = None
    cache_matched_source: str | None = None
    fewshots: list[dict[str, Any]] = Field(default_factory=list)
    canonical_question: str | None = None
    shape_key: str | None = None
    answer_cache_hit: bool = False
    healed: bool = False
    heal_attempts: int = 0
    guard_first_try: bool = True
    llm_calls: list[LLMCall] = Field(default_factory=list)
    step_ms: dict[str, int] = Field(default_factory=dict)
    total_ms: int = 0
    golden_id: str | None = None

    @property
    def llm_call_count(self) -> int:
        return len(self.llm_calls)


class ScopeViolation(BaseModel):
    entity: str
    entity_type: str
    reason: str
    allowed_alternative: str | None = None


class ClarifyOption(BaseModel):
    label: str
    question: str                      # the full question to re-ask
    is_default: bool = False
    note: str | None = None


class Clarification(BaseModel):
    """
    The question cannot be answered completely as asked.

    Only raised when a MISSING input would change the answer materially — a time
    window, or a denominator choice. Anything the system can reasonably default is
    defaulted and stated as an assumption instead; asking about everything is its
    own kind of unhelpful.
    """
    reason: str
    missing: list[str] = Field(default_factory=list)
    options: list[ClarifyOption] = Field(default_factory=list)


class Finding(BaseModel):
    """One result of the health scan. See backend/core/diagnose.py for why each
    carries its own rule and owner rather than just a number."""
    id: str
    title: str
    detail: str
    severity: Literal["high", "medium", "low", "ok"] = "medium"
    impact_attempts: int = 0
    impact_value: float = 0.0
    rule: str = ""
    owner: str = ""
    action: str = ""
    rows: list[dict[str, Any]] = Field(default_factory=list)
    sql: str = ""
    metric_hint: str = ""


class Diagnostic(BaseModel):
    """A health scan: what was checked, and what cleared its threshold."""
    window_start: date
    window_end: date
    findings: list[Finding] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)


class EvidenceFact(BaseModel):
    """
    One measured number offered to the advisory composer.

    Every fact carries the SQL that produced it, because the point of the
    advisory path is that its numbers are the same numbers the metric path
    would give you — measured through the same guard, under the same access
    scope — rather than something a language model remembered about payments.
    """
    id: str                      # E1, E2 ... cited by the prose
    label: str
    value: str                   # already formatted; the composer never formats
    detail: str = ""
    probe: str = ""
    sql: str = ""


class Grounding(BaseModel):
    """
    The result of checking a composed answer against its evidence.

    A number in the prose that is not in the evidence pack is a fabricated
    statistic, and in a payments tool that is worse than no answer at all. See
    backend/core/advisory.py for why failing the check discards the whole
    composition rather than editing it.
    """
    numbers_checked: int = 0
    ungrounded: list[str] = Field(default_factory=list)
    citations_checked: int = 0
    bad_citations: list[str] = Field(default_factory=list)
    verdict: Literal["grounded", "rejected", "not_applicable"] = "not_applicable"
    note: str = ""


class AdvisoryLever(BaseModel):
    """One thing the user could do, selected from semantic/playbooks.yaml."""
    lever: str
    owner: str
    why: str
    measure: str = ""            # a question PayLens can answer to check it


class AdvisoryDefinition(BaseModel):
    term: str
    says: str
    matters: str = ""


class Advisory(BaseModel):
    """
    An answer to a question with no computable result.

    `composed_by` is load-bearing in the UI: a reader is entitled to know
    whether the prose was written by a model or assembled from the catalogue,
    because those two things fail in different ways.
    """
    kind: Literal["definition", "recommendation", "explanation",
                  "benchmark", "hypothetical"]
    title: str
    frames: str = ""             # the one idea to hold before the levers
    answer: str = ""             # composed prose
    definitions: list[AdvisoryDefinition] = Field(default_factory=list)
    levers: list[AdvisoryLever] = Field(default_factory=list)
    facts: list[EvidenceFact] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)   # catalogue ids used
    window_start: date | None = None
    window_end: date | None = None
    composed_by: Literal["llm", "catalogue"] = "catalogue"
    grounding: Grounding = Field(default_factory=Grounding)
    follow_ups: list[str] = Field(default_factory=list)


class Inherited(BaseModel):
    """
    What a follow-up carried over from the previous turn. Always rendered: an
    inherited assumption the user cannot see is one they cannot correct.
    """
    metric: str | None = None
    window_label: str | None = None
    filters: list[Filter] = Field(default_factory=list)
    segment_by: list[str] = Field(default_factory=list)
    replaced_filters: list[str] = Field(default_factory=list)
    from_question: str = ""
    summary: str = ""


class AnswerCard(BaseModel):
    ok: bool = True
    question: str
    role: str
    refused: bool = False
    refusal_reason: str | None = None
    scope_violations: list[ScopeViolation] = Field(default_factory=list)

    needs_clarification: bool = False
    clarification: Clarification | None = None

    plan: AnalysisPlan | None = None
    narration: Narration | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    chart: ChartSpec | None = None
    decompositions: list[Decomposition] = Field(default_factory=list)
    caveats: list[Caveat] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    confidence: Confidence | None = None
    evidence: Evidence | None = None
    diagnostic: Diagnostic | None = None
    advisory: Advisory | None = None
    inherited: Inherited | None = None
    conversation_id: str | None = None
    turn: int | None = None
    trace: RunTrace = Field(default_factory=RunTrace)
    generated_at: datetime = Field(default_factory=datetime.now)
