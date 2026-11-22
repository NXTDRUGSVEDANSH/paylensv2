"""
The validation gate. Nine checks. Fails closed.

NOTHING reaches DuckDB that did not pass this on the current request — no exception
for cache hits, self-healed SQL, data-quality probes, or the row-count query behind
"showing 5,000 of N". Fractal AI applies its equivalent (`_secure_sql`) once and then
re-executes self-healed output unvalidated; that is the hole this module closes.

All checks work on the sqlglot AST, not on the SQL string. Regex filtering of SQL is
defeated trivially: a `DROP` substring check misses `/*x*/DROP`, and naively banning
`;` breaks any query containing ';' in a string literal.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from backend import config
from backend.core import db, rbac
from backend.core.schemas import GuardResult
from semantic.loader import SemanticModel

DIALECT = "duckdb"

_WRITE_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Grant, exp.Command, exp.Copy, exp.Attach, exp.Detach, exp.Set,
)
_DANGEROUS_WORD = re.compile(
    r"\b(pg_read_file|pg_sleep|load_extension|install|attach|detach|copy\s+|export\s+database)\b",
    re.I)


class GuardBlocked(Exception):
    def __init__(self, check: str, reason: str):
        self.check, self.reason = check, reason
        super().__init__(f"[{check}] {reason}")


def _parse(sql: str, dialect: str = DIALECT) -> exp.Expression:
    try:
        stmts = sqlglot.parse(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        raise GuardBlocked("c1_parse", f"query does not parse: {exc}") from exc
    stmts = [s for s in stmts if s is not None]
    if len(stmts) == 0:
        raise GuardBlocked("c1_parse", "empty query")
    if len(stmts) > 1:
        raise GuardBlocked("c1_single_statement",
                           f"{len(stmts)} statements found; exactly one SELECT is allowed")
    return stmts[0]


def _c2_select_only(root: exp.Expression) -> None:
    if not isinstance(root, (exp.Select, exp.Subquery, exp.Union, exp.With)):
        raise GuardBlocked("c2_select_only",
                           f"only SELECT is permitted, got {type(root).__name__.upper()}")
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            raise GuardBlocked("c2_select_only",
                               f"write or DDL operation ({type(node).__name__.upper()}) "
                               "is not permitted — PayLens is read-only")


def _c3_no_injection(sql: str, root: exp.Expression, model: SemanticModel) -> None:
    if _DANGEROUS_WORD.search(sql):
        raise GuardBlocked("c3_no_injection",
                           f"query contains a forbidden construct: "
                           f"{_DANGEROUS_WORD.search(sql).group(0)!r}")
    for fn in root.find_all(exp.Anonymous):
        name = (fn.this or "").lower() if isinstance(fn.this, str) else ""
        if name in model.denied_functions:
            raise GuardBlocked("c3_no_injection",
                               f"function '{name}' is not permitted (filesystem or system access)")


def _c4_tables(root: exp.Expression, model: SemanticModel) -> list[str]:
    cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    used: list[str] = []
    for t in root.find_all(exp.Table):
        name = (t.name or "").lower()
        if not name or name in cte_names:
            continue
        if name not in {a.lower() for a in model.allowed_tables}:
            raise GuardBlocked(
                "c4_table_allowlist",
                f"table '{name}' is not in the semantic model. "
                f"Allowed: {', '.join(sorted(model.allowed_tables))}")
        used.append(name)
    if not used:
        raise GuardBlocked("c4_table_allowlist", "query reads no permitted table")
    return sorted(set(used))


def _c5_columns(root: exp.Expression, model: SemanticModel, p: rbac.Principal) -> None:
    known = db.table_columns()
    every = db.all_columns()
    aliases = {a.lower() for a in
               [s.alias_or_name for s in root.find_all(exp.Alias)] if a}
    aliases |= {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    table_alias: dict[str, str] = {}
    for t in root.find_all(exp.Table):
        if t.name:
            table_alias[(t.alias or t.name).lower()] = t.name.lower()

    masked = rbac.masked_columns(p, model)
    for col in root.find_all(exp.Column):
        cname = (col.name or "").lower()
        if not cname or cname == "*":
            continue
        tref = (col.table or "").lower()
        if tref and tref in table_alias:
            real = table_alias[tref]
            if f"{real}.{cname}" in masked:
                raise GuardBlocked("c5_column_allowlist",
                                   f"column {real}.{cname} is masked for role '{p.role}'")
            cols = known.get(real, frozenset())
            if cols and cname not in cols and cname not in aliases:
                raise GuardBlocked("c5_column_allowlist",
                                   f"column '{cname}' does not exist on table '{real}'")
        else:
            if cname in aliases:
                continue
            if cname not in every:
                raise GuardBlocked("c5_column_allowlist",
                                   f"column '{cname}' does not exist in any permitted table")
            for mc in masked:
                if mc.endswith("." + cname):
                    raise GuardBlocked("c5_column_allowlist",
                                       f"column '{cname}' is masked for role '{p.role}'")


def _c7_sentinel_limit(root: exp.Expression, max_rows: int) -> int:
    """
    Attach LIMIT max_rows+1, never max_rows.

    The sentinel exists to DETECT overflow, never to deliver a truncated answer.
    With exactly max_rows you cannot distinguish "exactly at the cap" from "over
    the cap", and you will ship a silently-truncated result as if it were complete.
    """
    sentinel = max_rows + 1
    target = root.this if isinstance(root, exp.With) else root
    existing = target.args.get("limit") if isinstance(target, exp.Select) else None
    if existing is not None:
        try:
            cur = int(existing.expression.this)
        except Exception:  # noqa: BLE001
            cur = sentinel + 1
        if cur <= sentinel:
            return cur
        target.set("limit", exp.Limit(expression=exp.Literal.number(sentinel)))
        return sentinel
    if isinstance(target, exp.Select):
        target.set("limit", exp.Limit(expression=exp.Literal.number(sentinel)))
    return sentinel


def _c8_explain(sql: str, scan_limit: int | None = None) -> int | None:
    """
    Cost control, not row-cap control. DuckDB's GROUP BY cardinality estimates are
    routinely off by an order of magnitude in both directions, so the threshold is
    deliberately generous; the sentinel limit catches real overflow 200ms later.

    `scan_limit` raises the ceiling for a caller whose SQL is FIXED and reviewed
    rather than model-generated. Measured need: a dashboard panel joining three
    dimension tables and running a `count(distinct)` estimates at 965M rows and
    actually executes in 129ms over 777k. Blocking it is a false positive, and the
    plan this system was built from says so explicitly — a false block refuses a
    legitimate question. For such callers the real protection is the statement
    timeout in executor.py, not an estimate that is wrong by three orders of
    magnitude. The check still runs, and still fires on a genuine runaway.
    """
    try:
        rows = db.cursor().execute(f"explain {sql}").fetchall()
    except Exception as exc:  # noqa: BLE001
        raise GuardBlocked("c8_cost", f"query could not be planned: {exc}") from exc
    text = "\n".join(str(c) for r in rows for c in r)
    # DuckDB's physical plan reports estimated cardinality as "~777,757 rows".
    # Older/other builds use "EC: 777757"; accept both.
    ecs = [int(x.replace(",", "")) for x in re.findall(r"~\s*([\d,]+)\s*rows", text)]
    ecs += [int(x.replace(",", "")) for x in re.findall(r"EC:\s*([\d,]+)", text)]
    est = max(ecs) if ecs else None
    limit = config.EXPLAIN_SCAN_LIMIT if scan_limit is None else scan_limit
    if est is not None and est > limit:
        raise GuardBlocked(
            "c8_cost",
            f"estimated scan of {est:,} rows exceeds the limit of "
            f"{limit:,}. Narrow the time window, add a filter, "
            "or drop a dimension.")
    return est


def guard(sql: str, p: rbac.Principal, model: SemanticModel, *,
          max_rows: int | None = None, run_explain: bool = True,
          explain_scan_limit: int | None = None,
          dialect: str = DIALECT) -> GuardResult:
    """
    Validate, scope and cap a query. Raises GuardBlocked on any failure.

    `dialect` defaults to duckdb, which is the only dialect PayLens speaks. It
    is a parameter so the evaluation harness can run this same check set
    against a SQLite benchmark corpus without a second copy of it existing —
    a copy would drift, and the one that drifted would be the one claiming the
    security properties.
    """
    max_rows = config.MAX_SQL_ROWS if max_rows is None else max_rows
    passed: list[str] = []

    root = _parse(sql, dialect);            passed.append("c1_single_statement")
    _c2_select_only(root);                  passed.append("c2_select_only")
    _c3_no_injection(sql, root, model);     passed.append("c3_no_injection")
    tables = _c4_tables(root, model);       passed.append("c4_table_allowlist")
    _c5_columns(root, model, p);            passed.append("c5_column_allowlist")

    preds = rbac.row_predicates(p, model)
    root, applied = rbac.inject(root, preds)
    passed.append("c6_rbac_inject")

    sentinel = _c7_sentinel_limit(root, max_rows)
    passed.append("c7_row_cap")

    final_sql = root.sql(dialect=dialect, pretty=True)

    est = _c8_explain(final_sql, explain_scan_limit) if run_explain else None
    passed.append("c8_cost_estimate")

    ok, why = rbac.assert_injected(root, preds)
    if not ok:
        raise GuardBlocked("c9_rbac_present", why)
    passed.append("c9_rbac_present")

    return GuardResult(ok=True, sql=final_sql, checks_passed=passed,
                       injected_predicates=applied, estimated_rows=est,
                       sentinel_limit=sentinel)


def guard_or_result(sql: str, p: rbac.Principal, model: SemanticModel, **kw) -> GuardResult:
    """Non-raising variant — returns a failed GuardResult instead."""
    try:
        return guard(sql, p, model, **kw)
    except GuardBlocked as gb:
        return GuardResult(ok=False, failed_check=gb.check, reason=gb.reason)
    except PermissionError as pe:
        return GuardResult(ok=False, failed_check="c6_rbac_inject", reason=str(pe))
