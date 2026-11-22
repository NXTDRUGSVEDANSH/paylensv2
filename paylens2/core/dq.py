"""
Data-quality checks, run against the window an answer actually used.

WHY THE ANSWER CARRIES THEM
---------------------------
A number with a problem nobody mentioned is worse than no number. The planted
faults in this dataset are real analysis hazards: ~2% of attempts carry no
issuer, so an issuer breakdown genuinely will not sum to the total, and the
last two days are part-ingested, so the right-hand edge of a trend slopes down
for a reason that has nothing to do with payments.

EVERY PROBE GOES THROUGH THE GUARD
----------------------------------
A background query is the easiest place to leak rows a role may not see — it
is not user-initiated, so it never looks like the thing to check. These run
through `guard()` with the caller's own principal, exactly like the query they
are reporting on.

WHAT IS DIFFERENT FROM v1
-------------------------
v1 counted duplicates in `fact_transactions`. v2's allowlist blocks the raw
tables and that is not relaxed for a probe, so the duplicate check verifies
the GUARANTEE on the view instead: no txn_id may appear twice. It fails loudly
if de-duplication ever breaks, which is the thing actually worth knowing.

v1 also TRIMMED a part-ingested window and re-planned. v2 does not. The query
is already authored, and silently rewriting a model's query — then reporting
numbers from a window the user never asked for — is the kind of invisible edit
this pipeline avoids everywhere else. It reports the caveat and leaves the
query alone.
"""
from __future__ import annotations

import functools
import logging
import re
import statistics
from typing import Any, Callable

import yaml

from backend.core.executor import execute
from backend.core.guard import GuardBlocked, guard
from paylens2 import config

log = logging.getLogger("paylens2.dq")

_DATE = re.compile(r"DATE\s*'(\d{4}-\d{2}-\d{2})'|'(\d{4}-\d{2}-\d{2})'", re.I)
SEVERITY_ORDER = {"warn": 0, "info": 1, "pass": 2}


@functools.lru_cache(maxsize=1)
def _cfg() -> dict:
    """Thresholds and wording live in views.yaml, reviewed next to the model."""
    raw = yaml.safe_load(config.VIEWS_YAML.read_text(encoding="utf-8")) or {}
    return {"checks": raw.get("data_quality", {}) or {},
            "reconciliation": raw.get("reconciliation", {}) or {}}


def window_of(sql: str) -> tuple[str, str] | None:
    """
    The window the answer used, read off its own SQL.

    Taken from the query rather than from the question because the query is
    what produced the numbers — if the model chose a different window from the
    one the user had in mind, the checks should describe the data that was
    actually read.
    """
    found = sorted({a or b for a, b in _DATE.findall(sql or "")})
    return (found[0], found[-1]) if len(found) >= 2 else None


def _runner(p, model) -> Callable[[str], list[dict]]:
    def run(sql: str) -> list[dict]:
        g = guard(sql, p, model, run_explain=False)
        return execute(g.sql, timeout_s=config.SQL_TIMEOUT_S).rows
    return run


def _where(win: tuple[str, str] | None, col: str = "txn_ts") -> str:
    if not win:
        return "1=1"
    return f"{col} >= DATE '{win[0]}' AND {col} < DATE '{win[1]}'"


def _result(cid: str, status: str, message: str, **evidence) -> dict:
    return {"id": cid, "status": status,
            "severity": _cfg()["checks"].get(cid, {}).get("severity", "info"),
            "message": " ".join(message.split()), "evidence": evidence}


def _fmt(cid: str, field: str, **kw) -> str:
    tpl = str(_cfg()["checks"].get(cid, {}).get(field, "") or "")
    try:
        return tpl.format(**kw)
    except (KeyError, IndexError):
        return tpl


