"""Execution Accuracy (EX) metrics for BIRD dataset evaluation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluation.pipelines.base import EvalResult


def _norm_val(v):
    """Normalise a single cell value for comparison."""
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, str):
        return v.strip().lower()
    return v


def normalise_rows(rows: list[dict]) -> list[tuple]:
    """Sort rows and convert to hashable tuples for set comparison.

    BIRD-standard: compares **values only**, ignoring column names.
    This avoids false negatives when predicted SQL uses different aliases
    (e.g. ``AS ratio`` vs the raw expression ``SUM(...)/SUM(...)``).

    Values within each row are sorted by their string representation so
    that column ordering differences are neutralised.  Floats are rounded
    to 4 dp; strings are lowercased and stripped.
    """
    normalised = []
    for row in rows:
        vals = tuple(sorted((_norm_val(v) for v in row.values()), key=lambda x: str(x)))
        normalised.append(vals)
    return sorted(normalised, key=lambda t: str(t))


def ex_score(pred_rows: list[dict], gold_rows: list[dict]) -> float:
    """Return 1.0 if result sets match (order-insensitive), 0.0 otherwise.

    Comparison is performed on normalised, sorted row tuples so that row
    ordering and minor float precision differences do not cause false negatives.
    """
    if normalise_rows(pred_rows) == normalise_rows(gold_rows):
        return 1.0
    return 0.0


def compute_ex_metrics(results: list["EvalResult"]) -> dict:
    """Compute Execution Accuracy metrics for a list of BIRD EvalResults.

    Metrics computed:
      - ex:             Overall EX using the final sql/rows after any healing.
      - first_shot_ex:  EX restricted to queries that succeeded on the first
                        attempt (first_shot_success=True) AND whose rows match
                        the gold rows.
      - healed_ex:      EX among queries that required healing
                        (first_shot_success=False).  Numerator counts those
                        that eventually succeeded and whose final rows match gold.
      - n_total:        Total number of results evaluated.
      - n_ex_pass:      Queries whose final rows match gold.
      - n_first_shot_pass: Queries that were first-shot successes with gold match.

    Returns a dict containing all of the above fields.
    """
    n_total = len(results)
    n_ex_pass = 0
    n_first_shot_pass = 0
    n_healed_attempted = 0
    n_healed_gold_match = 0

    for r in results:
        gold = r.gold_rows or []
        pred = r.rows or []

        # Overall EX: did the final execution produce the correct result?
        if r.success and ex_score(pred, gold) == 1.0:
            n_ex_pass += 1

        # First-shot EX: no healing was needed AND result is correct.
        if r.first_shot_success and ex_score(pred, gold) == 1.0:
            n_first_shot_pass += 1

        # Healed queries: any healing was attempted (error-based or result-aware).
        if r.heal_attempts > 0:
            n_healed_attempted += 1
            if r.success and ex_score(pred, gold) == 1.0:
                n_healed_gold_match += 1

    ex = n_ex_pass / n_total if n_total else 0.0
    first_shot_ex = n_first_shot_pass / n_total if n_total else 0.0
    healed_ex = (
        n_healed_gold_match / n_healed_attempted if n_healed_attempted else 0.0
    )

    return {
        "ex": round(ex, 4),
        "first_shot_ex": round(first_shot_ex, 4),
        "healed_ex": round(healed_ex, 4),
        "n_total": n_total,
        "n_ex_pass": n_ex_pass,
        "n_first_shot_pass": n_first_shot_pass,
    }


# ---------------------------------------------------------------------------
# Tolerance- and scale-aware EX, for the own-query track
# ---------------------------------------------------------------------------
# Exact set equality is the right test on BIRD, where gold SQL and predicted
# SQL run on the same engine over the same tables. It is the wrong test here,
# for two measured reasons:
#
#   SCALE. v2 reports a ratio metric as a percent (86.60) because the format is
#   declared in views.yaml; every hand-written reference in eval/golden.yaml
#   returns the raw ratio (0.8660). The existing harness papers over this with
#   `if want < 1: want *= 100` (eval/run.py ~line 97), which silently guesses.
#   Here the bank states it: `gold_scale` is "ratio" or "percent", so the
#   comparison knows rather than infers.
#
#   PRECISION. The reference computes from fact_transactions with a QUALIFY
#   de-duplication; v2 computes from the view's indicator columns. The float
#   arithmetic differs in the last places. 0.1% relative tolerance is the same
#   bar eval/run.py already used and is far tighter than any defect this is
#   meant to catch.
#
# Strings still compare exactly (lowered and stripped, as normalise_rows does),
# because a wrong label is a wrong answer at any tolerance.

def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _cells_match(pred, gold, *, rel: float, scales: tuple[float, ...]) -> bool:
    pn, gn = _num(pred), _num(gold)
    if pn is None or gn is None:
        return _norm_val(pred) == _norm_val(gold)
    for factor in scales:
        want = gn * factor
        if abs(pn - want) <= max(rel * abs(want), 1e-9):
            return True
    return False


def ex_score_numeric(pred_rows: list[dict], gold_rows: list[dict], *,
                     rel: float = 0.001, gold_scale: str = "ratio") -> float:
    """
    1.0 if the predicted result matches the reference within tolerance.

    Row order is ignored, as in `ex_score`. Within a row, values are matched
    greedily rather than positionally, because the model is free to name and
    order its output columns however it likes — a correct answer that puts the
    count before the rate is still a correct answer.

    `gold_scale` says what the REFERENCE returns. "ratio" allows the prediction
    to be the same number or 100x it; "percent" allows the same number or
    1/100th. "exact" disables scale tolerance entirely.
    """
    scales = {"ratio": (1.0, 100.0), "percent": (1.0, 0.01),
              "exact": (1.0,)}.get(gold_scale, (1.0, 100.0))

    if len(pred_rows or []) != len(gold_rows or []):
        return 0.0
    if not gold_rows:
        return 1.0

    remaining = [dict(r) for r in pred_rows]
    for grow in gold_rows:
        gvals = list(grow.values())
        hit = None
        for i, prow in enumerate(remaining):
            pvals = list(prow.values())
            unused = list(range(len(pvals)))
            ok = True
            for gv in gvals:
                found = next((j for j in unused
                              if _cells_match(pvals[j], gv, rel=rel,
                                              scales=scales)), None)
                if found is None:
                    ok = False
                    break
                unused.remove(found)
            if ok:
                hit = i
                break
        if hit is None:
            return 0.0
        remaining.pop(hit)
    return 1.0


def compute_own_ex(results: list["EvalResult"], *, rel: float = 0.001) -> dict:
    """
    EX over the gold-SQL subset of the own-query bank.

    Only results carrying `gold_rows` are scored — the other records have no
    reference to be right or wrong against, and folding them in as passes or
    failures would both be a lie. `n_scored` is reported so the denominator is
    never ambiguous.
    """
    scored = [r for r in results if r.gold_rows is not None]
    n = len(scored)
    if not n:
        return {"own_ex": None, "own_first_shot_ex": None, "n_scored": 0,
                "n_pass": 0, "failures": []}

    passes, first_shot, failures = 0, 0, []
    for r in scored:
        scale = (r.record or {}).get("gold_scale", "ratio") \
            if hasattr(r, "record") else "ratio"
        got = ex_score_numeric(r.rows or [], r.gold_rows or [],
                               rel=rel, gold_scale=scale)
        if got == 1.0:
            passes += 1
            if r.first_shot_success:
                first_shot += 1
        else:
            failures.append(r.question_id or r.question[:60])

    return {"own_ex": round(passes / n, 4),
            "own_first_shot_ex": round(first_shot / n, 4),
            "n_scored": n, "n_pass": passes, "failures": failures[:20]}
