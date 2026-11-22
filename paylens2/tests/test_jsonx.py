"""
Tolerant JSON parsing — written against the output Qwen 2.5 Coder actually
produced, not against invented malformations.
"""
from __future__ import annotations

import pytest

from paylens2.core import jsonx

# Verbatim from a real author call: unquoted key, absent value, truncated key.
REAL_FAILURE = '''```json
{
  "action": "sql",
  "sql": "SELECT count(*) AS n FROM v_attempts WHERE merchant_id = 'M-1042'",
  "intent": "single_value",
  "chart": {
    "chart_type": "kpi",
    "x": null,
 y: "txn_count",
    "series":,
    "titl'''


def test_clean_json_is_unchanged():
    d = jsonx.loads('{"action":"sql","sql":"SELECT 1 AS n FROM v_attempts"}')
    assert d["sql"] == "SELECT 1 AS n FROM v_attempts"


def test_fenced_json_parses():
    assert jsonx.loads('```json\n{"a": 1}\n```')["a"] == 1


def test_the_real_failure_is_recovered_whole():
    d = jsonx.loads(REAL_FAILURE)
    assert d["action"] == "sql"
    assert d["sql"].startswith("SELECT count(*)")
    assert "M-1042" in d["sql"]
    assert d["intent"] == "single_value"
    assert d["chart"]["chart_type"] == "kpi"
    assert d["chart"]["y"] == "txn_count"       # the unquoted key
    assert d["chart"]["series"] is None         # the absent value


def test_trailing_comma():
    assert jsonx.loads('{"a": 1, "b": 2,}')["b"] == 2


def test_python_literals():
    d = jsonx.loads('{"a": True, "b": None, "c": False}')
    assert d == {"a": True, "b": None, "c": False}


def test_truncation_keeps_what_was_finished():
    d = jsonx.loads('{"action": "sql", "sql": "SELECT 1 AS n", "chart": {"x": "a", "y')
    assert d["sql"] == "SELECT 1 AS n"


def test_a_colon_inside_a_sql_string_is_not_mistaken_for_a_key():
    """
    The repair rewrites bare keys. A DuckDB cast or an interval inside the SQL
    string must survive it untouched, or the repair corrupts the query it is
    meant to rescue.
    """
    src = ('{"action":"sql","sql":"SELECT amount::DOUBLE AS a, '
           'x FROM v_attempts WHERE note = \'time: now\'"}')
    assert jsonx.loads(src)["sql"] == ("SELECT amount::DOUBLE AS a, x FROM v_attempts "
                                       "WHERE note = 'time: now'")


def test_nothing_usable_raises():
    with pytest.raises(ValueError):
        jsonx.loads("I'm sorry, I can't help with that.")


def test_salvage_prefers_the_sql_field():
    assert jsonx.salvage_sql(REAL_FAILURE).startswith("SELECT count(*)")


def test_salvage_from_a_bare_fenced_query():
    txt = "Here you go:\n```sql\nSELECT count(*) AS n FROM v_attempts\n```"
    assert jsonx.salvage_sql(txt) == "SELECT count(*) AS n FROM v_attempts"


def test_salvage_returns_empty_when_there_is_no_query():
    assert jsonx.salvage_sql("no sql here at all") == ""
