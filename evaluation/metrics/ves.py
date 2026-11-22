"""Valid Efficiency Score (VES) — BIRD execution-time variant and static heuristic."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluation.pipelines.base import EvalResult


# ---------------------------------------------------------------------------
# BIRD VES (execution-time based)
# ---------------------------------------------------------------------------


def ves_bird(
    pred_rows: list[dict],
    gold_rows: list[dict],
    pred_time_ms: float,
    gold_time_ms: float,
) -> float:
    """Compute BIRD's Valid Efficiency Score for a single query.

    Formula:
      Vi  = 1 if result sets match (order-insensitive), else 0
      Ri  = min(gold_time_ms / pred_time_ms, 1.0)  — efficiency ratio capped at 1
      VES = Vi * Ri

    Returns 0.0 when pred_time_ms == 0 or the result sets do not match.
    """
    if pred_time_ms == 0:
        return 0.0

    # Import here to avoid a module-level circular dependency at runtime.
    from evaluation.metrics.ex_accuracy import ex_score  # noqa: PLC0415

    v_i = ex_score(pred_rows, gold_rows)
    if v_i == 0.0:
        return 0.0

    r_i = min(gold_time_ms / pred_time_ms, 1.0)
    return v_i * r_i


def compute_ves_bird(results: list["EvalResult"]) -> float:
    """Compute the mean BIRD VES across all results.

    Each EvalResult must have a ``gold_exec_time_ms`` attribute set by the runner.
    Results where the attribute is missing or None are skipped.

    Returns the mean VES as a float rounded to 4 decimal places.
    """
    scores: list[float] = []
    for r in results:
        gold_time_ms = getattr(r, "gold_exec_time_ms", None)
        if gold_time_ms is None:
            continue
        score = ves_bird(
            pred_rows=r.rows or [],
            gold_rows=r.gold_rows or [],
            pred_time_ms=r.latency_ms,
            gold_time_ms=gold_time_ms,
        )
        scores.append(score)

    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 4)


# ---------------------------------------------------------------------------
# Static heuristic VES (for own / production queries without gold SQL)
# ---------------------------------------------------------------------------


def ves_static(sql: str) -> float:
    """
    Heuristic SQL quality for queries with no gold SQL to compare against.

    The deductions are re-aimed at the PayLens schema. The reference harness
    penalised `fact_created_videos` and a missing `client_id`; here the
    equivalent mistakes are reaching past the views and answering a question
    with no time filter at all.

    Starts at 1.0:
      -0.25  SELECT * — on a 60-column view this is waste, and a model that
             selects everything has not decided what it needs
      -0.25  reads a raw fact_* / dim_* table instead of a view: the single
             mistake the view layer exists to prevent
      -0.20  no time predicate anywhere: a payments number with no window is a
             different question from the one that was asked
      -0.15  no LIMIT on a non-aggregate detail listing
      -0.10  a metric-shaped ratio computed with count(*) as the denominator,
             which silently includes PENDING
      -0.10  JOIN with no ON — a cartesian product

    Floored at 0.0. Regex-based on purpose: this is a cheap signal reported
    beside the real checks, not a replacement for them.
    """
    score = 1.0
    up = (sql or "").upper()

    if re.search(r"SELECT\s+\*", up):
        score -= 0.25

    if re.search(r"\b(FACT_TRANSACTIONS|FACT_SETTLEMENTS|DIM_[A-Z_]+)\b", up):
        score -= 0.25

    # A window can be expressed as a literal DATE, a txn_date/txn_ts/
    # settlement_date comparison, or a date_trunc grain.
    if not re.search(r"\b(TXN_TS|TXN_DATE|SETTLEMENT_DATE)\b", up) \
            and not re.search(r"\bDATE_TRUNC\b", up):
        score -= 0.20

    is_aggregate = bool(re.search(
        r"\b(GROUP\s+BY|COUNT\s*\(|SUM\s*\(|AVG\s*\(|MIN\s*\(|MAX\s*\(|MEDIAN\s*\(|QUANTILE)",
        up))
    if not is_aggregate and not re.search(r"\bLIMIT\b", up):
        score -= 0.15

    # The PENDING trap: a rate whose denominator is a bare count(*) rather
    # than the resolved count.
    if re.search(r"(SUCCESS_RATE|FAILURE_RATE)", up) and \
            re.search(r"/\s*(NULLIF\s*\(\s*)?COUNT\s*\(\s*\*\s*\)", up) and \
            "IS_RESOLVED" not in up:
        score -= 0.10

    if re.search(r"\bJOIN\b", up) and not re.search(r"\bON\b", up):
        score -= 0.10

    return round(max(score, 0.0), 4)


def compute_ves_static(results: list["EvalResult"]) -> dict:
    """Run ves_static over every result's SQL and aggregate."""
    scores = [ves_static(r.sql or "") for r in results]
    n_total = len(scores)
    return {
        "mean_ves": round(sum(scores) / n_total, 4) if n_total else 0.0,
        "scores": scores,
        "n_perfect": sum(1 for s in scores if s == 1.0),
        "n_penalised": sum(1 for s in scores if s < 1.0),
    }
