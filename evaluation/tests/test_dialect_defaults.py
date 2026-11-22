"""
The shared-code changes must not change PayLens's own behaviour.

`guard()` and `check_v2()` gained a `dialect` parameter and `check_v2()` gained
a `skip` set, both for the benchmark track. These tests exist so that a later
change to those defaults goes red here rather than silently relaxing a
production check.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core import guard as guard_mod
from paylens2.core import checks


def test_defaults_are_still_duckdb_and_no_skips():
    assert checks.DIALECT == "duckdb"
    sig = inspect.signature(checks.check_v2)
    assert sig.parameters["dialect"].default == "duckdb"
    assert sig.parameters["skip"].default == frozenset()
    assert inspect.signature(guard_mod.guard).parameters["dialect"].default \
        == "duckdb"


def test_paylens_checks_still_fire_on_their_own_dialect():
    """The four defects check 10 exists for must still be rejected."""
    with pytest.raises(checks.CheckFailed) as e:
        checks.check_v2("SELECT count(*) FROM fact_transactions")
    assert e.value.check == "c10a_views_only"

    with pytest.raises(checks.CheckFailed) as e:
        checks.check_v2("SELECT * FROM v_attempts")
    assert e.value.check == "c10c_no_select_star"

    with pytest.raises(checks.CheckFailed) as e:
        checks.check_v2("SELECT count(*)::DOUBLE / count(*) AS success_rate "
                        "FROM v_attempts")
    assert e.value.check == "c10d_metric_formula"

    with pytest.raises(checks.CheckFailed) as e:
        checks.check_v2(
            "SELECT failure_owner, (count(*) filter (where is_success))::DOUBLE"
            " / nullif(count(*) filter (where is_resolved), 0) AS success_rate "
            "FROM v_attempts GROUP BY failure_owner")
    assert e.value.check == "c10e_failure_only_grouping"


def test_a_clean_v2_query_passes_all_five():
    passed = checks.check_v2(
        "SELECT payment_method, (count(*) filter (where is_success))::DOUBLE / "
        "nullif(count(*) filter (where is_resolved), 0) AS success_rate "
        "FROM v_attempts GROUP BY payment_method")
    assert passed == ["c10a_views_only", "c10b_parse", "c10c_no_select_star",
                      "c10d_metric_formula", "c10e_failure_only_grouping"]


# ── the benchmark configuration ─────────────────────────────────────────────
BIRD_SKIP = frozenset({"c10a_views_only", "c10c_no_select_star",
                       "c10d_metric_formula", "c10e_failure_only_grouping"})


def test_sqlite_identifiers_parse_only_under_the_sqlite_dialect():
    """
    The finding that makes the benchmark number honest.

    A SQLite corpus is full of column names with spaces and parentheses. Parsed
    as duckdb they fail, and that failure would be recorded as the model
    writing a broken query.
    """
    sql = "SELECT count(*) FROM frpm WHERE `Free Meal Count (K-12)` > 100"
    with pytest.raises(checks.CheckFailed) as e:
        checks.check_v2(sql, skip=BIRD_SKIP)
    assert e.value.check == "c10b_parse"

    assert checks.check_v2(sql, dialect="sqlite", skip=BIRD_SKIP) == \
        ["c10a_views_only", "c10b_parse", "c10c_no_select_star",
         "c10d_metric_formula", "c10e_failure_only_grouping"]


def test_select_star_is_allowed_only_when_skipped():
    sql = "SELECT * FROM schools WHERE County = 'Alameda'"
    with pytest.raises(checks.CheckFailed):
        checks.check_v2(sql, dialect="sqlite")
    assert checks.check_v2(sql, dialect="sqlite", skip=BIRD_SKIP)


def test_skipping_does_not_leak_into_a_default_call():
    """A skip is per call, not sticky."""
    checks.check_v2("SELECT * FROM schools", dialect="sqlite", skip=BIRD_SKIP)
    with pytest.raises(checks.CheckFailed):
        checks.check_v2("SELECT * FROM v_attempts")
