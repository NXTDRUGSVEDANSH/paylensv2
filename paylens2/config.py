"""v2 settings. Everything overridable by environment, with defaults that work."""
from __future__ import annotations

import os
from pathlib import Path

from backend import config as base

VIEWS_YAML = Path(__file__).parent / "schema" / "views.yaml"

# Models. Both default to the classic pipeline's, so a working .env needs no edit.
# AUTHOR_MODEL writes SQL and benefits from a coding-capable model; SYNTH_MODEL
# writes prose. Split them when the budget allows.
AUTHOR_MODEL = os.getenv("V2_AUTHOR_MODEL", "") or base.PLANNER_MODEL
SYNTH_MODEL = os.getenv("V2_SYNTH_MODEL", "") or base.NARRATOR_MODEL

# Call budget. The paid key makes a generous heal loop affordable; the cap exists
# so a pathological question cannot spin.
MAX_AUTHOR_CALLS = int(os.getenv("V2_MAX_AUTHOR_CALLS", "1"))
MAX_HEAL_CALLS = int(os.getenv("V2_MAX_HEAL_CALLS", "2"))
MAX_SYNTH_CALLS = int(os.getenv("V2_MAX_SYNTH_CALLS", "1"))
MAX_REPAIR_CALLS = int(os.getenv("V2_MAX_REPAIR_CALLS", "1"))

# Rows handed to the profiler before it summarises rather than enumerates.
PROFILE_FULL_ROWS = int(os.getenv("V2_PROFILE_FULL_ROWS", "50"))
PROFILE_TOP_K = int(os.getenv("V2_PROFILE_TOP_K", "8"))

# Bumped whenever the prompt contract or the schema card changes in a way that
# would make previously-stored SQL wrong to copy. v2 few-shots carrying a
# different stamp are ignored rather than shown to the model.
#
# This is not hypothetical. The first version had no stamp: a query written
# before "last week" was pinned to the ISO week got written back, came straight
# back as a similarity-1.000 few-shot, and the model copied its trailing-7-day
# window over the corrected instruction. A bad example beats a good rule.
PROMPT_VERSION = os.getenv("V2_PROMPT_VERSION", "p3")

# Few-shot retrieval from the existing Chroma KB.
FEWSHOT_K = int(os.getenv("V2_FEWSHOT_K", "4"))
USE_KB = os.getenv("V2_USE_KB", "true").lower() in ("true", "1", "yes")

# Enumerated dimension values in the schema card. A dimension with more distinct
# values than this is described by its cardinality instead of listed.
MAX_ENUM_VALUES = int(os.getenv("V2_MAX_ENUM_VALUES", "25"))

MAX_SQL_ROWS = base.MAX_SQL_ROWS
SQL_TIMEOUT_S = base.SQL_TIMEOUT_S

# ── NLQ -> SQL cache ────────────────────────────────────────────────────────
# Caches the AUTHORED query, never the answer: a hit skips call 1 and nothing
# else, so the numbers are measured afresh every time and the guard still
# injects the caller's own row filter.
#
# Default on is safe because of PROMOTE_AFTER. A query is not eligible for
# reuse until the same question has produced an equivalent query that many
# times, so a one-off lucky parse can never be served back.
USE_CACHE = os.getenv("V2_CACHE", "true").lower() in ("true", "1", "yes")
CACHE_SIZE = int(os.getenv("V2_CACHE_SIZE", "256"))
CACHE_TTL_S = int(os.getenv("V2_CACHE_TTL", "900"))
CACHE_PROMOTE_AFTER = int(os.getenv("V2_CACHE_PROMOTE_AFTER", "2"))

# ── data quality ────────────────────────────────────────────────────────────
# A handful of small probes over the window the answer actually used. They go
# through the guard like any user query: a background check is the easiest
# place to leak rows a role may not see.
USE_DQ = os.getenv("V2_DQ", "true").lower() in ("true", "1", "yes")

# ── mix-vs-rate decomposition ───────────────────────────────────────────────
USE_DECOMPOSE = os.getenv("V2_DECOMPOSE", "true").lower() in ("true", "1", "yes")
