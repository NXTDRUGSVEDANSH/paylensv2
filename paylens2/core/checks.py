"""
Check 10 — the v2 backstop, run BEFORE the existing nine.

The views make the two dangerous mistakes (forgetting de-duplication, writing a
wrong join) structurally impossible. They do not make a wrong METRIC FORMULA
impossible: `sum(is_success) / count(*)` is syntactically fine, reads naturally,
and silently includes PENDING in the denominator. That is a different number from
the one the classic pipeline reports for the same question, and two paths
disagreeing is worse than either being slow.

So: if the query aliases a column with a known metric id, the official expression
for that metric must appear in the query. Normalised for whitespace and case, not
parsed — the expression is pinned in schema/views.yaml and echoed verbatim in the
schema card, so reproducing it is the easy path.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from paylens2.core.model import get_model, metric_expressions

DIALECT = "duckdb"


class CheckFailed(Exception):
    def __init__(self, check: str, reason: str, hint: str = ""):
        self.check, self.reason, self.hint = check, reason, hint
        super().__init__(f"[{check}] {reason}")


# Columns the generator only ever sets on a FAILED attempt.
FAILURE_ONLY = frozenset({"failure_code", "failure_category", "decline_type",
                          "failure_owner", "journey_step_label", "is_retriable",
                          "failure_description"})

_RAW = re.compile(r"\b(fact_transactions|fact_settlements|dim_merchant|dim_bank|"
                  r"dim_payment_method|dim_terminal|dim_failure_code)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).replace("( ", "(").replace(" )", ")")


def check_v2(sql: str, *, dialect: str = DIALECT,
             skip: frozenset[str] = frozenset()) -> list[str]:
    """
    Returns the list of checks passed. Raises CheckFailed on the first failure.

    Deliberately raises rather than returning a verdict: every caller must handle
    the failure, and the heal loop needs the reason text to feed back to the model.

    `dialect` and `skip` exist for the evaluation harness and default to
    today's behaviour exactly, so PayLens itself is unaffected.

    They are needed because benchmarking this check set against an external
    corpus means pointing it at foreign schemas. Two of the checks are
    PayLens-specific in a way that is invisible until you do:

      - the parse runs in the duckdb dialect, and a SQLite corpus is full of
        identifiers like `Free Meal Count (K-12)` that duckdb cannot parse.
        Left alone, every one of those is recorded as the model writing a
        broken query when the query was fine and the parser was wrong.
      - 10c rejects `SELECT *`, which is right here (60 columns into a prompt
        is waste) and wrong for a benchmark whose reference answers use it.

    Nothing is relaxed for PayLens's own traffic: `skip` is empty by default,
    and a test asserts the defaults are unchanged.
    """
    passed: list[str] = []

    # 10a — views only. The guard's table allowlist enforces this too, but failing
    # here produces a far better message for the heal prompt than "table not in
    # the semantic model".
    raw = None if "c10a_views_only" in skip else _RAW.search(sql or "")
    if raw:
        raise CheckFailed(
            "c10a_views_only",
            f"the query reads '{raw.group(0)}' directly",
            "Query v_attempts instead. It is already de-duplicated and already "
            "joined to every dimension, so no JOIN is needed.")
    passed.append("c10a_views_only")

    try:
        root = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        raise CheckFailed("c10b_parse", f"query does not parse: {exc}") from exc
    passed.append("c10b_parse")

    # 10c — SELECT * on the wide view. 60 columns into a synthesis prompt is
    # waste, and a model that selects everything has not decided what it needs.
    for star in (() if "c10c_no_select_star" in skip
                 else root.find_all(exp.Star)):
        parent = star.parent
        if isinstance(parent, exp.Select):
            raise CheckFailed(
                "c10c_no_select_star",
                "SELECT * on a wide view returns far more columns than any answer needs",
                "Name the columns you actually need.")
    passed.append("c10c_no_select_star")

    # 10d — metric formula fidelity.
    exprs = {} if "c10d_metric_formula" in skip else metric_expressions()
    model = get_model() if exprs else None
    flat = _norm(sql)
    for alias in root.find_all(exp.Alias):
        mid = (alias.alias or "").lower()
        if mid not in exprs:
            continue
        official = exprs[mid]
        met = model.metrics[mid]
        # A ratio is pinned on its numerator AND denominator; a sum or count on
        # its single expression. Checking the assembled string would reject a
        # model that wrote an equivalent nullif() spelling, which is pedantry
        # rather than safety.
        parts = ([met.numerator, met.denominator] if met.is_ratio else [met.expr])
        missing = [p for p in parts if p and _norm(p) not in flat]
        if missing:
            raise CheckFailed(
                "c10d_metric_formula",
                f"column aliased '{mid}' does not use the official definition "
                f"(missing: {'; '.join(missing)})",
                f"The definition of {mid} is exactly:\n    {official}\n"
                f"Use it verbatim, or alias the column something other than a "
                f"metric id if you mean a different quantity.")
    passed.append("c10d_metric_formula")

    # 10e — a rate metric grouped by a failure-only column.
    #
    # Observed in a live answer: "the 'customer' failure owner has a 0% success
    # rate on 19,784 attempts". Every row carrying a failure_owner is a failure,
    # so the group's success rate is zero by construction. The number is
    # correct and the sentence is nonsense, which is the most dangerous
    # combination this system can produce — it reads as a finding.
    rate_aliases = set() if "c10e_failure_only_grouping" in skip else \
        {a.alias.lower() for a in root.find_all(exp.Alias)
         if (a.alias or "").lower() in ("success_rate", "failure_rate")}
    if rate_aliases:
        grouped = {(c.name or "").lower()
                   for g in root.find_all(exp.Group) for c in g.find_all(exp.Column)}
        # A positional GROUP BY (GROUP BY 1) names no column, so also look at
        # the projection: the label column of a one-dimension breakdown.
        if not grouped:
            grouped = {(c.name or "").lower()
                       for sel in root.find_all(exp.Select)
                       for c in sel.find_all(exp.Column)} & FAILURE_ONLY
        bad = sorted(grouped & FAILURE_ONLY)
        if bad:
            raise CheckFailed(
                "c10e_failure_only_grouping",
                f"a rate metric is grouped by {', '.join(bad)}, which is only "
                f"populated on failed attempts",
                "Every such group is 0% or 100% by construction, so the result "
                "reads as a finding when it is an artefact. Either COUNT the "
                "failures by that column — count(*) filter (where is_failed) — "
                "or group the rate by something present on every attempt "
                "(payment_method, issuer_bank, entry_mode, region).")
    passed.append("c10e_failure_only_grouping")
    return passed


def is_aggregate(sql: str, *, dialect: str = DIALECT) -> bool:
    """
    Does this query aggregate?

    Decides whether an over-cap result is BLOCKED or truncated. A truncated
    aggregate is a wrong number presented as a complete one; a truncated detail
    listing is an honest sample. Erring toward True is the safe direction.
    """
    try:
        root = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return True
    if list(root.find_all(exp.Group)):
        return True
    aggs = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max, exp.AggFunc)
    return any(isinstance(n, aggs) for n in root.walk())
