"""
Deterministic repair of the small, mechanical ways a code model mangles SQL.

WHY THIS EXISTS
---------------
A user asked two questions and both died on this:

    [c10b_parse] query does not parse: Invalid expression / Unexpected token.
    :DOUBLE / nullif(count(*) filter (where is_resolved), 0) AS success_rate

The model had written `(count(*) filter (where is_success)):DOUBLE` — one colon
instead of two. Everything else about the query was correct: the right view, the
right indicator columns, the official metric expression. One character cost the
whole answer, the heal call did not catch it, and the user was told the data did
not hold the field.

Same philosophy as jsonx.py, applied to SQL instead of JSON: repair the
mechanical flaw, keep the correct query. A round trip to the model to fix a
missing colon is waste, and a failed round trip is a dead end.

WHAT MAKES IT SAFE
------------------
1. String literals are masked before any pattern runs, so a cast fix can never
   touch `WHERE note = 'time: now'`.
2. Every repair is verified by re-parsing. If a repair does not parse, or the
   input already parsed and the output does not, the ORIGINAL is returned.
3. Repairs are recorded and shown in the Trace tab. Silently rewriting a user's
   query is its own defect — if we changed it, they get to see that we did.
4. Nothing here relaxes a check. The repaired SQL still faces check 10 and all
   nine guard checks exactly as written.
"""
from __future__ import annotations

import re

import sqlglot

DIALECT = "duckdb"

# Types DuckDB casts to, for the single-colon repair. Deliberately a closed list:
# matching any identifier after a colon would rewrite `:param` placeholders and
# anything else that legitimately follows one.
_CAST_TYPES = ("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC", "INT", "INTEGER",
               "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "VARCHAR", "TEXT",
               "STRING", "DATE", "TIMESTAMP", "TIME", "BOOLEAN", "BOOL", "JSON",
               "UUID", "BLOB", "INTERVAL")

_STR = re.compile(r"'(?:[^']|'')*'")          # SQL string literal, '' escaping
_FENCE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.I)
_SENTINEL = "\x00S%d\x00"


def _mask(sql: str) -> tuple[str, list[str]]:
    """Replace every string literal with a sentinel no pattern below can match."""
    lits: list[str] = []

    def take(m: re.Match) -> str:
        lits.append(m.group(0))
        return _SENTINEL % (len(lits) - 1)

    return _STR.sub(take, sql), lits


def _unmask(sql: str, lits: list[str]) -> str:
    for i, lit in enumerate(lits):
        sql = sql.replace(_SENTINEL % i, lit)
    return sql


def parses(sql: str) -> bool:
    try:
        return sqlglot.parse_one(sql, read=DIALECT) is not None
    except Exception:  # noqa: BLE001
        return False


# Each entry: (label, pattern, replacement). Applied to the MASKED text only.
_FIXES: list[tuple[str, re.Pattern, str]] = [
    # The one that started this. `x:DOUBLE` -> `x::DOUBLE`.
    ("single-colon cast",
     # Only a preceding colon is excluded, so `x::DOUBLE` is left alone while
     # `x:DOUBLE` — the form that actually broke a user's question — is fixed.
     # `:param` placeholders are safe because the type list is closed.
     re.compile(r"(?<!:):(" + "|".join(_CAST_TYPES) + r")\b", re.I), r"::\1"),
    # Three or more colons from an over-eager repair elsewhere.
    ("triple-colon cast", re.compile(r":{3,}"), "::"),
    # `count(*)filter(where …)` — DuckDB wants the space.
    ("filter spacing", re.compile(r"\)\s*filter\s*\(", re.I), ") FILTER ("),
    # A doubled comma left by an edited projection list.
    ("doubled comma", re.compile(r",\s*,"), ", "),
    # A comma immediately before FROM/WHERE/GROUP/ORDER/HAVING/LIMIT.
    ("dangling comma",
     re.compile(r",\s*(?=\b(FROM|WHERE|GROUP|ORDER|HAVING|LIMIT|UNION)\b)", re.I), " "),
    # Keywords welded to the previous token when newlines were lost in transit:
    # `AS attemptsFROM v_attempts`, `UNIONALL`, `ALLSELECT`.
    ("welded keyword",
     re.compile(r"(?<=[a-z0-9_)])(FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|UNION|"
                r"LIMIT|SELECT|JOIN|ON)\b"), r" \1"),
    ("welded union",
     re.compile(r"\b(UNION|EXCEPT|INTERSECT)(ALL|SELECT)\b", re.I), r"\1 \2"),
    ("welded all-select", re.compile(r"\bALL(SELECT)\b", re.I), r"ALL \1"),
    # Smart quotes from a model that formatted its answer.
    ("smart quotes", re.compile(r"[‘’‛]"), "'"),
    ("smart double quotes", re.compile(r"[“”]"), '"'),
    # Non-breaking spaces read as ordinary ones by people and not by parsers.
    ("non-breaking space", re.compile(r"[   ]"), " "),
]


def repair(sql: str) -> tuple[str, list[str]]:
    """
    (repaired_sql, labels_of_repairs_applied).

    A query that already parses is returned untouched apart from fence and
    semicolon trimming, because a parsing query needs no help and every rewrite
    is a chance to change its meaning.
    """
    if not sql or not sql.strip():
        return sql, []

    applied: list[str] = []
    out = _FENCE.sub("", sql).strip()
    if out != sql.strip():
        applied.append("markdown fence")
    stripped = out.rstrip().rstrip(";").rstrip()
    if stripped != out.rstrip():
        applied.append("trailing semicolon")
    out = stripped

    if parses(out):
        return out, applied

    before_fixes = out
    masked, lits = _mask(out)
    for label, pat, repl in _FIXES:
        fixed = pat.sub(repl, masked)
        if fixed != masked:
            masked = fixed
            applied.append(label)
    candidate = _unmask(masked, lits)

    # Collapse any double spaces the welding fixes introduced, outside literals.
    masked2, lits2 = _mask(candidate)
    masked2 = re.sub(r"[ \t]{2,}", " ", masked2)
    candidate = _unmask(masked2, lits2)

    if candidate != before_fixes and parses(candidate):
        return candidate, applied
    # Could not be repaired. Hand back the fence/semicolon-trimmed original so
    # the error message quotes what the model actually wrote.
    return before_fixes, [a for a in applied if a in ("markdown fence",
                                                      "trailing semicolon")]


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """
    Remove terminal colour codes from an error message.

    sqlglot highlights the offending token with ANSI escapes. Those reached the
    user's screen as literal `[4m` / `[0m` noise inside the answer, and reached
    the heal prompt as tokens the model then had to reason around.
    """
    return _ANSI.sub("", text or "")
