"""The profiler computes; the model does not. And the grounding check proves it."""
from __future__ import annotations

from paylens2.core.profile import Fact, profile
from paylens2.core.synth import verify

ROWS = [{"issuer_bank": "HDFC Bank", "success_rate": 0.9012, "attempts": 12043},
        {"issuer_bank": "Fino Payments Bank", "success_rate": 0.7606, "attempts": 3473},
        {"issuer_bank": "SBI", "success_rate": 0.8711, "attempts": 20110}]


def test_profile_is_deterministic():
    a = profile(ROWS, metric_columns=["success_rate", "attempts"])
    b = profile(ROWS, metric_columns=["success_rate", "attempts"])
    assert a.text == b.text
    assert [f.value for f in a.facts] == [f.value for f in b.facts]


def test_facts_name_the_segment_not_just_the_number():
    p = profile(ROWS, metric_columns=["success_rate"])
    labels = " ".join(f.label for f in p.facts)
    assert "Fino Payments Bank" in labels and "HDFC Bank" in labels


def test_empty_result_is_a_finding_not_an_error():
    p = profile([])
    assert p.row_count == 0
    assert "no rows" in p.text
    assert p.facts and p.facts[0].value == "0"


def test_grounded_prose_passes():
    p = profile(ROWS, metric_columns=["success_rate", "attempts"])
    text = "Fino Payments Bank is the weakest issuer at 0.7606 [E3]."
    g = verify(text, p.facts, p, "success rate by issuer")
    assert g.verdict == "grounded", g.note


def test_an_invented_benchmark_is_rejected():
    """The output this check exists to catch."""
    p = profile(ROWS, metric_columns=["success_rate", "attempts"])
    text = ("Fino is weakest at 0.7606 [E3], against an industry average of 94.3%.")
    g = verify(text, p.facts, p, "success rate by issuer")
    assert g.verdict == "rejected"
    assert any("94.3" in u for u in g.ungrounded)


def test_a_citation_to_nothing_is_rejected():
    p = profile(ROWS, metric_columns=["success_rate"])
    g = verify("Attempts totalled 35,626 [E99].", p.facts, p, "q")
    assert g.verdict == "rejected"
    assert g.bad_citations == ["E99"]


def test_small_integers_are_not_treated_as_claims():
    p = profile(ROWS, metric_columns=["success_rate"])
    g = verify("Three issuers carried volume this week.", p.facts, p, "q")
    assert g.verdict == "grounded"


def test_rounding_is_allowed_but_invention_is_not():
    facts = [Fact(id="E1", label="Success rate", value="86.60%", raw=86.60)]
    p = profile(ROWS)
    p.facts = facts
    assert verify("It was 86.6%.", facts, p, "q").verdict == "grounded"
    assert verify("It was 91.2%.", facts, p, "q").verdict == "rejected"
