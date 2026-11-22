"""
The advisory path: business questions reach the catalogue, and the advice is
verified the way the numbers are.
"""
from __future__ import annotations

import pytest

from paylens2.core import catalogue
from paylens2.core.author import Authored, _coerce, validate
from paylens2.core.profile import Fact, Profile, combine, profile
from paylens2.core.synth import verify_levers

LEVERS = [
    {"lever": "Retry technical failures immediately; they are transient by definition.",
     "owner": "Payment Ops", "why": "w", "measure": "technical declines last week"},
    {"lever": "Never retry wrong PIN, blocked cards or expired cards.",
     "owner": "Payment Ops", "why": "w2", "measure": "failures by code last week"},
]


# ── call 1 returns advice ────────────────────────────────────────────────────
def test_the_advise_action_is_parsed():
    a = _coerce({"action": "advise", "playbook_ids": ["retry_strategy"],
                 "evidence_queries": [
                     {"id": "q1", "purpose": "retriable split",
                      "sql": "SELECT count(*) AS n FROM v_attempts"}],
                 "assumptions": ["last 30 days"]})
    assert a.action == "advise"
    assert a.playbook_ids == ["retry_strategy"]
    assert a.evidence_queries[0]["purpose"] == "retriable split"
    assert a.assumptions == ["last 30 days"]


def test_evidence_queries_are_capped():
    a = _coerce({"action": "advise", "playbook_ids": ["retry_strategy"],
                 "evidence_queries": [{"id": f"q{i}", "sql": f"SELECT {i} AS n"}
                                      for i in range(9)]})
    assert len(a.evidence_queries) == 4


def test_advice_with_no_catalogue_match_degrades_to_a_measurement():
    """
    Advice with nothing behind it would be the model's own opinion, which is
    what the catalogue exists to prevent. Answering the evidence query is a
    smaller, honest result.
    """
    a = validate(_coerce({"action": "advise", "playbook_ids": ["not_a_playbook"],
                          "evidence_queries": [
                              {"id": "q1", "sql": "SELECT count(*) AS n FROM v_attempts"}]}))
    assert a.action == "sql"
    assert a.sql.startswith("SELECT count(*)")
    assert any("no catalogue entry" in n for n in a.notes)


def test_advice_with_no_evidence_is_not_served():
    a = validate(Authored(action="advise", playbook_ids=["retry_strategy"]))
    assert a.action == "reauthor"


def test_an_action_is_inferred_when_the_model_omits_it():
    a = _coerce({"playbook_ids": ["retry_strategy"],
                 "evidence_queries": [{"id": "q1", "sql": "SELECT 1 AS n"}]})
    assert a.action == "advise"


# ── the catalogue ────────────────────────────────────────────────────────────
def test_every_lever_carries_an_owner_and_a_way_to_check_it():
    """Advice with no owner never gets actioned; with no measure, never verified."""
    all_ids = [s["id"] for s in catalogue.summaries()]
    levers = catalogue.levers(all_ids)
    assert levers, "the catalogue must expose levers"
    assert all(l["owner"] for l in levers)
    assert all(l["measure"] for l in levers)


def test_framing_and_limits_resolve():
    assert catalogue.framing(["retry_strategy"])
    assert isinstance(catalogue.limits(["retry_strategy"]), list)


# ── lever verification — the check numbers cannot do ─────────────────────────
def test_an_invented_recommendation_is_dropped():
    """
    "Enable network tokenisation" contains no number, so the grounding check
    waves it through. It is advice this system has no basis for, aimed at a
    team nobody named.
    """
    kept, invented = verify_levers(
        [{"lever": "Retry technical failures immediately", "why_here": "x"},
         {"lever": "Enable network tokenisation across all cards", "why_here": "y"}],
        LEVERS)
    assert len(kept) == 1
    assert invented == ["Enable network tokenisation across all cards"]


def test_a_kept_lever_regains_its_full_text_and_owner():
    kept, _ = verify_levers([{"lever": "Retry technical failures immediately"}], LEVERS)
    assert kept[0]["owner"] == "Payment Ops"
    assert kept[0]["lever"] == LEVERS[0]["lever"]
    assert kept[0]["check_with"] == "technical declines last week"


def test_an_empty_recommendation_is_ignored_not_counted_as_invented():
    kept, invented = verify_levers([{"lever": "", "why_here": "x"}], LEVERS)
    assert kept == [] and invented == []