# ── the checks ──────────────────────────────────────────────────────────────
def window_probe(run, win, columns: list[str]) -> list[dict]:
    """
    Duplicates, null dimensions and negative amounts, in ONE query.

    They were three, and each cost ~275ms — not because the arithmetic is
    hard but because `v_attempts` re-runs its de-duplication window function
    on every scan. Three probes meant three materialisations of 775k rows for
    four numbers. Folded into one aggregate over one scan.

    The duplicate count is `count(*) - count(DISTINCT txn_id)`, which gives
    the number of surplus rows directly and needs no GROUP BY at all.

    Column names are interpolated, so they are filtered against the real view
    schema by the caller first and re-checked by guard c5 after.
    """
    cols = [c for c in columns[:3] if c in _VIEW_COLUMNS()]
    nulls = "".join(f", count(*) FILTER (WHERE {c} IS NULL) AS null_{c}"
                    for c in cols)
    rows = run(f"SELECT count(*) AS total, "
               f"count(*) - count(DISTINCT txn_id) AS dup_extra, "
               f"count(*) FILTER (WHERE amount < 0) AS negatives{nulls} "
               f"FROM v_attempts WHERE {_where(win)}")
    r = rows[0] if rows else {}
    total = int(r.get("total") or 0)
    out: list[dict] = []

    dup = int(r.get("dup_extra") or 0)
    out.append(_result("duplicate_txn_ids", "warn",
                       _fmt("duplicate_txn_ids", "message_fail", n=f"{dup:,}"),
                       surplus_rows=dup)
               if dup else
               _result("duplicate_txn_ids", "pass",
                       _fmt("duplicate_txn_ids", "message_pass")))

    thresh = float(_cfg()["checks"].get("null_issuer", {}).get("threshold", 0.005))
    for c in cols:
        n = int(r.get(f"null_{c}") or 0)
        if not total:
            continue
        frac = n / total
        out.append(_result("null_issuer", "warn",
                           _fmt("null_issuer", "message_fail",
                                pct=f"{frac * 100:.2f}%", column=c),
                           column=c, null_rows=n, total_rows=total,
                           null_pct=round(frac * 100, 2))
                   if frac >= thresh else
                   _result("null_issuer", "pass",
                           f"{c} is recorded on {(1 - frac) * 100:.2f}% of "
                           f"attempts in this window.",
                           column=c, null_pct=round(frac * 100, 2)))

    neg = int(r.get("negatives") or 0)
    out.append(_result("negative_amount", "info",
                       _fmt("negative_amount", "message_fail", n=f"{neg:,}"), rows=neg)
               if neg else
               _result("negative_amount", "pass",
                       "No negative amounts in this window."))
    return out


def check_partial_days(run, win) -> dict:
    """
    The right-hand edge of a trend slopes down because ingest is incomplete,
    not because payments fell. Reported, never silently trimmed.
    """
    cfg = _cfg()["checks"].get("partial_days", {})
    ratio = float(cfg.get("threshold", 0.85))
    lag = 2
    try:
        lag = int(_model_lag())
    except Exception:  # noqa: BLE001
        pass
    rows = _daily_histogram(run)
    if len(rows) < 10 or lag <= 0:
        return _result("partial_days", "pass",
                       "Not enough daily history to judge ingest completeness.")
    counts = [int(r["n"]) for r in rows]
    typical = statistics.median(counts[:-lag])
    recent = counts[-lag:]
    avg = sum(recent) / len(recent)
    edge = str(rows[-1]["d"])
    # Only a window that actually reaches into the part-ingested tail is affected.
    if win and win[1] <= str(rows[-lag]["d"]):
        return _result("partial_days", "pass",
                       "This window ends before the part-ingested days, so volume "
                       "is complete.", window_end=win[1])
    if typical and avg < ratio * typical:
        return _result("partial_days", "warn",
                       _fmt("partial_days", "message_fail", days=lag,
                            pct=f"{avg / typical * 100:.0f}%"),
                       typical_daily_rows=int(typical),
                       recent_daily_avg=int(avg), last_day=edge)
    return _result("partial_days", "pass",
                   "Recent days are at normal volume; nothing looks part-ingested.")


_HIST: dict[str, list[dict]] = {}


def _daily_histogram(run) -> list[dict]:
    """
    Daily volume, memoised against the data watermark.

    This is the only probe that scans the whole table rather than one window,
    and it was most of the cost of the check round — a full second on an
    answer that otherwise takes four. The shape of the histogram changes only
    when the data does, so it is keyed on the ingest high-water mark and
    recomputed when that moves.
    """
    try:
        from backend.core.db import data_watermark
        wm = data_watermark()
    except Exception:  # noqa: BLE001
        wm = "unknown"
    if wm not in _HIST:
        _HIST.clear()
        _HIST[wm] = run("SELECT txn_ts::DATE AS d, count(*) AS n FROM v_attempts "
                        "GROUP BY 1 ORDER BY 1")
    return _HIST[wm]


