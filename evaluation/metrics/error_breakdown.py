"""
Error classification, re-aimed at how v2 actually fails.

WHY THIS IS NOT THE REFERENCE VERSION
-------------------------------------
The harness this ports from classified errors by regex over a database driver's
message, because that was the only signal it had. v2 carries the classification
on the answer itself: `failure_kind` already separates the three cases that
matter, and `attempts[].stage` names the exact check that rejected a query.
Reading those is both cheaper and more accurate than guessing from a string.

The three kinds are the point of the taxonomy:

    data    the concept genuinely is not in the schema — an honest refusal
    system  WE failed: a query would not parse, build, validate or run
    config  the deployment is wrong: views missing, no model, absent claim

A parse error reported as "the field is not here" tells the user something
false about their own data, so these are never collapsed into one bucket.
Within `system`, the rejecting stage is kept, because "check 10 rejected the
metric formula" and "the guard blocked a raw table" are different defects with
different fixes.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluation.pipelines.base import EvalResult

# Canonical categories.
REFUSED_DATA = "refused_data_absent"        # honest: concept not in the schema
CHECK10 = "check10_rejected"                # our SQL failed the v2 backstop
GUARD = "guard_blocked"                     # our SQL failed one of the nine
PARSE = "sql_parse_error"                   # would not parse at all
EXECUTE = "execute_error"                   # ran and threw
TIMEOUT = "timeout"
ROW_CAP = "row_cap_blocked"                 # aggregate over the cap, blocked
NO_MODEL = "config_no_model"
CONFIG = "config_other"
EMPTY = "empty_result"                      # ran fine, zero rows
OTHER = "other"

_STAGE_PATTERNS = (
    (re.compile(r"^c10", re.I), CHECK10),
    (re.compile(r"^c[1-9](_|$)", re.I), GUARD),
    (re.compile(r"guard", re.I), GUARD),
)


def classify(res: "EvalResult") -> str:
    """One category per result. Reads the answer's own fields, not a message."""
    detail = f"{res.error or ''} {getattr(res, 'failure_detail', '') or ''}"
    kind = getattr(res, "failure_kind", "") or ""

    if res.success:
        return EMPTY if not (res.rows or []) else OTHER

    if kind == "data":
        return REFUSED_DATA

    if kind == "config":
        return NO_MODEL if re.search(r"language model|LLM_API_KEY", detail, re.I) \
            else CONFIG

    # kind == "system" (or unset): name the stage that rejected it.
    if re.search(r"row cap|more than .* groups|truncat", detail, re.I):
        return ROW_CAP
    if re.search(r"timed out|timeout|QueryTimeout", detail, re.I):
        return TIMEOUT

    for attempt in reversed(getattr(res, "attempts", []) or []):
        if attempt.get("ok"):
            continue
        stage = str(attempt.get("stage") or "")
        if re.search(r"c10b_parse|c1_parse|does not parse", stage + detail, re.I):
            return PARSE
        for pattern, label in _STAGE_PATTERNS:
            if pattern.search(stage):
                return label

    if re.search(r"does not parse|syntax|unexpected token", detail, re.I):
        return PARSE
    if re.search(r"failed to run|Error:", detail, re.I):
        return EXECUTE
    return OTHER


def compute_error_breakdown(results: list["EvalResult"]) -> dict:
    """
    Counts per category, plus the two rates worth reporting separately.

    `error_rate` counts every unsuccessful result. `our_fault_rate` excludes
    the honest refusals, because a correct "this dataset holds no chargebacks"
    is a success of the system and counting it as an error would reward a
    pipeline that invents an answer instead.
    """
    n_total = len(results)
    failed = [r for r in results if not r.success]

    counts: dict[str, int] = {}
    for r in failed:
        label = classify(r)
        counts[label] = counts.get(label, 0) + 1

    honest = counts.get(REFUSED_DATA, 0)
    our_fault = len(failed) - honest

    return {
        **counts,
        "total_errors": len(failed),
        "error_rate": round(len(failed) / n_total, 4) if n_total else 0.0,
        "honest_refusals": honest,
        "our_fault": our_fault,
        "our_fault_rate": round(our_fault / n_total, 4) if n_total else 0.0,
    }
