"""
BIRD mini-dev loader.

OFFLINE FIRST, BECAUSE THE OBVIOUS PATH DOES NOT WORK HERE
----------------------------------------------------------
The reference harness used `datasets.load_dataset("birdsql/bird_mini_dev")`.
In this venv `datasets` is not importable and there is no `pip` to add it, so
that path is kept as a last resort rather than the first one. `huggingface_hub`
IS installed, and the questions file can be fetched directly — one dependency
fewer and no install step.

The SQLite databases are a separate, manual download: BIRD distributes the
Mini-Dev databases through a Google Drive link that cannot be scripted. The
runner therefore preflights, reports which `db_id`s are present, and evaluates
only those — so the harness is correct at 0, 3 or 11 databases, and a partial
result is labelled as one rather than quietly reported as a full run.

Expected layout, either naming:

    evaluation/datasets/bird_dbs/<db_id>/<db_id>.sqlite
    evaluation/datasets/bird_dbs/<db_id>/sqlite/<db_id>.db
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evaluation import config

# Candidate filenames inside the Mini-Dev download that hold the questions.
_QUESTION_FILES = ("mini_dev_sqlite.json", "mini_dev_sqlite.jsonl",
                   "dev.json", "mini_dev.json")


def load_questions(limit: int | None = None, *, source: str = "auto") -> list[dict]:
    """
    [{question, gold_sql, db_id, difficulty, evidence}], oldest-first.

    `source`:
      local     a questions file sitting beside the downloaded databases
      hub       huggingface_hub.hf_hub_download
      datasets  the original load_dataset path, if the package works
      auto      local, then hub, then datasets, reporting which one won
    """
    order = ([source] if source != "auto" else ["local", "hub", "datasets"])
    errors: list[str] = []
    for src in order:
        try:
            recs = _LOADERS[src]()
            if recs:
                print(f"[bird] {len(recs)} questions from '{src}'")
                return recs[:limit] if limit else recs
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{src}: {type(exc).__name__}: {exc}")
    raise RuntimeError("could not load BIRD questions.\n  "
                       + "\n  ".join(errors)
                       + f"\n\nPut a questions file in {config.BIRD_DB_ROOT} "
                         f"(one of {', '.join(_QUESTION_FILES)}), or download "
                         f"the Mini-Dev set from https://bird-bench.github.io/")


def _norm(rows) -> list[dict]:
    out = []
    for r in rows:
        q = r.get("question") or r.get("Question")
        sql = r.get("SQL") or r.get("sql") or r.get("gold_sql")
        if not (q and sql):
            continue
        out.append({"question": q, "gold_sql": sql,
                    "db_id": r.get("db_id") or r.get("database") or "",
                    "difficulty": r.get("difficulty", "unknown"),
                    "evidence": r.get("evidence", "")})
    return out


def _from_local() -> list[dict]:
    for name in _QUESTION_FILES:
        for path in (config.BIRD_DB_ROOT / name,
                     config.BIRD_DB_ROOT.parent / name):
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".jsonl":
                return _norm(json.loads(l) for l in text.splitlines() if l.strip())
            return _norm(json.loads(text))
    raise FileNotFoundError(f"no questions file under {config.BIRD_DB_ROOT}")


def _from_hub() -> list[dict]:
    from huggingface_hub import hf_hub_download, list_repo_files
    files = list_repo_files(config.BIRD_HF_REPO, repo_type="dataset")
    wanted = [f for f in files
              if f.endswith((".json", ".jsonl")) and "mini_dev" in f.lower()] \
        or [f for f in files if f.endswith((".json", ".jsonl"))]
    if not wanted:
        raise FileNotFoundError(f"no json in {config.BIRD_HF_REPO}: {files[:10]}")
    path = Path(hf_hub_download(config.BIRD_HF_REPO, wanted[0],
                                repo_type="dataset"))
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return _norm(json.loads(l) for l in text.splitlines() if l.strip())
    return _norm(json.loads(text))


def _from_datasets() -> list[dict]:
    from datasets import load_dataset
    ds = load_dataset(config.BIRD_HF_REPO, split="mini_dev_sqlite")
    return _norm(dict(r) for r in ds)


_LOADERS = {"local": _from_local, "hub": _from_hub, "datasets": _from_datasets}


# ─────────────────────────────────────────────────────────────────────────────
def db_path(db_id: str) -> Path | None:
    for cand in (config.BIRD_DB_ROOT / db_id / f"{db_id}.sqlite",
                 config.BIRD_DB_ROOT / db_id / "sqlite" / f"{db_id}.db",
                 config.BIRD_DB_ROOT / db_id / f"{db_id}.db",
                 config.BIRD_DB_ROOT / f"{db_id}.sqlite"):
        if cand.exists():
            return cand
    return None


def available_dbs() -> list[str]:
    """Which db_ids are actually on disk. The runner evaluates only these."""
    if not config.BIRD_DB_ROOT.is_dir():
        return []
    out = set()
    for child in config.BIRD_DB_ROOT.iterdir():
        if child.is_dir() and db_path(child.name):
            out.add(child.name)
        elif child.suffix == ".sqlite":
            out.add(child.stem)
    return sorted(out)


def gold_conn(db_id: str) -> sqlite3.Connection:
    """
    The untouched SQLite connection.

    Gold SQL is executed here, in its native dialect, exactly as the reference
    harness did — so EX and VES sit beside the published figures rather than
    being a different measurement wearing the same name.
    """
    path = db_path(db_id)
    if path is None:
        raise FileNotFoundError(f"no database for {db_id!r} under "
                                f"{config.BIRD_DB_ROOT}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def schema_str(conn: sqlite3.Connection) -> str:
    """Compact DDL for the prompt: one line per table."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    lines = []
    for t in tables:
        if t.startswith("sqlite_"):
            continue
        cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
        lines.append(f'TABLE "{t}" (' +
                     ", ".join(f'"{c[1]}" {c[2]}' for c in cols) + ")")
    return "\n".join(lines)
