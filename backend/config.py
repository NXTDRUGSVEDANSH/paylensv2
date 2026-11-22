"""Central configuration. Env-driven, with safe defaults."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# ── LLM ──────────────────────────────────────────────────────────────────────
# Planner and narrator can live on DIFFERENT providers. Both must be
# OpenAI-compatible, which covers OpenRouter, NVIDIA NIM, Google's OpenAI
# endpoint, Together, vLLM, Ollama and LM Studio.
#
# Each role falls back to the shared LLM_* values, so a single-provider setup
# still works with one key and nothing else set.
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")

PLANNER_MODEL = os.getenv("PLANNER_MODEL", "qwen/qwen3-coder-next")
PLANNER_API_KEY = os.getenv("PLANNER_API_KEY", "") or LLM_API_KEY
PLANNER_BASE_URL = os.getenv("PLANNER_BASE_URL", "") or LLM_BASE_URL

NARRATOR_MODEL = os.getenv("NARRATOR_MODEL", "google/gemini-3-flash-preview")
NARRATOR_API_KEY = os.getenv("NARRATOR_API_KEY", "") or LLM_API_KEY
NARRATOR_BASE_URL = os.getenv("NARRATOR_BASE_URL", "") or LLM_BASE_URL

LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
# Reasoning models (NVIDIA Nemotron, Qwen "thinking" variants) spend output tokens
# on an internal monologue BEFORE the answer. With a normal budget they hit the
# limit mid-thought and return empty content, which looks like a provider fault
# and is not. Give the planner headroom.
PLANNER_MAX_TOKENS = int(os.getenv("PLANNER_MAX_TOKENS", "8192"))
LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "120"))

# ── Storage ──
DUCKDB_PATH = Path(os.getenv("DUCKDB_PATH", ROOT / "data" / "paylens.duckdb")).expanduser()
if not DUCKDB_PATH.is_absolute():
    DUCKDB_PATH = (ROOT / DUCKDB_PATH).resolve()
# Absolute by construction. Fractal AI's CWD-relative "./chroma_db" puts the KB
# wherever uvicorn happened to be launched from.
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", ROOT / "data" / "chroma_db")).expanduser()
if not CHROMA_PATH.is_absolute():
    CHROMA_PATH = (ROOT / CHROMA_PATH).resolve()
SEMANTIC_MODEL_PATH = ROOT / "semantic" / "semantic_model.yaml"
SEEDS_PATH = ROOT / "semantic" / "seeds.yaml"

# ── Safety / cost controls ──
MAX_SQL_RETRIES = int(os.getenv("MAX_SQL_RETRIES", "2"))
MAX_SQL_ROWS = int(os.getenv("MAX_SQL_ROWS", "5000"))
EXPLAIN_SCAN_LIMIT = int(os.getenv("EXPLAIN_SCAN_LIMIT", "50000000"))
# Ceiling for the KPI Lab panels, whose SQL is fixed and reviewed rather than
# model-generated. DuckDB estimates a three-way join with a count(distinct) at
# ~1e9 rows and then executes it in 129ms over 777k, so the default ceiling is a
# false block there. The statement timeout is the real control for these.
PANEL_SCAN_LIMIT = int(os.getenv("PANEL_SCAN_LIMIT", "2000000000"))

# DuckDB is held to a memory budget because the deployment target has 512MB of
# RAM in total and the process has to leave room for everything else.
#
# `DUCKDB_TEMP_DIR` is not an optimisation — it is what makes the budget safe.
# Without a temp directory DuckDB cannot spill, so a query that needs one block
# more than the limit FAILS instead of going to disk, and the symptom is an
# OutOfMemoryException on whichever query happened to run when the buffer pool
# was full. That is a query failing because of what ran before it, which is the
# least debuggable failure mode there is.
DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "256MB")
DUCKDB_TEMP_DIR = os.getenv("DUCKDB_TEMP_DIR", str(ROOT / "data" / ".duckdb_tmp"))
SQL_TIMEOUT_S = int(os.getenv("SQL_TIMEOUT_S", "10"))

# ── Caching ──
DISABLE_CHROMA = os.getenv("DISABLE_CHROMA", "false").lower() in ("true", "1", "yes")
ANSWER_CACHE_TTL = int(os.getenv("ANSWER_CACHE_TTL", "600"))
ANSWER_CACHE_SIZE = int(os.getenv("ANSWER_CACHE_SIZE", "256"))
CACHE_T1 = float(os.getenv("CACHE_T1", "0.95"))     # reuse SQL, no planner call
CACHE_T2 = float(os.getenv("CACHE_T2", "0.75"))     # inject as few-shot
LEXICAL_FLOOR = float(os.getenv("LEXICAL_FLOOR", "0.60"))
PROMPT_VERSION = "plan-v1"

# ── LLM call budget, enforced by the orchestrator (not left emergent) ──
MAX_PLANNER_CALLS = 1
MAX_HEAL_CALLS = MAX_SQL_RETRIES
MAX_NARRATOR_CALLS = 1

# ── Authentication ──
# Separate SQLite file: the DuckDB warehouse is opened read-only by design, so
# user and session state cannot live there.
AUTH_DB_PATH = Path(os.getenv("AUTH_DB_PATH", ROOT / "data" / "auth.db")).expanduser()
if not AUTH_DB_PATH.is_absolute():
    AUTH_DB_PATH = (ROOT / AUTH_DB_PATH).resolve()
# Seeds four demo accounts at startup. Turn OFF for any real deployment and
# provision through an identity provider instead.
SEED_DEMO_USERS = os.getenv("SEED_DEMO_USERS", "true").lower() in ("true", "1", "yes")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "paylens-demo")
# Signing in is OPTIONAL by default: anyone can browse, and signing in changes
# which rows you see. Set REQUIRE_AUTH=true for a real deployment and the
# full-screen gate returns — the enforcement machinery is identical either way,
# this only decides whether an unauthenticated visitor is turned away.
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "false").lower() in ("true", "1", "yes")
# The role an unauthenticated visitor gets. 'cxo' (unrestricted) suits a demo;
# a real deployment that wanted anonymous browsing would set something narrow.
ANON_ROLE = os.getenv("ANON_ROLE", "cxo")

# ── Clarification ──
# Ask rather than assume when a REQUIRED input is missing. Default on: for a metric
# like success rate, "over what period?" changes the answer completely, and a silent
# default is a confidently wrong number. Set false to fall back to stating the
# assumption instead (the previous behaviour, still used for everything we CAN default).
CLARIFY_MISSING_WINDOW = os.getenv("CLARIFY_MISSING_WINDOW", "true").lower() in ("true", "1", "yes")

# ── Deployment ──
# CORS defaults to "*" for local development. In any shared environment set
# CORS_ORIGINS to an explicit comma-separated list — a wildcard plus credentials
# is the single most common way an internal analytics API ends up public.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
PORT = int(os.getenv("PORT", "8000"))           # App Runner/ECS inject this
SERVE_FRONTEND = os.getenv("SERVE_FRONTEND", "true").lower() in ("true", "1", "yes")

# "Today" for the demo. The generated data ends here.
DEMO_NOW = os.getenv("DEMO_NOW", "2026-11-20T12:00:00")
