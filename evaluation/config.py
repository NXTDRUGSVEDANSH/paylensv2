"""
Central config for the PayLens v2 evaluation harness.

Mirrors the layout of the reference harness this ports from, so the two
reports' numbers sit beside each other without a translation step.

THE PINNED DATE IS LOAD-BEARING
-------------------------------
`periods.resolve()` bakes literal dates into the SQL, so "last week" means
something different on every run. The generated data ends at
`DEMO_NOW = 2026-11-20T12:00:00` (backend/config.py), which means an unpinned
run asked on 2026-11-22 resolves "last week" to 2026-11-16..23 — a window with
almost no rows in it. Every accuracy and latency figure would then be measured
against a nearly empty result and would be meaningless.

2026-11-20 is chosen because it also reproduces eval/golden.yaml's windows
exactly (last week -> 2026-11-09..16, last month -> 2026-10-01..11-01), so the
hand-written reference SQL already in that file can be reused as gold without
being rewritten.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

EVAL_ROOT = Path(__file__).parent
PROJECT_ROOT = EVAL_ROOT.parent

# ── the pinned evaluation date ───────────────────────────────────────────────
EVAL_TODAY = date.fromisoformat(os.getenv("EVAL_TODAY", "2026-11-20"))

# ── datasets ────────────────────────────────────────────────────────────────
OWN_QUERIES_PATH = EVAL_ROOT / "datasets" / "own_queries.jsonl"
GOLD_SQL_PATH = EVAL_ROOT / "datasets" / "gold_sql.yaml"
BIRD_DB_ROOT = EVAL_ROOT / "datasets" / "bird_dbs"
BIRD_HF_REPO = os.getenv("BIRD_HF_REPO", "birdsql/bird_mini_dev")

# Inputs folded into the query bank rather than duplicated.
GOLDEN_YAML = PROJECT_ROOT / "eval" / "golden.yaml"
ADVERSARIAL_YAML = PROJECT_ROOT / "eval" / "adversarial.yaml"
SEEDS_YAML = PROJECT_ROOT / "semantic" / "seeds.yaml"

# ── outputs ─────────────────────────────────────────────────────────────────
RESULTS_DIR = Path(os.getenv("EVAL_RESULTS_DIR", str(EVAL_ROOT / "results")))

# ── the judge ───────────────────────────────────────────────────────────────
# Deliberately NOT the author model. Scoring a model's output with the same
# model is self-evaluation: it shares the blind spots that produced the answer.
# ernie-5.5-flash was the only free model that passed both the planner and the
# narrator task reliably (docs/FREE_MODELS.md), which makes it the cheapest
# independent judge available here.
JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "baidu/ernie-5.5-flash-preview:free")

# ── run mechanics ───────────────────────────────────────────────────────────
# 3 rather than 4: the configured models are OpenRouter free tier and
# rate-limited, and a 429 storm costs more wall clock than the fourth worker
# saves. The shared DuckDB connection is read-only with a cursor per query, so
# threads themselves are safe.
MAX_WORKERS = int(os.getenv("EVAL_MAX_WORKERS", "3"))
CHECKPOINT_EVERY = int(os.getenv("EVAL_CHECKPOINT_EVERY", "10"))

# The number of checks a clean v2 measurement answer should pass:
# 5 from check 10 (c10a..c10e) + 9 from the guard (c1..c9).
EXPECTED_CHECKS = 14

# The six actions call 1 can return. The order is fixed so the confusion
# matrix is stable across runs.
ACTIONS = ("sql", "advise", "explain", "decompose", "clarify", "refuse")
