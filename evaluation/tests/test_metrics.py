"""
Fixture tests for the evaluation metrics.

Every metric is exercised on hand-built results where the right answer is
obvious by inspection, plus the degenerate empty case — a harness that reports
1.000 on an empty set is worse than one that crashes, because the number looks
like a pass.
"""
from __future__ import annotations

import pytest

from evaluation.metrics import paylens_metrics as pm
from evaluation.metrics.ex_accuracy import ex_score, ex_score_numeric
from evaluation.metrics.healing import compute_healing_metrics
from evaluation.metrics.latency import compute_latency_percentiles
from evaluation.metrics.ves import ves_static
from evaluation.pipelines.base import EvalResult


def mk(**kw) -> EvalResult:
    base = dict(question="q", pipeline_name="t", question_id=kw.pop("qid", "q1"))
    base.update(kw)
    return EvalResult(**base)


# ── the empty set must never read as a pass ─────────────────────────────────
@pytest.mark.parametrize("fn", [
    pm.action_routing, pm.metric_formula_fidelity, pm.schema_precision,
    pm.rbac_enforcement, pm.grounding, pm.failure_honesty, pm.clarification,
    pm.silent_empty, pm.decomposition_identity, pm.dq_surfacing, pm.cost,
])
def test_empty_input_reports_none_not_zero(fn):
    out = fn([])
    assert out, "a metric must still return its keys on an empty set"
    assert all(v is None or v == 0 or isinstance(v, (dict, list))
               for v in out.values())
    rates = [k for k in out if k.endswith(("_rate", "_accuracy", "precision",
                                           "recall", "_f1"))]
    for k in rates:
        assert out[k] is None, f"{fn.__name__}.{k} should be None, got {out[k]}"


# ── B.1 action routing ──────────────────────────────────────────────────────
def test_action_routing_counts_the_definitional_defect():
    rs = [
        mk(qid="a", expected_action="explain", action="sql"),     # the defect
        mk(qid="b", expected_action="explain", action="explain"),
        mk(qid="c", expected_action="sql", action="sql"),
        mk(qid="d", expected_action="refuse", action="sql"),      # breach
    ]
    out = pm.action_routing(rs)
    assert out["action_routing_accuracy"] == 0.5
    assert out["confusion"]["explain"]["sql"] == 1
    assert out["confusion"]["refuse"]["sql"] == 1
    assert out["per_action"]["explain"]["recall"] == 0.5


# ── B.2 metric formula fidelity ─────────────────────────────────────────────
def test_mff_separates_first_shot_from_healed():
    good = mk(qid="g", measured=True, sql="select x as success_rate from v",
              checks_passed=["c10d_metric_formula"],
              attempts=[{"stage": "ok", "ok": True}])
    healed = mk(qid="h", measured=True, sql="select x as success_rate from v",
                checks_passed=["c10d_metric_formula"],
                attempts=[{"stage": "c10d_metric_formula", "ok": False},
                          {"stage": "ok", "ok": True}])
    out = pm.metric_formula_fidelity([good, healed])
    assert out["n_relevant"] == 2
    assert out["mff_final"] == 1.0          # both ended correct
    assert out["mff_first_shot"] == 0.5     # only one got it unaided
    assert out["n_rejected_once"] == 1


def test_mff_ignores_queries_that_claim_no_metric():
    out = pm.metric_formula_fidelity(
        [mk(measured=True, sql="select count(*) as n from v_attempts")])
    assert out["n_relevant"] == 0 and out["mff_final"] is None


# ── B.3 schema precision ────────────────────────────────────────────────────
def test_schema_precision_flags_raw_tables():
    out = pm.schema_precision([
        mk(qid="v", sql="select 1 from v_attempts"),
        mk(qid="r", sql="select 1 from fact_transactions"),
        mk(qid="d", sql="select 1 from dim_bank join v_attempts on 1=1"),
    ])
    assert out["schema_precision"] == pytest.approx(1 / 3, abs=1e-4)
    assert sorted(out["offender_ids"]) == ["d", "r"]


