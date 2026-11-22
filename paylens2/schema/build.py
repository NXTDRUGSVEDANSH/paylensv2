"""
Create the v2 views inside the DuckDB file.

Run once after data/generate.py, and again whenever views.sql changes:

    python -m paylens2.schema.build

The serving connection opens DuckDB read-only, so this script opens its own
writable connection. Nothing else may hold the file open while it runs —
DuckDB permits a single writer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

from backend import config

VIEWS_SQL = Path(__file__).with_name("views.sql")
V2_VIEWS = ("v_attempts", "v_settlements")


def build(db_path: str | None = None, *, verbose: bool = True) -> list[str]:
    path = db_path or config.DUCKDB_PATH
    ddl = VIEWS_SQL.read_text(encoding="utf-8")
    con = duckdb.connect(path, read_only=False)
    try:
        con.execute(ddl)
        made = []
        for v in V2_VIEWS:
            n = con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]
            made.append(v)
            if verbose:
                print(f"  {v:16} {n:>12,} rows")
        return made
    finally:
        con.close()


def present(con) -> list[str]:
    """Which v2 views exist on an already-open connection."""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_type = 'VIEW' AND table_name IN ('v_attempts','v_settlements')"
    ).fetchall()
    return sorted(r[0] for r in rows)


if __name__ == "__main__":
    print(f"building v2 views in {config.DUCKDB_PATH}")
    try:
        build()
    except duckdb.IOException as exc:
        print(f"\ncannot open the database for writing: {exc}\n"
              "Stop the server (it holds a read-only handle on some platforms) "
              "and try again.", file=sys.stderr)
        raise SystemExit(1)
    print("done")
