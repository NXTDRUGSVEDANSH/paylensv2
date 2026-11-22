"""DuckDB connection + schema introspection (cached)."""
from __future__ import annotations

import functools
import threading
import time
from typing import Any

import duckdb
from pathlib import Path

from backend import config

_lock = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None


def connect() -> duckdb.DuckDBPyConnection:
    """One shared read-only connection; DuckDB cursors are created per query."""
    global _con
    with _lock:
        if _con is None:
            if not config.DUCKDB_PATH.exists():
                raise FileNotFoundError(
                    f"{config.DUCKDB_PATH} not found — run `python data/generate.py` first")
            Path(config.DUCKDB_TEMP_DIR).mkdir(parents=True, exist_ok=True)
            _con = duckdb.connect(
                str(config.DUCKDB_PATH), read_only=True,
                config={"memory_limit": config.DUCKDB_MEMORY_LIMIT,
                        # Read-only does not imply no scratch space. See
                        # config.DUCKDB_TEMP_DIR for why leaving this unset turns
                        # a tight memory budget into intermittent query failures.
                        "temp_directory": config.DUCKDB_TEMP_DIR})
        return _con


def cursor() -> duckdb.DuckDBPyConnection:
    return connect().cursor()


@functools.lru_cache(maxsize=1)
def table_columns() -> dict[str, frozenset[str]]:
    """{table: {column, ...}} for the real database — the guard's column allowlist."""
    rows = cursor().execute("""
        select table_name, column_name
        from information_schema.columns
        where table_schema = 'main'
    """).fetchall()
    out: dict[str, set[str]] = {}
    for t, c in rows:
        out.setdefault(t, set()).add(c)
    return {t: frozenset(c) for t, c in out.items()}


@functools.lru_cache(maxsize=1)
def all_columns() -> frozenset[str]:
    return frozenset(c for cols in table_columns().values() for c in cols)


_watermark: tuple[float, str] | None = None
_WATERMARK_TTL = 60


def data_watermark() -> str:
    """
    Identifies the data state for the answer cache. Uses the ingest high-water mark,
    not file mtime — DuckDB rewrites the file on checkpoint with no logical change,
    which would flush the cache for nothing.
    """
    global _watermark
    now = time.time()
    if _watermark and now - _watermark[0] < _WATERMARK_TTL:
        return _watermark[1]
    mx, n = cursor().execute(
        "select max(ingested_at), count(*) from fact_transactions").fetchone()
    wm = f"{mx}:{n}"
    _watermark = (now, wm)
    return wm


def fetch_dicts(sql: str, params: list[Any] | None = None) -> list[dict]:
    cur = cursor().execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
