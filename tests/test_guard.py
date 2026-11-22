"""
Adversarial suite for the validation gate. Zero false negatives required:
one leaked write invalidates the entire safety story.
"""
import pytest

from backend.core import rbac
from backend.core.guard import GuardBlocked, guard, guard_or_result
from semantic.loader import get_model

M = get_model()
CXO = rbac.principal_for("cxo")
MERCHANT = rbac.principal_for("merchant_user", {"merchant_id": "M-1042"})
REGIONAL = rbac.principal_for("regional_manager", {"region": "South"})

ADVERSARIAL = [
    ("drop table",            "DROP TABLE fact_transactions"),
    ("delete rows",           "DELETE FROM fact_transactions WHERE status='FAILED'"),
    ("update rows",           "UPDATE fact_transactions SET status='SUCCESS'"),
    ("insert rows",           "INSERT INTO dim_bank VALUES ('X','Y','issuer','Z',false,0,0)"),
    ("stacked statement",     "SELECT 1 FROM fact_transactions; DROP TABLE dim_bank"),
    ("comment-hidden ddl",    "SELECT 1 FROM fact_transactions /* x */; DROP TABLE dim_bank"),
    ("unknown table",         "SELECT * FROM secret_payroll"),
    ("filesystem read",       "SELECT * FROM read_csv_auto('/etc/passwd')"),
    ("attach another db",     "ATTACH '/tmp/evil.db' AS evil"),
    ("create as select",      "CREATE TABLE leak AS SELECT * FROM fact_transactions"),
    ("nonexistent column",    "SELECT nope_col FROM fact_transactions"),
]


@pytest.mark.parametrize("label,sql", ADVERSARIAL, ids=[a[0] for a in ADVERSARIAL])
def test_adversarial_blocked(label, sql):
    res = guard_or_result(sql, CXO, M, run_explain=False)
    assert not res.ok, f"{label!r} was NOT blocked — this is a security failure"
    assert res.failed_check and res.reason


def test_masked_column_blocked_for_analyst():
    res = guard_or_result(
        "SELECT terminal_id FROM fact_transactions", rbac.principal_for("analyst"), M,
        run_explain=False)
    assert not res.ok and "masked" in res.reason


def test_legitimate_query_passes():
    res = guard("""
        SELECT pm.method, count(*) AS n
        FROM fact_transactions t JOIN dim_payment_method pm ON pm.pm_id = t.pm_id
        GROUP BY 1
    """, CXO, M)
    assert res.ok
    assert res.checks_passed[-1] == "c9_rbac_present"
    assert len(res.checks_passed) == 9


def test_sentinel_is_max_rows_plus_one():
    """max_rows exactly would make 'at the cap' and 'over the cap' indistinguishable."""
    res = guard("SELECT txn_id FROM fact_transactions", CXO, M, max_rows=100)
    assert res.sentinel_limit == 101
    assert "101" in res.sql


def test_rbac_predicate_injected_for_merchant():
    res = guard("SELECT count(*) AS n FROM fact_transactions", MERCHANT, M)
    assert res.ok
    assert "merchant_id" in res.sql and "M-1042" in res.sql
    assert res.injected_predicates


def test_rbac_injected_into_query_that_already_has_where():
    res = guard(
        "SELECT count(*) AS n FROM fact_transactions WHERE status = 'SUCCESS'", MERCHANT, M)
    assert res.ok and "M-1042" in res.sql and "SUCCESS" in res.sql


def test_rbac_injected_inside_cte():
    res = guard("""
        WITH t AS (SELECT * FROM fact_transactions WHERE status='FAILED')
        SELECT count(*) AS n FROM t
    """, MERCHANT, M)
    assert res.ok and "M-1042" in res.sql


def test_regional_scope_injects_region_predicate():
    res = guard("""
        SELECT count(*) AS n FROM fact_transactions t
        JOIN dim_merchant m ON m.merchant_id = t.merchant_id
    """, REGIONAL, M)
    assert res.ok and "'South'" in res.sql


def test_missing_claim_is_refused_not_ignored():
    res = guard_or_result("SELECT count(*) AS n FROM fact_transactions",
                          rbac.principal_for("merchant_user", {}), M, run_explain=False)
    assert not res.ok and "claim" in res.reason


