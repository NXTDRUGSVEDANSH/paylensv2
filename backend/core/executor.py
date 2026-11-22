"""
Query execution: timeout, row-cap adjudication, serialisation.

Row cap: the guard attaches a sentinel LIMIT of MAX_SQL_ROWS + 1, and THIS module
decides what that means. EXPLAIN is a cost control, not a row-cap control.

  intent == detail   -> drop the sentinel row, report "showing N of TOTAL"
  otherwise          -> BLOCK

Blocking aggregates is not cosmetic. A truncated GROUP BY makes the segment weights
stop summing to 1, so drivers.decompose() returns a mix and rate effect that do not
sum to the observed delta. Returning a wrong decomposition is worse than returning a
refusal in a tool whose whole value proposition is auditability.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from dataclasses import dataclass, field
from decimal import Decimal

from backend import config
from backend.core import db
import sqlglot
from sqlglot import exp


def to_count_query(bound_sql: str) -> str:
    """
    Wrap a bound query to count its rows, for "showing 5,000 of N".

    Strips ORDER BY / LIMIT, which are meaningless under a count and would
    otherwise cap the count at the sentinel.

    Lived in the v1 compiler, which this build no longer ships. It is the only
    thing the executor ever needed from it, and it is pure SQL rewriting with
    no dependency on how the query was produced — so it belongs next to the
    code that executes queries rather than next to the code that compiled them.
    """
    root = sqlglot.parse_one(bound_sql, read="duckdb")
    target = root.this if isinstance(root, exp.With) else root
    for key in ("order", "limit", "offset"):
        if isinstance(target, exp.Select):
            target.set(key, None)
    return (f"SELECT count(*) AS total_rows FROM (\n"
            f"{root.sql(dialect='duckdb', pretty=True)}\n) _cnt")

log = logging.getLogger("paylens.executor")


class QueryTimeout(RuntimeError):
    pass


class RowCapExceeded(RuntimeError):
    def __init__(self, cap: int, total: int | None):
        self.cap, self.total = cap, total
        hint = f"about {total:,}" if total else f"more than {cap:,}"
        super().__init__(
            f"This analysis would return {hint} groups, over the {cap:,}-row cap. "
            "Narrow the time window, add a filter, or drop a dimension. "
            "(A truncated aggregate would give you a wrong number, so it is refused "
            "rather than cut short.)")


@dataclass
class ExecResult:
    rows: list[dict]
    exec_ms: int
    truncated: bool = False
    total_rows: int | None = None
    notes: list[str] = field(default_factory=list)


def _scalarise(v):
    if v is None:
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def serialize(rows: list[dict]) -> list[dict]:
    return [{k: _scalarise(v) for k, v in r.items()} for r in rows]


def execute(sql: str, *, timeout_s: int | None = None) -> ExecResult:
    """
    Run a query with a hard wall-clock stop.

    DuckDB has no statement_timeout, so a watchdog calls interrupt() on the
    connection. Without this, one pathological query hangs a request forever.
    """
    timeout_s = config.SQL_TIMEOUT_S if timeout_s is None else timeout_s
    cur = db.cursor()
    fired = threading.Event()

    def _kill():
        fired.set()
        try:
            cur.interrupt()
        except Exception:  # noqa: BLE001
            pass

    t = threading.Timer(timeout_s, _kill)
    t.start()
    t0 = dt.datetime.now()
    try:
        c = cur.execute(sql)
        cols = [d[0] for d in c.description]
        raw = c.fetchall()
    except Exception as exc:  # noqa: BLE001
        if fired.is_set():
            raise QueryTimeout(
                f"query exceeded the {timeout_s}s limit and was cancelled") from exc
        raise
    finally:
        t.cancel()
    ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
    return ExecResult(rows=serialize([dict(zip(cols, r)) for r in raw]), exec_ms=ms)


def apply_row_cap(result: ExecResult, *, bound_sql: str, is_aggregate: bool,
                  max_rows: int | None = None,
                  count_runner=None) -> ExecResult:
    """
    Adjudicate the sentinel. `count_runner(sql) -> int` must route the count query
    back through the guard — invariant 1 has no exceptions, including for counts.
    """
    cap = config.MAX_SQL_ROWS if max_rows is None else max_rows
    if len(result.rows) <= cap:
        return result

    total: int | None = None
    if count_runner is not None:
        try:
            total = count_runner(to_count_query(bound_sql))
        except Exception as exc:  # noqa: BLE001
            log.warning("row-count query failed: %s", exc)

    if is_aggregate:
        raise RowCapExceeded(cap, total)

    result.rows = result.rows[:cap]
    result.truncated = True
    result.total_rows = total
    result.notes.append(
        f"Showing {cap:,} of {f'{total:,}' if total else 'many'} rows. "
        "This is a raw-detail listing, so the sample is representative but incomplete.")
    return result
