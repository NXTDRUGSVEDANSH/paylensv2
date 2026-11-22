"""
Tolerant JSON parsing for models that write excellent SQL and careless JSON.

Qwen 2.5 Coder is the case this exists for. On the same prompt that produced
perfectly correct SQL it emitted:

    "x": null,
 y: "txn_count",          <- unquoted key
    "series":,            <- missing value
    "titl                 <- and then stopped

A code model is optimised for code, not for a nested object with seven keys.
Rejecting the whole response throws away a correct query because its wrapper was
untidy, so this repairs the common malformations and, failing that, salvages the
SQL directly.

NOTHING UNSAFE RIDES ON THIS. Salvaged SQL still passes check 10 and all nine
guard checks before it can execute; a bad salvage becomes a rejection, not a
wrong answer. The alternative — discarding the call and retrying — costs a round
trip to fix a missing quotation mark.
"""
from __future__ import annotations

import json
import logging
import re
from backend.core import events

log = logging.getLogger("paylens2.jsonx")

_FENCE = re.compile(r"```(?:json|sql)?\s*(.*?)(?:```|\Z)", re.DOTALL)


def _balance(text: str) -> str:
    """Close brackets a truncated response left open, ignoring those in strings."""
    depth_c = depth_s = 0
    in_str = esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        depth_c += (ch == "{") - (ch == "}")
        depth_s += (ch == "[") - (ch == "]")
    out = text
    if in_str:
        out += '"'
    return out + "]" * max(0, depth_s) + "}" * max(0, depth_c)


def _normalise(text: str) -> str:
    """The malformations that are mechanical, with no truncation handling."""
    s = text.strip()
    # a value that is simply absent:  "series":,   or   "series":}
    s = re.sub(r':\s*(?=[,}\]])', ": null", s)
    # a bare key:  y: "x"  ->  "y": "x". Anchored to a line start or a separator
    # so it cannot fire inside a string literal such as a SQL cast.
    s = re.sub(r'(?m)(^|[{,])(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1\2"\3"\4:', s)
    s = re.sub(r'""([A-Za-z_][A-Za-z0-9_]*)""\s*:', r'"\1":', s)
    # trailing commas
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # python literals
    s = re.sub(r"(?<![\w\"])(True|False|None)(?![\w\"])",
               lambda m: {"True": "true", "False": "false", "None": "null"}[m.group(1)], s)
    return s


def _cut_points(s: str) -> list[int]:
    """
    Offsets we can truncate at and still have a meaningful object: the end, and
    every separator outside a string literal, latest first.

    This is what rescues a response that simply stopped mid-key. Cutting back to
    the last complete pair and closing the brackets keeps everything the model
    did finish saying.
    """
    pts, in_str, esc = [len(s)], False, False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str and ch in ",}":
            pts.append(i + 1 if ch == "}" else i)
    return sorted(set(pts), reverse=True)


def _candidates(text: str) -> list[str]:
    """Fenced blocks first, then the raw text from its first brace."""
    out = []
    for block in _FENCE.findall(text or ""):
        if block.strip().startswith("{"):
            out.append(block.strip())
    start = (text or "").find("{")
    if start != -1:
        out.append(text[start:])
    return out


def _try(s: str) -> dict | None:
    try:
        v = json.loads(s)
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def _sql_usable(d: dict) -> bool:
    """
    A recovered object is only a recovery if the query inside it survived.

    Truncation recovery cuts back to the last complete key/value pair, which can
    leave a `sql` field holding half a query — `WITH src_last_week AS (… UNION`
    with no closing SELECT. That parsed as JSON, passed as a recovery, reached
    the guard and produced the parse error a user saw printed as their answer.
    Repair is attempted first, because a one-character flaw is not a truncation.
    """
    sql = d.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return True                      # clarify/refuse carry no SQL
    from paylens2.core import sqlfix
    fixed, _ = sqlfix.repair(sql)
    return sqlfix.parses(fixed)


def loads(text: str) -> dict:
    """
    Parse, repairing and truncating as needed.

    Raises ValueError only when nothing resembles an object; the caller then
    falls back to salvage_sql().
    """
    for cand in _candidates(text):
        v = _try(cand)
        if v is not None:
            return v
        norm = _normalise(cand)
        v = _try(norm)
        if v is not None:
            log.info("repaired malformed JSON from the model")
            events.recovery("author", "the model's JSON was malformed — repaired")
            return v
        for cut in _cut_points(norm):
            piece = norm[:cut].rstrip().rstrip(",")
            if len(piece) < 2:
                continue
            v = _try(_balance(piece))
            if v is None:
                continue
            if not _sql_usable(v):
                log.info("truncation recovery left an unusable query; cutting further")
                events.recovery("author", "a recovered fragment held an unusable "
                                          "query — cutting further back")
                continue
            log.info("recovered a truncated JSON response (cut at %d of %d)",
                     cut, len(norm))
            events.recovery("author", "the model's response was cut off — "
                                      f"recovered {cut} of {len(norm)} characters")
            return v
    raise ValueError(f"no JSON object found in response: {(text or '')[:200]!r}")


_SQL = re.compile(r"\b(WITH|SELECT)\b.*", re.I | re.DOTALL)


def salvage_sql(text: str) -> str:
    """
    Last resort: pull the query out of a response whose JSON cannot be rescued.

    Prefers the "sql" field's own string, because a fenced block may be an
    illustrative snippet rather than the answer.
    """
    m = re.search(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', text or "", re.S)
    if m:
        try:
            return json.loads(f'"{m.group(1)}"').strip().rstrip(";")
        except json.JSONDecodeError:
            pass
    for block in _FENCE.findall(text or ""):
        hit = _SQL.search(block)
        if hit:
            return hit.group(0).strip().rstrip(";` \n")
    hit = _SQL.search(text or "")
    if hit:
        # Stop at the first line that is clearly back in JSON land.
        sql = hit.group(0)
        sql = re.split(r'"\s*,\s*"[a-z_]+"\s*:', sql)[0]
        return sql.strip().rstrip('";`, \n')
    return ""
