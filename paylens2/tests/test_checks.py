"""Check 10 — the backstop that keeps v2's numbers equal to the classic path's."""
from __future__ import annotations

import pytest

from paylens2.core.checks import CheckFailed, check_v2, is_aggregate

GOOD = ("SELECT payment_method, "
        "(count(*) filter (where is_success))::DOUBLE / "
        "nullif(count(*) filter (where is_resolved), 0) AS success_rate, "
        "count(*) AS attempts "
        "FROM v_attempts GROUP BY payment_method")


def test_a_correct_query_passes_every_check():
    assert check_v2(GOOD) == ["c10a_views_only", "c10b_parse",
                              "c10c_no_select_star", "c10d_metric_formula",
                              "c10e_failure_only_grouping"]


@pytest.mark.parametrize("sql,check", [
    ("SELECT count(*) AS n FROM fact_transactions", "c10a_views_only"),
    ("SELECT t.* FROM v_attempts t JOIN dim_bank b ON b.bank_id = t.bank_id",
     "c10a_views_only"),
    ("SELECT * FROM v_attempts", "c10c_no_select_star"),
    ("SELECT 1 FROM v_attempts WHERE ((", "c10b_parse"),
])
def test_rejections(sql, check):
    with pytest.raises(CheckFailed) as e:
        check_v2(sql)
    assert e.value.check == check


def test_the_pending_bug_is_caught():
    """
    The failure this check exists for. Dividing by count(*) instead of by the
    resolved count includes PENDING in the denominator and understates the rate
    — a plausible, different number for the same question.
    """
    sql = ("SELECT (count(*) filter (where is_success))::DOUBLE / count(*) "
           "AS success_rate FROM v_attempts")
    with pytest.raises(CheckFailed) as e:
        check_v2(sql)
    assert e.value.check == "c10d_metric_formula"
    assert "is_resolved" in e.value.hint


def test_a_non_metric_alias_is_not_policed():
    """`AS my_rate` is the user's own quantity and is none of the check's business."""
    sql = ("SELECT (count(*) filter (where is_success))::DOUBLE / count(*) "
           "AS my_rate FROM v_attempts")
    assert check_v2(sql)


def test_aggregate_detection():
    assert is_aggregate(GOOD) is True
    assert is_aggregate("SELECT txn_id, amount FROM v_attempts") is False
    assert is_aggregate("SELECT count(*) AS n FROM v_attempts") is True
    # Unparseable SQL errs toward True: blocking an over-cap result is the safe
    # direction, since a truncated aggregate is a wrong number.
    assert is_aggregate("not sql at all ((") is True


# ── 10e — the check that stops a correct number being a nonsense sentence ──

def test_a_rate_grouped_by_a_failure_only_column_is_blocked():
    """
    From a real advisory answer: "the 'customer' failure owner has a 0% success
    rate on 19,784 attempts". Every row with a failure_owner IS a failure, so
    the group's success rate is zero by construction. Correct arithmetic,
    nonsense sentence — and it reads as a finding.
    """
    sql = ("SELECT failure_owner, (count(*) filter (where is_success))::DOUBLE / "
           "nullif(count(*) filter (where is_resolved), 0) AS success_rate "
           "FROM v_attempts GROUP BY failure_owner")
    with pytest.raises(CheckFailed) as e:
        check_v2(sql)
    assert e.value.check == "c10e_failure_only_grouping"
    assert "failure_owner" in e.value.reason


@pytest.mark.parametrize("col", ["decline_type", "failure_code", "journey_step_label"])
def test_every_failure_only_column_is_covered(col):
    sql = (f"SELECT {col}, (count(*) filter (where is_success))::DOUBLE / "
           f"nullif(count(*) filter (where is_resolved), 0) AS success_rate "
           f"FROM v_attempts GROUP BY {col}")
    with pytest.raises(CheckFailed):
        check_v2(sql)


def test_counting_failures_by_those_columns_is_the_right_shape_and_allowed():
    sql = ("SELECT failure_owner, count(*) filter (where is_failed) AS failures "
           "FROM v_attempts GROUP BY failure_owner")
    assert "c10e_failure_only_grouping" in check_v2(sql)


def test_a_rate_by_an_ordinary_dimension_is_untouched():
    sql = ("SELECT payment_method, (count(*) filter (where is_success))::DOUBLE / "
           "nullif(count(*) filter (where is_resolved), 0) AS success_rate "
           "FROM v_attempts GROUP BY payment_method")
    assert len(check_v2(sql)) == 5
