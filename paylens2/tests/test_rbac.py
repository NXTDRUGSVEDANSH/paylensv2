"""
The security test this whole design exists to pass.

rbac.assert_injected (guard check 9) SKIPS tables the query does not name:

    targets = _selects_touching(root, pred.table)
    if not targets: continue

The classic semantic model anchors every role scope on fact_transactions. A v2
query reads v_attempts and names no fact_transactions — so under the classic
model no row filter would be injected AND check 9 would pass anyway. A
merchant_user would silently read every merchant's rows.

paylens2/schema/views.yaml anchors the scopes on v_attempts instead. These tests
assert that it worked, and the first one asserts the hole exists so that nobody
later "simplifies" v2 back onto the classic model without the test going red.
"""
from __future__ import annotations

import pytest

from backend.core import rbac
from backend.core.guard import GuardBlocked, guard
from paylens2.core.model import get_model, views_present
from semantic.loader import load as load_classic

pytestmark = pytest.mark.skipif(
    not views_present(), reason="v2 views not built: python -m paylens2.schema.build")

SQL = ("SELECT payment_method, count(*) AS attempts "
       "FROM v_attempts GROUP BY payment_method")


def test_classic_model_would_not_scope_a_view_query():
    """The hole, demonstrated. If this ever fails, the guard changed — rejoice."""
    classic = load_classic()
    p = rbac.principal_for("merchant_user", {"merchant_id": "M-1042"})
    preds = rbac.row_predicates(p, classic)
    assert preds, "merchant_user must have a row predicate"
    assert preds[0].table == "fact_transactions"
    # The query names no fact_transactions, so nothing is injected...
    import sqlglot
    root = sqlglot.parse_one(SQL, read="duckdb")
    _, applied = rbac.inject(root, preds)
    assert applied == []
    # ...and check 9 passes regardless. This is why v2 has its own model.
    ok, _ = rbac.assert_injected(root, preds)
    assert ok is True


def test_v2_model_injects_the_merchant_filter():
    p = rbac.principal_for("merchant_user", {"merchant_id": "M-1042"})
    g = guard(SQL, p, get_model())
    assert g.ok
    assert "c9_rbac_present" in g.checks_passed
    assert any("merchant_id" in s for s in g.injected_predicates)
    assert "M-1042" in g.sql


def test_v2_model_injects_the_region_filter():
    p = rbac.principal_for("regional_manager", {"region": "South"})
    g = guard(SQL, p, get_model())
    assert g.ok
    assert "'South'" in g.sql


def test_cxo_is_unscoped():
    g = guard(SQL, rbac.principal_for("cxo", {}), get_model())
    assert g.ok
    assert g.injected_predicates == []


def test_missing_claim_is_refused_not_defaulted():
    """An absent claim must fail, never silently widen to every merchant."""
    p = rbac.principal_for("merchant_user", {})
    with pytest.raises(PermissionError):
        rbac.row_predicates(p, get_model())


def test_or_1_eq_1_cannot_neutralise_the_predicate():
    """
    The escape check 9 exists for: a predicate present but disarmed by an OR.
    Injection appends with AND, so to test the check we hand it SQL that already
    contains a neutralised filter and assert the conjunct test is not fooled.
    """
    import sqlglot
    p = rbac.principal_for("merchant_user", {"merchant_id": "M-1042"})
    preds = rbac.row_predicates(p, get_model())
    sneaky = ("SELECT count(*) AS n FROM v_attempts "
              "WHERE (merchant_id = 'M-1042' OR 1 = 1)")
    root = sqlglot.parse_one(sneaky, read="duckdb")
    ok, why = rbac.assert_injected(root, preds)
    assert ok is False and "top-level conjunct" in why


def test_merchant_user_cannot_read_a_masked_column():
    p = rbac.principal_for("merchant_user", {"merchant_id": "M-1042"})
    with pytest.raises(GuardBlocked) as e:
        guard("SELECT merchant_name, count(*) AS n FROM v_attempts GROUP BY 1",
              p, get_model())
    assert e.value.check == "c5_column_allowlist"


def test_raw_tables_are_not_in_the_v2_allowlist():
    p = rbac.principal_for("cxo", {})
    with pytest.raises(GuardBlocked) as e:
        guard("SELECT count(*) AS n FROM fact_transactions", p, get_model())
    assert e.value.check == "c4_table_allowlist"