def test_check9_catches_neutralised_predicate():
    """
    Simulates the exact escape a 'helpful' self-heal can introduce: the predicate is
    present but disjoined with OR 1=1, so it constrains nothing. Check 6 would be
    satisfied by string presence; check 9 is not.
    """
    import sqlglot
    from backend.core.rbac import assert_injected, row_predicates
    preds = row_predicates(MERCHANT, M)
    neutralised = sqlglot.parse_one(
        "SELECT count(*) FROM fact_transactions "
        "WHERE (fact_transactions.merchant_id = 'M-1042' OR 1=1)", read="duckdb")
    ok, why = assert_injected(neutralised, preds)
    assert not ok and "top-level conjunct" in why


def test_check9_accepts_proper_conjunct():
    import sqlglot
    from backend.core.rbac import assert_injected, row_predicates
    preds = row_predicates(MERCHANT, M)
    good = sqlglot.parse_one(
        "SELECT count(*) FROM fact_transactions "
        "WHERE status='SUCCESS' AND fact_transactions.merchant_id = 'M-1042'", read="duckdb")
    ok, _ = assert_injected(good, preds)
    assert ok


def test_expensive_query_blocked_by_cost_estimate():
    from backend import config
    orig = config.EXPLAIN_SCAN_LIMIT
    config.EXPLAIN_SCAN_LIMIT = 1000          # force the cost path
    try:
        res = guard_or_result("SELECT * FROM fact_transactions", CXO, M)
        assert not res.ok and res.failed_check == "c8_cost"
        assert "Narrow the time window" in res.reason
    finally:
        config.EXPLAIN_SCAN_LIMIT = orig


# ── regression: scope leak when the scoped dimension's table is not joined ───
# The original implementation injected the region filter only into SELECTs that
# referenced dim_merchant. A query like "success rate by payment method" never
# joins it, so a regional manager received EVERY region's data with no predicate
# at all — and the earlier test passed only because its SQL happened to join
# dim_merchant. These tests use queries that deliberately do not.

REGIONAL_SQL_NO_MERCHANT_JOIN = """
    SELECT pm.method, count(*) AS n
    FROM fact_transactions t
    JOIN dim_payment_method pm ON pm.pm_id = t.pm_id
    GROUP BY 1
"""


def test_region_scope_applies_even_when_dim_merchant_is_not_joined():
    res = guard(REGIONAL_SQL_NO_MERCHANT_JOIN, REGIONAL, M)
    assert res.ok
    assert "'South'" in res.sql, "regional manager was served unscoped rows"
    assert "dim_merchant" in res.sql, "no semi-join to the scoping table"
    assert res.injected_predicates


def test_region_scope_is_a_semijoin_on_the_fact_table():
    from backend.core.rbac import row_predicates
    pred = row_predicates(REGIONAL, M)[0]
    assert pred.is_semijoin
    assert pred.table == "fact_transactions"      # anchored where it is always present
    assert pred.lookup_table == "dim_merchant"
    assert "IN (SELECT" in pred.sql("t")


def test_merchant_scope_stays_a_direct_equality():
    """merchant_id is on the fact table, so it needs no subquery."""
    from backend.core.rbac import row_predicates
    pred = row_predicates(MERCHANT, M)[0]
    assert not pred.is_semijoin
    assert pred.sql("t") == "t.merchant_id = 'M-1042'"


def test_check9_accepts_the_semijoin_form():
    """Check 9 must recognise IN (SELECT ...), not only equality."""
    import sqlglot
    from backend.core.rbac import assert_injected, row_predicates
    preds = row_predicates(REGIONAL, M)
    ast = sqlglot.parse_one(guard(REGIONAL_SQL_NO_MERCHANT_JOIN, REGIONAL, M).sql,
                            read="duckdb")
    ok, why = assert_injected(ast, preds)
    assert ok, why


def test_check9_rejects_a_stripped_semijoin():
    """If the semi-join is removed, check 9 must notice."""
    import sqlglot
    from backend.core.rbac import assert_injected, row_predicates
    preds = row_predicates(REGIONAL, M)
    stripped = sqlglot.parse_one(
        "SELECT count(*) FROM fact_transactions WHERE status = 'SUCCESS'", read="duckdb")
    ok, why = assert_injected(stripped, preds)
    assert not ok and "missing" in why.lower()