# ── B.4 RBAC ────────────────────────────────────────────────────────────────
def test_rbac_enforcement_requires_a_predicate():
    out = pm.rbac_enforcement([
        mk(qid="ok", role="merchant_user", measured=True,
           sql="SELECT 1 FROM v_attempts WHERE merchant_id = 'M-1042'",
           injected_predicates=["merchant_id = 'M-1042'"],
           checks_passed=["c9_rbac_present"]),
        mk(qid="bad", role="merchant_user", measured=True,
           sql="SELECT sum(net_amount) FROM v_settlements",
           injected_predicates=[]),
        mk(qid="cxo", role="cxo", measured=True, sql="SELECT 1 FROM v_attempts"),
        # Never built, so it cannot have had a predicate injected: a build
        # failure, not a scope failure, and excluded from the denominator.
        mk(qid="nobuild", role="merchant_user", measured=True, sql=""),
    ])
    assert out["n_scoped"] == 2
    assert out["rbac_enforcement"] == 0.5
    assert out["missing_ids"] == ["bad"]
    # The diagnosis is the table, not the count: a scope declared against one
    # view leaves every other view in the allowlist unprotected.
    assert out["unscoped_tables"] == {"v_settlements": 1}


def test_strip_predicates_removes_the_conjunct():
    sql = ("SELECT count(*) FROM v_attempts WHERE txn_ts >= DATE '2026-11-09' "
           "AND merchant_id = 'M-1042'")
    out = pm._strip_predicates(sql, ["merchant_id = 'M-1042'"])
    assert out is not None and "M-1042" not in out
    assert "txn_ts" in out, "the rest of the query must survive"


def test_strip_predicates_handles_a_leading_where():
    sql = "SELECT 1 FROM v_attempts WHERE merchant_id = 'M-1042' AND x = 1"
    out = pm._strip_predicates(sql, ["merchant_id = 'M-1042'"])
    assert out is not None and "M-1042" not in out and "x = 1" in out


def test_neutralise_keeps_the_text_and_removes_the_filter():
    """The tamper a textual check cannot catch."""
    sql = "SELECT 1 FROM v_attempts WHERE merchant_id = 'M-1042'"
    out = pm._neutralise_predicates(sql, ["merchant_id = 'M-1042'"])
    assert out is not None
    assert "M-1042" in out, "the predicate is still textually present"
    assert "OR 1 = 1" in out


def test_leak_test_blocks_both_tampers():
    """
    The claim this harness exists to make: tamper with the scope, get refused.

    Both tampers must fail check 9 — one deleted, one neutralised — so a clean
    query yields two blocks and no leaks.
    """
    sql = ("SELECT count(*) AS n FROM v_attempts "
           "WHERE txn_ts >= DATE '2026-11-09' AND merchant_id = 'M-1042'")
    out = pm.rbac_leak_resistance(
        [mk(role="merchant_user", sql=sql,
            injected_predicates=["merchant_id = 'M-1042'"])],
        role_claims={"merchant_user": {"merchant_id": "M-1042"}})
    assert out["n_leak_tests"] == 2, out
    assert out["n_leaked"] == 0, out
    assert out["rbac_leak_blocked"] == 1.0, out


def test_leak_test_reports_a_genuine_leak():
    """An unscoped query must be reported as leaking, not silently skipped."""
    out = pm.rbac_leak_resistance(
        [mk(role="merchant_user",
            sql="SELECT count(*) AS n FROM v_attempts WHERE x = 1",
            injected_predicates=["merchant_id = 'M-1042'"])],
        role_claims={"merchant_user": {"merchant_id": "M-1042"}})
    # Nothing to strip, so both tampers are skipped — and a skip is never
    # counted as a block it did not earn.
    assert out["n_leak_tests"] == 0 and out["n_leak_skipped"] == 2, out


# ── B.5 grounding ───────────────────────────────────────────────────────────
def test_grounding_rate_excludes_not_applicable_from_the_denominator():
    rs = [
        mk(grounding={"verdict": "grounded", "numbers_checked": 4,
                      "ungrounded": []}),
        mk(qid="x", grounding={"verdict": "rejected", "numbers_checked": 4,
                               "ungrounded": ["2.5"]}),
        mk(grounding={"verdict": "not_applicable", "numbers_checked": 0,
                      "ungrounded": []}),
    ]
    out = pm.grounding(rs)
    assert out["n_grounded_answers"] == 3 and out["n_decided"] == 2
    assert out["grounding_rate"] == 0.5
    assert out["ungrounded_number_rate"] == pytest.approx(1 / 8)
    assert out["offender_ids"] == ["x"]


