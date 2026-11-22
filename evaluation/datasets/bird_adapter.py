"""
Pointing the real guard at a foreign schema.

THE PROBLEM
-----------
`guard()` is coupled to the PayLens warehouse in two places: `_c5_columns`
calls `db.table_columns()` / `db.all_columns()` for its column allowlist, and
`_c8_explain` calls `db.cursor()` to plan the query. Both are module-level
lookups into `backend.core.db`. To run the same nine checks over a BIRD
database, the guard has to see that database's catalog.

THE APPROACH
------------
Materialise the BIRD SQLite file into a fresh in-memory DuckDB, then rebind
`backend.core.db`'s entry points to it for the duration of one query.

Materialise rather than ATTACH: DuckDB's sqlite extension works and was
verified, but it needs a network install on first use and the real warehouse
connection is read-only, so it could not be installed there anyway. Copying
tables through `sqlite3` costs a second per database and removes the
dependency.

THREAD-LOCAL, NOT GLOBAL
------------------------
The rebind is a module attribute swap and is therefore process-wide. With N
workers on N different databases they would see each other's catalog and the
column allowlist would validate against the wrong schema — which fails in the
most confusing possible way, by passing. So every rebound entry point resolves
through a thread-local, and a thread with nothing bound falls through to the
real warehouse untouched.

WHAT THIS DOES *NOT* RELAX
--------------------------
All nine guard checks run. RBAC is off because a BIRD database has no tenant
to scope to — the generated model declares `row_scope: null`, which makes
checks 6 and 9 pass with nothing to inject rather than being skipped. Check
10's two PayLens-specific members are skipped by name and the skip is
recorded, so the report can state what it cost.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path

import duckdb

from evaluation.datasets import bird_loader

_TLS = threading.local()
_CACHE: dict[tuple[int, str], duckdb.DuckDBPyConnection] = {}
_CACHE_LOCK = threading.Lock()

# The two check-10 members that are PayLens-specific, plus the two that are
# inert anyway. Named explicitly so the report can say which were not applied.
BIRD_SKIP = frozenset({
    "c10a_views_only",          # matches PayLens table names only; a no-op here
    "c10c_no_select_star",      # right for a 60-column view, wrong for BIRD
    "c10d_metric_formula",      # the generated model declares no metrics
    "c10e_failure_only_grouping",   # nothing aliases success_rate here
})
BIRD_DIALECT = "sqlite"


def materialise(db_id: str) -> duckdb.DuckDBPyConnection:
    """
    BIRD's SQLite tables, copied into a private in-memory DuckDB.

    Cached per (thread, db_id): the copy is the expensive part and a thread
    works through many questions against the same database.
    """
    key = (threading.get_ident(), db_id)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]

    path = bird_loader.db_path(db_id)
    if path is None:
        raise FileNotFoundError(f"no database for {db_id!r}")

    # Types must survive the copy. The first version created every column as
    # VARCHAR, and DuckDB then refused `"Free Meal Count (K-12)" > 100` with a
    # binder error — "Cannot compare VARCHAR and INTEGER_LITERAL". Every BIRD
    # question with a numeric predicate would have been recorded as a failed
    # query when the query was correct, which would have understated EX across
    # the whole track. pandas carries the types across.
    import pandas as pd

    src = sqlite3.connect(str(path))
    con = duckdb.connect(":memory:")
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        if t.startswith("sqlite_"):
            continue
        frame = pd.read_sql_query(f'SELECT * FROM "{t}"', src)  # noqa: F841
        con.register("_src_frame", frame)
        con.execute(f'CREATE TABLE "{t}" AS SELECT * FROM _src_frame')
        con.unregister("_src_frame")
    src.close()
    with _CACHE_LOCK:
        _CACHE[key] = con
    return con


@contextlib.contextmanager
def bound(db_id: str):
    """Rebind backend.core.db to this BIRD database, for this thread only."""
    from backend.core import db as dbmod

    con = materialise(db_id)
    _TLS.con = con
    _install(dbmod)
    try:
        yield con
    finally:
        _TLS.con = None


_INSTALLED = False
_ORIG: dict = {}


def _install(dbmod) -> None:
    """
    Wrap db's entry points once, resolving through the thread-local.

    Wrapping rather than replacing: a thread that has not called `bound()` —
    the own-query track running in the same process, say — gets the original
    function and the real warehouse, unchanged.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _ORIG.update(cursor=dbmod.cursor, table_columns=dbmod.table_columns,
                 all_columns=dbmod.all_columns, fetch_dicts=dbmod.fetch_dicts)

    def cursor():
        con = getattr(_TLS, "con", None)
        return con.cursor() if con is not None else _ORIG["cursor"]()

    def table_columns():
        con = getattr(_TLS, "con", None)
        if con is None:
            return _ORIG["table_columns"]()
        rows = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
        ).fetchall()
        out: dict[str, set] = {}
        for t, c in rows:
            out.setdefault(t.lower(), set()).add(c.lower())
        return {t: frozenset(c) for t, c in out.items()}

    def all_columns():
        con = getattr(_TLS, "con", None)
        if con is None:
            return _ORIG["all_columns"]()
        return frozenset(c for cols in table_columns().values() for c in cols)

    def fetch_dicts(sql, params=None):
        con = getattr(_TLS, "con", None)
        if con is None:
            return _ORIG["fetch_dicts"](sql, params)
        cur = con.cursor().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    dbmod.cursor = cursor
    dbmod.table_columns = table_columns
    dbmod.all_columns = all_columns
    dbmod.fetch_dicts = fetch_dicts
    _INSTALLED = True


