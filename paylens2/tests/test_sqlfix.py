"""
Deterministic SQL repair, written against the failure a user actually hit.

Two questions died on a single missing colon and were reported as "the field
genuinely is not here". These tests pin the repair and, just as importantly,
pin what it must never touch.
"""
from __future__ import annotations

import pytest

from paylens2.core.sqlfix import parses, repair, strip_ansi

# Verbatim from the failing answer, minus the ANSI noise.
REAL = ("SELECT (count(*) filter (where is_success)):DOUBLE / "
        "nullif(count(*) filter (where is_resolved), 0) AS success_rate, "
        "count(*) AS attemptsFROM v_attempts")


def test_the_real_failure_is_repaired():
    out, fixes = repair(REAL)
    assert parses(out)
    assert "single-colon cast" in fixes
    assert "::DOUBLE" in out


def test_a_query_that_already_parses_is_left_alone():
    sql = "SELECT count(*) AS n FROM v_attempts WHERE payment_method = 'UPI'"
    out, fixes = repair(sql)
    assert out == sql and fixes == []


def test_a_colon_inside_a_string_literal_is_never_touched():
    """
    The repair rewrites single-colon casts. A literal containing a colon must
    survive it, or the repair corrupts the query it exists to rescue.
    """
    sql = "SELECT count(*) AS n FROM v_attempts WHERE failure_code = 'TIME:OUT'"
    out, _ = repair(sql)
    assert "'TIME:OUT'" in out


def test_an_escaped_quote_inside_a_literal_survives():
    sql = "SELECT count(*) AS n FROM v_attempts WHERE merchant_name = 'O''Brien:X'"
    out, _ = repair(sql)
    assert "'O''Brien:X'" in out


@pytest.mark.parametrize("broken,label", [
    ("SELECT amount:DOUBLE AS a FROM v_attempts", "single-colon cast"),
    ("SELECT count(*) AS n FROM v_attempts;", "trailing semicolon"),
    ("```sql\nSELECT count(*) AS n FROM v_attempts\n```", "markdown fence"),
])
def test_each_repair(broken, label):
    out, fixes = repair(broken)
    assert label in fixes
    assert parses(out)


def test_comma_damage_is_repaired_when_it_actually_breaks_the_query():
    """
    sqlglot parses `SELECT a,, b FROM t` happily, and repair() leaves anything
    that parses alone — correct, because every rewrite is a chance to change
    meaning. The comma fixes exist for queries that are genuinely broken.
    """
    broken = ("SELECT count(*) AS n,, amount:DOUBLE AS a, FROM v_attempts")
    out, fixes = repair(broken)
    assert parses(out)
    assert {"doubled comma", "dangling comma", "single-colon cast"} <= set(fixes)


def test_welded_keywords_from_lost_newlines():
    out, _ = repair("SELECT count(*) AS nFROM v_attempts")
    assert parses(out) and " FROM " in out


def test_an_unrepairable_query_is_returned_not_mangled():
    """A repair that does not produce a parseable query must change nothing."""
    broken = "SELECT FROM WHERE (("
    out, fixes = repair(broken)
    assert out == broken
    assert fixes == []


def test_empty_input_is_safe():
    assert repair("") == ("", [])


def test_ansi_is_stripped():
    assert strip_ansi("\x1b[4msrc\x1b[0m UNION") == "src UNION"
    assert strip_ansi("plain text") == "plain text"


def test_repair_never_changes_a_querys_meaning():
    """
    The repairs are mechanical. A repaired query must select the same thing,
    so the parsed output is compared against the hand-corrected input.
    """
    import sqlglot
    fixed, _ = repair(REAL)
    hand = ("SELECT (count(*) filter (where is_success))::DOUBLE / "
            "nullif(count(*) filter (where is_resolved), 0) AS success_rate, "
            "count(*) AS attempts FROM v_attempts")
    assert (sqlglot.parse_one(fixed, read="duckdb").sql()
            == sqlglot.parse_one(hand, read="duckdb").sql())