def _model_lag() -> int:
    from paylens2.core.model import get_model
    ds = (getattr(get_model(), "datasets", {}) or {}).get("v_attempts")
    return int(getattr(ds, "freshness_lag_days", 2) or 2)


def check_negative_amounts(run, win) -> dict:
    rows = run(f"SELECT count(*) AS n FROM v_attempts "
               f"WHERE {_where(win)} AND amount < 0")
    n = int((rows[0] if rows else {}).get("n") or 0)
    if n:
        return _result("negative_amount", "info",
                       _fmt("negative_amount", "message_fail", n=f"{n:,}"), rows=n)
    return _result("negative_amount", "pass",
                   "No negative amounts in this window.")


def reconcile(run, win) -> dict | None:
    """
    Two sources, one metric.

    States the documented precedence and quantifies the gap. It never averages
    the two and never quietly serves the larger — a reader who is handed one
    number is entitled to know the other existed and disagreed.
    """
    rc = _cfg()["reconciliation"]
    if not rc:
        return None
    tol = float(rc.get("tolerance", 0.02))
    rows = run(f"""
        WITH t AS (SELECT merchant_id, count(*) AS n FROM v_attempts
                   WHERE {_where(win)} AND is_success GROUP BY 1),
             s AS (SELECT merchant_id, sum(settled_txn_count) AS n
                   FROM v_settlements
                   WHERE {_where(win, 'settlement_date')} GROUP BY 1)
        SELECT t.merchant_id AS merchant_id, t.n AS txn_n, s.n AS settled_n,
               round(100.0 * (t.n - s.n) / nullif(t.n, 0), 2) AS gap_pct
        FROM t JOIN s ON s.merchant_id = t.merchant_id
        WHERE abs(t.n - s.n) > {tol} * t.n
        ORDER BY abs(t.n - s.n) DESC LIMIT 5""")
    if not rows:
        return {"id": "settlement_mismatch", "status": "pass", "severity":
                rc.get("severity", "warn"),
                "message": "Transactions and settlements agree on attempt counts "
                           "for this window.", "evidence": {}}
    worst = rows[0]
    msg = str(rc.get("message_fail", "")).format(
        n=len(rows), worst_id=worst.get("merchant_id"),
        worst_pct=f"{worst.get('gap_pct')}%", precedence=rc.get("precedence"))
    return {"id": "settlement_mismatch", "status": "warn",
            "severity": rc.get("severity", "warn"),
            "message": " ".join(msg.split()) + " "
                       + " ".join(str(rc.get("reason", "")).split()),
            "evidence": {"affected": [r.get("merchant_id") for r in rows],
                         "worst": worst, "precedence": rc.get("precedence")}}


# ── the entry point ─────────────────────────────────────────────────────────
def run_checks(sql_executed: str, columns: list[str], *, p, model,
               with_settlements: bool = False) -> list[dict]:
    """
    Every check, each one isolated.

    A probe that throws is logged and skipped: a data-quality check failing is
    not a reason to fail the answer it was describing.
    """
    if not config.USE_DQ:
        return []
    run = _runner(p, model)
    win = window_of(sql_executed)
    dims = [c for c in columns if c in _VIEW_COLUMNS()]
    out: list[dict] = []

    jobs: list[tuple[str, Any]] = [
        ("window_probe", lambda: window_probe(run, win, dims)),
        ("partial_days", lambda: [check_partial_days(run, win)]),
    ]
    if with_settlements:
        jobs.append(("settlement_mismatch",
                     lambda: [r for r in [reconcile(run, win)] if r]))

    for name, fn in jobs:
        try:
            out.extend(fn())
        except (GuardBlocked, PermissionError) as exc:
            log.warning("DQ probe %s was blocked by the guard: %s", name, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("DQ probe %s failed: %s", name, exc)
    out.sort(key=lambda c: SEVERITY_ORDER.get(
        c["severity"] if c["status"] != "pass" else "pass", 3))
    return out


@functools.lru_cache(maxsize=1)
def _VIEW_COLUMNS() -> frozenset[str]:
    from backend.core.db import table_columns
    return frozenset(table_columns().get("v_attempts", frozenset()))