# ── B.7 failure honesty ─────────────────────────────────────────────────────
def test_false_refusal_is_scored_against_answerable_questions_only():
    rs = [
        mk(qid="fr", expected_action="sql", refused=True, failure_kind="data"),
        mk(qid="ok", expected_action="sql", success=True),
        mk(qid="good_refuse", expected_action="refuse", refused=True,
           failure_kind="data"),
    ]
    out = pm.failure_honesty(rs)
    assert out["false_refusal_rate"] == 0.5     # 1 of the 2 answerable
    assert out["false_refusal_ids"] == ["fr"]
    assert out["refusal_recall"] == 1.0


# ── B.8 clarification ───────────────────────────────────────────────────────
def test_clarification_precision_catches_over_asking():
    rs = [
        mk(expected_action="clarify", needs_clarification=True),
        mk(expected_action="sql", needs_clarification=True),   # over-asked
        mk(expected_action="clarify", needs_clarification=False),  # missed
    ]
    out = pm.clarification(rs)
    assert out["clarification_precision"] == 0.5
    assert out["clarification_recall"] == 0.5
    assert out["clarification_f1"] == 0.5


# ── B.9 silent empty ────────────────────────────────────────────────────────
def test_silent_empty_only_counts_unannounced_zeroes():
    rs = [
        mk(qid="silent", success=True, measured=True, row_count=0,
           answer="The success rate was steady."),
        mk(qid="honest", success=True, measured=True, row_count=0,
           answer="No attempts matched this window."),
    ]
    out = pm.silent_empty(rs)
    assert out["n_empty"] == 2
    assert out["silent_ids"] == ["silent"]
    assert out["silent_empty_rate"] == 0.5


# ── B.10 decomposition identity ─────────────────────────────────────────────
def test_identity_breach_is_detected():
    rs = [
        mk(decomposition={"delta_pp": -1.33, "mix_pp": -1.23, "rate_pp": -0.10,
                          "residual_pp": 1.7e-15}),
        mk(qid="bad", decomposition={"delta_pp": -1.33, "mix_pp": -1.0,
                                     "rate_pp": -0.1, "residual_pp": -0.23}),
    ]
    out = pm.decomposition_identity(rs)
    assert out["identity_hold_rate"] == 0.5
    assert out["breached_ids"] == ["bad"]


def test_identity_is_recomputed_when_residual_is_absent():
    out = pm.decomposition_identity(
        [mk(decomposition={"delta_pp": -1.0, "mix_pp": -0.5, "rate_pp": -0.1})])
    assert out["max_residual_pp"] == pytest.approx(0.4)
    assert out["identity_hold_rate"] == 0.0


# ── healing metrics must ignore the non-measuring branches ──────────────────
def test_healing_excludes_branches_that_never_authored_sql():
    """
    An explain/refuse/clarify answer authored no SQL, so it can neither have
    needed healing nor earned a first-shot win. Including it would hand the
    healing numbers free passes.
    """
    measured = [mk(measured=True, success=True, first_shot_success=True),
                mk(measured=True, success=True, first_shot_success=False,
                   heal_attempts=1)]
    non_measuring = mk(measured=False, success=True, first_shot_success=True)
    scoped = [r for r in measured + [non_measuring] if r.measured]
    out = compute_healing_metrics(scoped)
    assert out["n_total"] == 2
    assert out["n_healed_attempted"] == 1
    assert out["healing_efficiency"] == 1.0


# ── ported modules still behave ─────────────────────────────────────────────
def test_ex_score_is_row_order_insensitive():
    assert ex_score([{"a": "x"}, {"a": "y"}], [{"a": "y"}, {"a": "x"}]) == 1.0


def test_ex_numeric_handles_percent_against_ratio():
    assert ex_score_numeric([{"m": "UPI", "sr": 88.79}],
                            [{"k": "UPI", "v": 0.887887}],
                            gold_scale="ratio") == 1.0
    assert ex_score_numeric([{"m": "UPI", "sr": 2.5}],
                            [{"k": "UPI", "v": 0.887887}],
                            gold_scale="ratio") == 0.0


def test_ves_static_rewards_a_clean_v2_query_and_penalises_a_raw_one():
    clean = ("SELECT payment_method, (count(*) filter (where is_success))::DOUBLE"
             " / nullif(count(*) filter (where is_resolved), 0) AS success_rate "
             "FROM v_attempts WHERE txn_ts >= DATE '2026-11-09' "
             "GROUP BY payment_method")
    assert ves_static(clean) == 1.0
    assert ves_static("SELECT * FROM fact_transactions") < 0.6


def test_latency_percentiles_on_a_single_result():
    out = compute_latency_percentiles([mk(latency_ms=1234.0)])
    assert out["p50"] == 1234.0 and out["p95"] == 1234.0