# ── the merged fact pack ─────────────────────────────────────────────────────
def test_several_queries_merge_into_one_numbered_pack():
    p1 = profile([{"rail": "UPI", "attempts": 10}], metric_columns=["attempts"])
    p2 = profile([{"code": "NO_FUNDS", "n": 5}], metric_columns=["n"])
    merged = combine([("where volume sits", p1), ("top failure codes", p2)])
    ids = [f.id for f in merged.facts]
    assert ids == [f"E{i + 1}" for i in range(len(ids))], "ids must be contiguous"
    assert any("where volume sits" in f.detail for f in merged.facts)
    assert any("top failure codes" in f.detail for f in merged.facts)


def test_per_query_row_counts_are_not_offered_as_evidence():
    """'Rows returned' is bookkeeping; a pack full of it invites useless citations."""
    p1 = profile([{"a": 1}], metric_columns=["a"])
    p2 = profile([{"b": 2}], metric_columns=["b"])
    merged = combine([("one", p1), ("two", p2)])
    assert not any(f.label == "Rows returned" for f in merged.facts)


def test_a_single_query_keeps_its_row_count_fact():
    merged = combine([("only", profile([{"a": 1}], metric_columns=["a"]))])
    assert any(f.label == "Rows returned" for f in merged.facts)


# ── presentation ────────────────────────────────────────────────────────────
def test_run_on_numbers_are_separated():
    from paylens2.core.synth import tidy
    assert tidy("dropped to86.60% last week") == "dropped to 86.60% last week"
    assert tidy("is87.38% [E1]") == "is 87.38% [E1]"


def test_tidy_leaves_identifiers_and_codes_alone():
    from paylens2.core.synth import tidy
    for s in ["merchant M-1042 processed 1,473", "3DS2 flows", "UPI at 88.79%"]:
        assert tidy(s) == s


def test_a_ratio_is_shown_as_a_percentage():
    """`0.8660` is right and unreadable. The declared format makes it 86.60%."""
    p = profile([{"rail": "UPI", "success_rate": 0.8879}],
                metric_columns=["success_rate"], formats={"success_rate": "percent"})
    assert any("88.79%" == f.value for f in p.facts)


def test_currency_is_rupees_not_dollars():
    p = profile([{"rail": "UPI", "gmv": 1234567.0}],
                metric_columns=["gmv"], formats={"gmv": "currency"})
    assert any(f.value.startswith("INR") for f in p.facts)
    assert not any("$" in f.value for f in p.facts)


def test_a_two_row_comparison_gets_its_change_computed():
    """
    "Why did it drop" needs the delta. Without it the composer either omits the
    point of the question or subtracts the numbers itself and is rejected for
    an ungrounded figure — so Python does the subtraction.
    """
    p = profile([{"period": "baseline", "success_rate": 0.8793},
                 {"period": "target", "success_rate": 0.8660}],
                metric_columns=["success_rate"], formats={"success_rate": "percent"})
    vals = {f.value for f in p.facts}
    assert "87.93%" in vals and "86.60%" in vals
    assert "-1.33pp" in vals, "the change must be stated in percentage points"
    assert any("percentage points, not percent" in f.detail for f in p.facts)


# ── malformed responses are re-authored, never reported as missing data ─────
def test_a_clarification_with_no_options_asks_the_model_again():
    """
    This reached a user as "This dataset does not hold a clear enough question."
    The model had failed to produce usable options; nothing about the data was
    involved. It must now come back as a re-author, not a refusal.
    """
    a = validate(Authored(action="clarify", clarify_question="which?", options=[]))
    assert a.action == "reauthor"
    assert "complete question" in a.reauthor_reason.lower()


def test_empty_sql_asks_the_model_again():
    a = validate(Authored(action="sql", sql=""))
    assert a.action == "reauthor" and a.reauthor_reason


def test_advice_with_no_evidence_asks_the_model_again():
    a = validate(Authored(action="advise", playbook_ids=["retry_strategy"]))
    assert a.action == "reauthor"


def test_a_refusal_naming_something_real_asks_the_model_again():
    a = validate(Authored(action="refuse", missing_concept="issuer_bank"))
    assert a.action == "reauthor"
    assert "it does" in a.reauthor_reason


def test_a_genuine_refusal_survives():
    """Only this may tell the user something is absent from their data."""
    a = validate(Authored(action="refuse", missing_concept="chargebacks"))
    assert a.action == "refuse"
    assert not a.reauthor_reason


def test_no_internal_failure_can_produce_a_data_refusal():
    """A sweep: none of the degradations may leave action == 'refuse'."""
    for bad in (Authored(action="clarify", options=[]),
                Authored(action="sql", sql=""),
                Authored(action="advise", playbook_ids=["retry_strategy"]),
                Authored(action="advise")):
        assert validate(bad).action != "refuse"
