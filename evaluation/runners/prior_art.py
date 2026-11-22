"""
The earlier project's published numbers, as data.

Frozen here so the report never retypes them and so the one figure that does
not reconcile is documented beside the figures that do.

Source: the Frammer AI / Fractal AI assignment report, "EVALUATION METRICS"
slide (p.15). Its harness lives at `~/Desktop/Projects/Fractal AI/evaluation/`.

TWO CAVEATS THAT TRAVEL WITH THESE NUMBERS
------------------------------------------
1. `track2b.ours.composite` is 69.6, but the weighted mean of its own four
   criteria is 0.30(79.2) + 0.25(86.0) + 0.30(77.8) + 0.15(77.4) = 80.2. The
   baseline row reconciles exactly (60.37 ≈ 60.4), so 69.6 is a transcription
   error rather than a different definition. Reported as published, flagged in
   the report.

2. `track1.ours.healed_ex` (44.40%) was produced with RESULT-AWARE healing:
   that pipeline's `run()` accepts `gold_rows` and re-prompts with the gold row
   count and gold column names on a mismatch. PayLens's heal loop sees only the
   rejection reason from check 10 and the guard. The two are not the same
   protocol and the report says so rather than presenting a lower PayLens
   figure as a regression.
"""
from __future__ import annotations

PRIOR_ART: dict = {
    "source": "Fractal AI / Frammer AI assignment report, slide 15",
    "track1": {
        "ours": {"ex": 0.900, "first_shot_ex": 0.880, "healed_ex": 0.444,
                 "ves": 0.028},
        "baseline": {"ex": 0.640, "first_shot_ex": 0.640, "healed_ex": None,
                     "ves": 0.019},
    },
    "track2a": {
        "ours": {"esr": 0.960, "healing_efficiency": 0.600, "p50": 5544,
                 "p95": 24987, "ves_static": 0.840},
        "baseline": {"esr": 0.750, "healing_efficiency": None, "p50": 2699,
                     "p95": 9172, "ves_static": 0.790},
    },
    "track2b": {
        "ours": {"temporal": 79.2, "schema": 86.0, "faithfulness": 77.8,
                 "completeness": 77.4, "composite": 69.6},
        "baseline": {"temporal": 65.8, "schema": 61.9, "faithfulness": 56.0,
                     "completeness": 55.7, "composite": 60.4},
    },
    "notes": {
        "composite_mismatch": "ours composite 69.6 != weighted mean 80.2 of "
                              "its own criteria; baseline reconciles at 60.4",
        "healed_ex": "prior-art healed EX used result-aware healing against "
                     "gold rows and columns; PayLens does not",
        "judge": "prior art judged its own output with the same Qwen model it "
                 "authored with; PayLens uses a different vendor",
    },
}
