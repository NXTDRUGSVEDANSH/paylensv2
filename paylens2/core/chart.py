"""
Chart selection for v2: the model proposes, the result shape decides.

Call 1 writes the chart spec BEFORE a single row exists, so every column it
names is a guess. It guesses well — it wrote the query — but it guesses, and a
chart keyed on a column that is not in the result draws nothing or, worse,
draws something misleading.

So the same contract the classic pipeline uses (backend/core/chartmeta.py:
"keep the model's proposal where it is possible; override where it is not"),
minus its dependency on an AnalysisPlan, which v2 has no concept of. The id
and column helpers are imported rather than re-written.

Every override is recorded and shown beside the chart. A system that silently
replaces the model's choice teaches nobody anything; one that says "you asked
for a line, there is no time column, here is a bar" is debuggable.
"""
from __future__ import annotations

from backend.core.chartmeta import (MAX_SERIES, TIME_HINTS, _label_columns,
                                    _numeric_columns, is_id_column)

KINDS = {"line", "bar", "horizontal_bar", "stacked_bar", "stacked_area_100",
         "pie", "kpi", "table"}
# Deliberately NOT accepted: waterfall. It needs a mix-vs-rate decomposition,
# which v2 does not compute — a waterfall drawn from ordinary rows would be a
# picture of an analysis that never happened.


def _time_column(cols: list[str]) -> str | None:
    """
    A time axis, matched on whole name parts rather than substrings.

    Substring matching made `attempts` a time column, because TIME_HINTS holds
    "ts" and "attempts" contains it. Every breakdown then came back as a line
    chart with the attempt count on the x-axis — a plausible-looking picture of
    nothing. Splitting on underscores means `txn_ts` still matches and
    `attempts` no longer does.
    """
    for c in cols:
        parts = set(str(c).lower().split("_"))
        if parts & set(TIME_HINTS):
            return c
    return None


def rule_chart(rows: list[dict], metric_columns: list[str] | None = None) -> dict:
    """The chart the result shape implies, ignoring whatever was proposed."""
    if not rows:
        return {"chart_type": "table", "title": "No rows", "overrides": []}
    nums = _numeric_columns(rows)
    labels = _label_columns(rows)
    metrics = [m for m in (metric_columns or []) if m in nums]
    y = metrics[0] if metrics else (nums[0] if nums else None)
    if not y:
        return {"chart_type": "table", "title": "Results", "overrides": []}

    pretty = y.replace("_", " ").title()
    if len(rows) == 1:
        return {"chart_type": "kpi", "y": y, "title": pretty, "overrides": []}

    tcol = _time_column(labels + nums)
    if tcol:
        return {"chart_type": "line", "x": tcol, "y": y,
                "series": labels[0] if labels and labels[0] != tcol else None,
                "title": f"{pretty} over time", "overrides": []}
    if labels:
        x = labels[0]
        return {"chart_type": "horizontal_bar" if len(rows) > MAX_SERIES else "bar",
                "x": x, "y": y,
                "title": f"{pretty} by {x.replace('_', ' ')}", "overrides": []}
    return {"chart_type": "table", "title": "Results", "overrides": []}


def validate(proposed: dict | None, rows: list[dict],
             metric_columns: list[str] | None = None) -> dict:
    """
    Honour the proposal where the real result allows it; otherwise derive one.

    Returns a plain dict so it can go straight into the JSON response; the
    `overrides` list says what was changed and why.
    """
    fallback = rule_chart(rows, metric_columns)
    if not proposed or not isinstance(proposed, dict):
        fallback["overrides"].append("no chart proposed; derived from the result shape")
        return fallback
    if not rows:
        return {"chart_type": "table", "title": proposed.get("title") or "No rows",
                "overrides": ["the query returned no rows"]}

    cols = set(rows[0].keys())
    nums = _numeric_columns(rows)
    overrides: list[str] = []
    kind = str(proposed.get("chart_type") or "").lower().strip()
    x, y = proposed.get("x"), proposed.get("series_x") or proposed.get("x")
    x = proposed.get("x")
    y = proposed.get("y")
    series = proposed.get("series")
    title = str(proposed.get("title") or fallback.get("title") or "Results")

    if kind not in KINDS:
        overrides.append(f"'{kind or 'none'}' is not a chart this UI draws")
        return {**fallback, "title": title, "overrides": overrides}

    # A y that is not a number in the result cannot be plotted, and an id column
    # is a number that means nothing on an axis.
    if kind != "table":
        if not y or y not in cols:
            overrides.append(f"y column '{y}' is not in the result")
            return {**fallback, "title": title, "overrides": overrides}
        if y not in nums:
            overrides.append(f"y column '{y}' is not numeric"
                             + (" (it is an identifier)" if is_id_column(y) else ""))
            return {**fallback, "title": title, "overrides": overrides}

    if kind == "kpi":
        if len(rows) > 1:
            overrides.append(f"a KPI needs one row; the result has {len(rows)}")
            return {**fallback, "title": title, "overrides": overrides}
        return {"chart_type": "kpi", "y": y, "title": title, "overrides": overrides}

    if kind in ("bar", "horizontal_bar", "pie", "line", "stacked_bar",
                "stacked_area_100"):
        if not x or x not in cols:
            overrides.append(f"x column '{x}' is not in the result")
            return {**fallback, "title": title, "overrides": overrides}
        if len(rows) == 1:
            overrides.append("a single row reads better as a KPI than as a chart")
            return {**fallback, "title": title, "overrides": overrides}
        # A vertical bar with many categories is unreadable; the same data
        # horizontal is fine, so this is a nudge rather than a rejection.
        if kind == "bar" and len(rows) > MAX_SERIES:
            kind = "horizontal_bar"
            overrides.append(f"{len(rows)} categories: switched to a horizontal bar")
        if series and series not in cols:
            overrides.append(f"series column '{series}' is not in the result")
            series = None

    return {"chart_type": kind, "x": x, "y": y, "series": series,
            "title": title, "overrides": overrides}
