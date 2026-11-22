"""
A measured number in a false sentence.

Every one of these came from a real answer. The numbers were all in the fact
pack and the grounding check passed them, because it asked "was this measured"
and never "is this claim true".
"""
from __future__ import annotations

from paylens2.core.profile import profile
from paylens2.core.synth import verify, verify_superlatives

ROWS = [
    {"payment_method": "UPI", "success_rate": 0.8879, "attempts": 26938},
    {"payment_method": "CARD", "success_rate": 0.9006, "attempts": 10775},
    {"payment_method": "NETBANKING", "success_rate": 0.7113, "attempts": 7347},
    {"payment_method": "WALLET", "success_rate": 0.9502, "attempts": 2448},
    {"payment_method": "EMI", "success_rate": 0.8454, "attempts": 1468},
]


def pack():
    return profile(ROWS, metric_columns=["success_rate"],
                   formats={"success_rate": "percent"})


def test_the_headline_that_shipped_is_rejected():
    """WALLET is the highest at 95.02%. This said UPI, and both figures were real."""
    p = pack()
    bad = verify_superlatives(
        "UPI had the highest success rate at 88.79% [E2].", p.facts, p)
    assert bad and "WALLET" in bad[0]


def test_a_sentence_that_contradicts_itself():
    bad = verify_superlatives(
        "UPI was highest at 88.79%, followed by EMI.", (p := pack()).facts, p)
    assert bad


def test_a_decimal_does_not_hide_the_claim():
    """
    The first splitter broke on every full stop, so 88.79% cut the sentence in
    two and neither half held both the segment and the superlative.
    """
    bad = verify_superlatives(
        "UPI, at 88.79%, was the highest performer.", (p := pack()).facts, p)
    assert bad and "WALLET" in bad[0]


def test_the_wrong_lowest_is_caught_too():
    bad = verify_superlatives(
        "Volumes varied. EMI posted the lowest rate at 84.54%.",
        (p := pack()).facts, p)
    assert bad and "NETBANKING" in bad[0]


def test_true_claims_pass():
    p = pack()
    for t in ("WALLET had the highest success rate at 95.02% [E2].",
              "NETBANKING was the lowest at 71.13% [E3].",
              "UPI processed 26,938 attempts at 88.79%."):
        assert verify_superlatives(t, p.facts, p) == [], t


def test_a_sentence_naming_both_ends_is_left_alone():
    """Conservative by design: a false rejection discards a good answer."""
    p = pack()
    assert verify_superlatives(
        "Rates run from the lowest at NETBANKING to the highest at WALLET.",
        p.facts, p) == []


def test_verify_rejects_the_whole_composition():
    p = pack()
    g = verify("UPI had the highest success rate at 88.79% [E2].",
               p.facts, p, "success rate by payment method")
    assert g.verdict == "rejected"
    assert g.wrong_superlatives


def test_no_unweighted_mean_for_a_ratio():
    """
    The average of five rails' rates is not the rate: it weights 1,468
    attempts the same as 26,938. It read 85.91% where the truth was 86.60%,
    and a composition called it "the overall average" despite the label
    saying unweighted. The fact is no longer produced, so it cannot be stated.
    """
    p = pack()
    labels = " ".join(f.label for f in p.facts)
    assert "average across rows" not in labels
    assert not any("85.9" in f.value for f in p.facts)


def test_a_non_ratio_still_gets_its_average():
    """Only ratios lose it — an average latency across rows is meaningful."""
    rows = [{"issuer_bank": "A", "avg_latency_ms": 300.0},
            {"issuer_bank": "B", "avg_latency_ms": 500.0}]
    p = profile(rows, metric_columns=["avg_latency_ms"],
                formats={"avg_latency_ms": "number"})
    assert any("change" in f.label or "avg latency" in f.label for f in p.facts)