# ─────────────────────────────────────────────────────────────────────────────
def model_for(db_id: str):
    """
    A SemanticModel for a BIRD database, built from its own catalog.

    Loaded through the project's real `semantic.loader`, so the object handed
    to `guard()` is the same dataclass production uses — not a stand-in that
    might diverge in a way the benchmark would not notice.

    Three deliberate choices:
      allowlist.tables = every real table, so check 4 passes on a table that
                         exists and still blocks one the model invented
      metrics = {}      makes check 10d inert by DATA rather than by a code
                         branch: `metric_expressions()` is empty, so its
                         `if mid not in exprs: continue` never fires
      row_scope = null   RBAC off. A BIRD database has no tenant column, so
                         checks 6 and 9 pass with nothing to inject — which is
                         different from being skipped, and is the honest state
    """
    import tempfile

    import yaml

    from semantic.loader import load

    con = materialise(db_id)
    rows = con.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
    ).fetchall()
    tables: dict[str, list[str]] = {}
    for t, c in rows:
        tables.setdefault(t, []).append(c)
    if not tables:
        raise RuntimeError(f"{db_id} materialised with no tables")

    denied = _denied_functions()
    first = sorted(tables)[0]
    spec = {
        "version": f"bird-{db_id}",
        "datasets": {t: {"label": t, "description": f"BIRD table {t}",
                         "grain": "row", "primary_key": cols[0],
                         "time_column": None}
                     for t, cols in tables.items()},
        # One nominal metric so the loader has something to validate; it is
        # never referenced by a BIRD query and 10d is skipped regardless.
        "metrics": {"row_count": {"label": "Rows", "type": "count",
                                  "base": first, "expr": "count(*)",
                                  "format": "number", "decimals": 0}},
        "dimensions": {},
        "joins": [],
        "allowlist": {"tables": sorted(tables), "functions_denied": denied},
        "rbac": {"roles": {"cxo": {"label": "Benchmark", "row_scope": None,
                                   "masked_columns": [], "denied_metrics": [],
                                   "persona": "Return the query only."}}},
        "intents": {"single_value": {"label": "A single number"}},
        "time_grains": {},
        # The loader reads these unconditionally. Empty is the honest value: a
        # BIRD database has no planted defects to probe and no second source to
        # reconcile against, and inventing probes for it would report data
        # quality on a dataset nobody claims anything about.
        "data_quality": {},
        "reconciliation": {},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as fh:
        yaml.safe_dump(spec, fh, sort_keys=False)
        tmp = Path(fh.name)
    try:
        return load(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def _denied_functions() -> list[str]:
    """
    The same denied-function list PayLens uses, read from its own YAML.

    Copied by reference rather than retyped: a benchmark that silently allowed
    `read_csv` while production blocked it would be measuring a different, more
    permissive system than the one being reported on.
    """
    import yaml

    from paylens2 import config as v2cfg
    raw = yaml.safe_load(v2cfg.VIEWS_YAML.read_text(encoding="utf-8")) or {}
    return list((raw.get("allowlist") or {}).get("functions_denied") or [])


def schema_card_for(db_id: str) -> str:
    """The prompt's schema section: this database's DDL and nothing else."""
    conn = bird_loader.gold_conn(db_id)
    try:
        ddl = bird_loader.schema_str(conn)
    finally:
        conn.close()
    return "\n".join([
        "# SCHEMA", "",
        "You may query these tables and nothing else.", "", ddl, "",
        "## RULES", "",
        "- SELECT only. One statement.",
        "- Quote identifiers containing spaces or punctuation with double "
        "quotes.",
        "- Do not add a LIMIT unless the question asks for a sample.",
        "- Return only the columns the question asks for.",
    ])
