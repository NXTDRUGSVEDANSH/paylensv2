"""
NPCI calibration constants for the PayLens synthetic generator.

WHY THIS FILE EXISTS
--------------------
There is no public transaction-level payments data — real payment logs are private.
What NPCI publishes is monthly, bank-level aggregates: decline rates (split into
technical and business) and platform volumes. So the generator is synthetic at row
level but *calibrated* so its aggregates reproduce NPCI's published figures.

Every constant below is tagged with its provenance:

    [NPCI]      published NPCI figure, or reported directly from NPCI data
    [DERIVED]   arithmetic on an [NPCI] figure
    [MODELLED]  not published by NPCI; an industry-typical assumption, chosen so
                the volume-weighted aggregate reproduces an [NPCI] figure
    [ASSUMED]   a portfolio-shape choice with no published equivalent

Nothing here is presented as an NPCI figure unless it is tagged [NPCI].
Full discussion and source links: datasets/CALIBRATION.md

DEFINITIONS (NPCI's own, and they matter for the denominator)
------------------------------------------------------------
Technical decline (TD): failure on the system side — bank server down, network
    fault at the bank or at NPCI.
Business decline (BD): failure on the customer side — wrong PIN, insufficient
    funds, transaction over a stipulated limit.
Both are failures from a merchant's point of view, so:
    approval rate = 100% - BD - TD
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Published aggregates — the anchors everything else is scaled to reproduce
# ─────────────────────────────────────────────────────────────────────────────

# [NPCI] Top-20 remitter banks, September 2026, as reported from NPCI BD/TD data.
UPI_TECHNICAL_DECLINE_PCT = 0.24      # up from 0.18% in August 2026
UPI_BUSINESS_DECLINE_PCT = 10.88      # up from 10.85% in August 2026

# [DERIVED] UPI approval rate implied by the two figures above.
UPI_APPROVAL_PCT = 100.0 - UPI_BUSINESS_DECLINE_PCT - UPI_TECHNICAL_DECLINE_PCT  # 88.88%

# [NPCI] Named outliers in the same reporting period. These are pinned, not scaled.
ANCHORED_BD = {
    "Fino Payments Bank": 24.26,      # highest BD among the top 20
    "Airtel Payments Bank": 23.20,    # second highest
}
ANCHORED_TD = {
    "ICICI Bank": 0.73,               # highest TD among the top 20
}

# [NPCI] NPCI circular OC-149 (June 2022) service-level targets.
# Reported by a secondary source; NOT confirmed against the circular itself.
NPCI_TARGET_TD_PCT = 1.0
NPCI_TARGET_BD_PCT = 5.0

# ─────────────────────────────────────────────────────────────────────────────
# Platform volumes — NPCI Retail Payments Statistics (monthly, millions)
# Used for the relative standing of rails, not copied as absolute volume.
# ─────────────────────────────────────────────────────────────────────────────
NPCI_MONTHLY_VOLUME_MN = {
    "UPI": 24_256.95,                 # [NPCI] UPI - Unified Payments Interface
    "IMPS": 1_419.38,                 # [NPCI]
    "RuPay_POS": 192.13,              # [NPCI] RuPay card usage at (POS)
    "RuPay_ECOM": 82.64,              # [NPCI] RuPay card usage at (eCom)
    "BBPS": 307.90,                   # [NPCI]
    "AePS": 300.58,                   # [NPCI]
    "NFS_ATM": 988.44,                # [NPCI] NFS ATM cash withdrawal
}

# ─────────────────────────────────────────────────────────────────────────────
# Issuer banks
#
# Names are real. UPI remitter volume shares are [MODELLED] — NPCI publishes
# bank-wise decline rates but we are not copying its volume table, and a
# merchant acquirer's issuer mix differs from national remitter share anyway.
#
# `bd_shape` is a RELATIVE decline profile, not a published rate. At import time
# calibrate_declines() scales every non-anchored bank by a single factor so the
# volume-weighted mean lands exactly on UPI_BUSINESS_DECLINE_PCT, with the
# [NPCI]-anchored banks held fixed. So the aggregate and the known extremes are
# real; only the interior distribution is modelled.
# ─────────────────────────────────────────────────────────────────────────────
#  bank_id, name, is_payments_bank, upi_volume_share, bd_shape, td_shape
ISSUER_BANKS = [
    ("B-01", "State Bank of India",      False, 0.235, 11.8, 0.22),
    ("B-02", "HDFC Bank",                False, 0.130,  8.9, 0.16),
    ("B-03", "ICICI Bank",               False, 0.090,  9.4, 0.73),   # TD anchored
    ("B-04", "Paytm Payments Bank",      True,  0.080, 14.5, 0.31),
    ("B-05", "Axis Bank",                False, 0.070,  9.8, 0.19),
    ("B-06", "Punjab National Bank",     False, 0.060, 13.2, 0.34),
    ("B-07", "Bank of Baroda",           False, 0.055, 12.6, 0.29),
    ("B-08", "Union Bank of India",      False, 0.050, 13.4, 0.33),
    ("B-09", "Kotak Mahindra Bank",      False, 0.045,  8.6, 0.15),
    ("B-10", "Canara Bank",              False, 0.045, 12.9, 0.31),
    ("B-11", "Yes Bank",                 False, 0.035,  9.1, 0.18),
    ("B-12", "Airtel Payments Bank",     True,  0.035, 23.20, 0.26),  # BD anchored
    ("B-13", "IDFC First Bank",          False, 0.030,  8.4, 0.17),
    ("B-14", "IndusInd Bank",            False, 0.025,  8.8, 0.21),
    ("B-15", "Fino Payments Bank",       True,  0.015, 24.26, 0.28),  # BD anchored
]

ACQUIRER_BANKS = [
    ("B-51", "HDFC Bank (Acquiring)", 0.45),
    ("B-52", "Axis Bank (Acquiring)", 0.33),
    ("B-53", "ICICI Bank (Acquiring)", 0.22),
]


def calibrate_declines() -> dict[str, dict[str, float]]:
    """
    Scale the non-anchored bd_shape/td_shape values so the volume-weighted means
    equal the published aggregates exactly, holding [NPCI]-anchored banks fixed.

    Returns {bank_id: {"bd": pct, "td": pct, "approval": fraction, ...}}.
    """
    out: dict[str, dict[str, float]] = {}

    def _solve(shape_idx: int, anchors: dict[str, float], target_mean: float) -> dict[str, float]:
        anchored_contrib = sum(
            share * anchors[name]
            for _, name, _, share, *_ in ISSUER_BANKS if name in anchors
        )
        free_shape_contrib = sum(
            share * row[shape_idx]
            for row in ISSUER_BANKS
            for _, name, _, share in [(row[0], row[1], row[2], row[3])]
            if name not in anchors
        )
        # target_mean = anchored_contrib + k * free_shape_contrib  ->  solve for k
        k = (target_mean - anchored_contrib) / free_shape_contrib if free_shape_contrib else 0.0
        return {
            row[0]: (anchors[row[1]] if row[1] in anchors else k * row[shape_idx])
            for row in ISSUER_BANKS
        }

    bd = _solve(4, ANCHORED_BD, UPI_BUSINESS_DECLINE_PCT)
    td = _solve(5, ANCHORED_TD, UPI_TECHNICAL_DECLINE_PCT)

    for bank_id, name, is_pb, share, _, _ in ISSUER_BANKS:
        out[bank_id] = {
            "name": name,
            "is_payments_bank": is_pb,
            "upi_share": share,
            "bd": round(bd[bank_id], 4),
            "td": round(td[bank_id], 4),
            "approval": round((100.0 - bd[bank_id] - td[bank_id]) / 100.0, 6),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Payment-method success rates
#
# ONLY UPI is NPCI-calibrated. NPCI does not publish success rates for cards,
# netbanking, wallets or EMI, so those are [MODELLED] industry-typical values.
# This distinction is stated in the demo and in datasets/CALIBRATION.md rather
# than letting all five look equally authoritative.
# ─────────────────────────────────────────────────────────────────────────────
METHOD_SUCCESS_RATE = {
    "UPI":        (UPI_APPROVAL_PCT / 100.0, "NPCI"),      # 0.8888
    "CARD":       (0.902, "MODELLED"),   # blended POS (~0.95) and e-comm (~0.85)
    "NETBANKING": (0.712, "MODELLED"),   # bank-page redirects; worst mainstream rail
    "WALLET":     (0.951, "MODELLED"),   # pre-funded, so few insufficient-funds declines
    "EMI":        (0.854, "MODELLED"),   # extra eligibility and bureau checks
}

# How sensitive each rail is to the *issuer's* own decline profile. UPI rides the
# issuer directly, so it is fully sensitive. Wallets are pre-funded and barely care.
METHOD_ISSUER_SENSITIVITY = {
    "UPI": 1.00, "CARD": 0.70, "NETBANKING": 0.55, "WALLET": 0.15, "EMI": 0.60,
}

# ─────────────────────────────────────────────────────────────────────────────
# Portfolio method mix — [ASSUMED]
#
# NOT national NPCI share. By national volume UPI is ~97% of NPCI rails, which
# would make every other rail statistically invisible and the demo pointless.
# This is a mid-market merchant-acquirer checkout mix: UPI-led, cards material,
# netbanking small, and EMI present because it is core to this business.
# ─────────────────────────────────────────────────────────────────────────────
METHOD_MIX_BASE = {"UPI": 0.62, "CARD": 0.22, "NETBANKING": 0.08, "WALLET": 0.05, "EMI": 0.03}

# The planted incident: a routing/checkout change pushes 7pp of traffic from UPI
# onto netbanking — structurally the worst rail. Per-rail success rates stay flat.
METHOD_MIX_SHIFTED = {"UPI": 0.55, "CARD": 0.22, "NETBANKING": 0.15, "WALLET": 0.05, "EMI": 0.03}

# The deliberately tiny rate effect, so mix dominates the decomposition.
SR_DRIFT = -0.001

# [MODELLED] Share of attempts left unresolved at snapshot time. Excluded from the
# success_rate denominator — see semantic_model.yaml's denominator_note.
PENDING_RATE = 0.004


# ─────────────────────────────────────────────────────────────────────────────
# FAILURE CODES — the payment journey, step 1 to 10
#
# Replaces the earlier 7-code table. The codes are now modelled on the actual
# Pine Labs journey (amount entry -> terminal -> acquirer -> issuer -> slip ->
# settlement) so that "where in the journey did it break" is answerable, which the
# flat code list could not do.
#
# THE NPCI SCOPE PROBLEM, AND HOW IT IS RESOLVED
# ----------------------------------------------
# NPCI's published technical decline of 0.24% covers faults at the BANK or at NPCI
# — issuer down, issuer timeout, response lost. It does NOT cover a logged-out
# terminal, a faulty card reader, a dead SIM or a gateway bug, because none of
# those reach NPCI's switch. A card-present journey therefore has strictly more
# technical failure than NPCI's UPI number, and quoting 0.24% as the overall
# technical decline of a POS estate would understate it by roughly 5x.
#
# So the model is split rather than fudged:
#
#   * UPI keeps the NPCI anchors exactly — BD 10.88%, TD 0.24% of UPI attempts,
#     with the per-bank profile (ICICI's 0.73% TD and so on) preserved.
#   * Card-present rails carry the terminal- and gateway-side technical codes
#     inside their own already-modelled failure budget.
#   * `npci_td_scope` marks the codes NPCI would actually count, so the comparable
#     subset stays auditable against the published figure.
#
# Overall technical decline lands near 1.1% of attempts, which is the realistic
# number for an estate with terminals in it, while the NPCI-comparable slice still
# reconciles to 0.24%. data/verify.py asserts both separately.
#
# WEIGHTS ARE [MODELLED]. NPCI publishes the BD/TD split but not a code-level
# breakdown, so the relative weights below are industry-shaped choices: they are
# ordered so INSUFFICIENT_FUNDS and WRONG_PIN are the two largest codes, which is
# what every published decline post-mortem reports. Every one is listed in
# ASSUMPTIONS.md. Nothing here is an NPCI figure.
#
# `modes` restricts a code to the entry modes where it is physically possible: a
# chip read error cannot happen on a QR payment and a tap limit cannot be exceeded
# when the card was never tapped. Without that restriction the failure mix is
# internally contradictory and any journey-step analysis is fiction.
# ─────────────────────────────────────────────────────────────────────────────

ENTRY_MODES = ["tap", "chip", "swipe", "qr", "online"]
CARD_PRESENT_MODES = ("tap", "chip", "swipe")

JOURNEY_STEPS = {
    1:  "Amount entry",
    2:  "Choose method",
    3:  "Card read",
    4:  "Authentication",
    5:  "Terminal to Pine Labs",
    6:  "Pine Labs routing",
    7:  "Acquirer / network",
    8:  "Issuer decision",
    9:  "Response / slip",
    10: "Settlement",
}

# [MODELLED] entry-mode mix per rail. Netbanking is a browser redirect so it is
# always `online`; UPI is QR at the counter or an in-app collect request.
ENTRY_MODE_MIX = {
    "UPI":        {"qr": 0.55, "online": 0.45},
    "CARD":       {"tap": 0.46, "chip": 0.38, "swipe": 0.10, "online": 0.06},
    "NETBANKING": {"online": 1.00},
    "WALLET":     {"online": 0.72, "qr": 0.28},
    "EMI":        {"chip": 0.62, "swipe": 0.13, "online": 0.25},
}

# [MODELLED] What fraction of a rail's failures are technical rather than business.
# UPI is NOT listed: it is derived per bank from the NPCI-calibrated BD/TD ratio,
# which is what keeps the published anchor exact. The others carry terminal and
# gateway faults, so their technical share is much higher.
TECH_SHARE_OF_FAILURES = {
    "CARD": 0.25, "NETBANKING": 0.12, "WALLET": 0.10, "EMI": 0.22,
}

# A degraded terminal or a weak SIM does not change HOW OFTEN a payment fails in
# this model — it changes WHY. These multiply the technical share of failures on
# that terminal, so the failure mix shifts toward NETWORK_TIMEOUT / TERM_OFFLINE
# without perturbing the success-rate surface the decomposition is pinned to.
DEGRADED_TERMINAL_TECH_BOOST = 2.2
SIM_TERMINAL_TECH_BOOST = 1.45

#     code, category, description, step, owner, decline_type, retriable, npci, weight, modes
JOURNEY_FAILURE_CODES = [
    # ── business ────────────────────────────────────────────────────────────
    ("AMT_MISMATCH",       "amount_mismatch",    "Wrong amount entered or billing not linked",
     1, "merchant", "business", True,  False, 0.03,  "*"),
    ("EMI_INELIGIBLE",     "emi_ineligible",     "Card not eligible for EMI",
     2, "customer", "business", False, False, 0.30,  ("chip", "swipe", "online")),
    ("OFFER_INVALID",      "offer_invalid",      "Offer expired or misconfigured",
     2, "merchant", "business", False, False, 0.03,  "*"),
    ("CHIP_READ_ERR",      "card_read_error",    "Damaged chip or magnetic stripe",
     3, "customer", "business", True,  False, 0.14,  ("chip", "swipe")),
    ("TAP_LIMIT",          "limit_exceeded",     "Contactless limit exceeded",
     3, "customer", "business", True,  False, 0.10,  ("tap",)),
    ("WRONG_PIN",          "invalid_credential", "Wrong PIN or UPI PIN",
     4, "customer", "business", True,  False, 0.26,  ("tap", "chip", "swipe", "qr", "online")),
    ("PIN_TRIES_EXCEEDED", "invalid_credential", "PIN attempts exceeded",
     4, "customer", "business", False, False, 0.05,  ("tap", "chip", "swipe", "qr", "online")),
    ("USER_ABANDONED",     "user_abandoned",     "Customer gave up before authorising",
     4, "customer", "business", True,  False, 0.08,  "*"),
    ("FRAUD_BLOCK",        "risk_block",         "Blocked by risk or fraud rules",
     6, "pine_labs", "business", False, False, 0.04, "*"),
    ("INSUFFICIENT_FUNDS", "insufficient_funds", "Not enough balance",
     8, "customer", "business", True,  False, 0.34,  "*"),
    ("LIMIT_EXCEEDED",     "limit_exceeded",     "Daily or per-transaction limit hit",
     8, "customer", "business", True,  False, 0.09,  "*"),
    ("CARD_BLOCKED",       "card_blocked",       "Card blocked or expired",
     8, "customer", "business", False, False, 0.07, ("tap", "chip", "swipe", "online")),

    # ── technical ───────────────────────────────────────────────────────────
    ("TERM_LOGGED_OUT",    "terminal_fault",     "Terminal logged out or software outdated",
     1, "terminal", "technical", True,  False, 0.06, CARD_PRESENT_MODES),
    ("QR_GEN_FAIL",        "qr_generation",      "QR code could not be generated",
     2, "pine_labs", "technical", True, False, 0.08, ("qr",)),
    ("READER_FAULT",       "terminal_fault",     "Faulty card reader",
     3, "terminal", "technical", True,  False, 0.10, CARD_PRESENT_MODES),
    ("NETWORK_TIMEOUT",    "connectivity",       "Weak SIM or Wi-Fi, request timed out",
     5, "terminal", "technical", True,  False, 0.22, CARD_PRESENT_MODES),
    ("TERM_OFFLINE",       "connectivity",       "Terminal offline",
     5, "terminal", "technical", True,  False, 0.12, CARD_PRESENT_MODES),
    ("GATEWAY_ERROR",      "gateway_error",      "Internal gateway error",
     6, "pine_labs", "technical", True, False, 0.10, "*"),
    ("ROUTED_TO_DEGRADED", "routing_fault",      "Routed to a degraded acquirer",
     6, "pine_labs", "technical", True, False, 0.05, "*"),
    ("ACQUIRER_DOWN",      "acquirer_unavailable", "Acquiring bank unavailable",
     7, "acquirer", "technical", True, False, 0.06, "*"),
    ("NETWORK_OUTAGE",     "network_fault",      "Card network or NPCI outage",
     7, "network", "technical", True,  False, 0.04, "*"),
    ("ISSUER_DOWN",        "issuer_unavailable", "Issuer server down",
     8, "issuer", "technical", True,   True,  0.08, "*"),
    ("ISSUER_TIMEOUT",     "issuer_unavailable", "Issuer too slow to respond",
     8, "issuer", "technical", True,   True,  0.12, "*"),
    ("RESPONSE_LOST",      "response_lost",      "Approved but the reply was lost, reversal raised",
     9, "network", "technical", True,  True,  0.04, "*"),
    ("DUPLICATE_CHARGE",   "duplicate_charge",   "Customer charged twice",
     9, "pine_labs", "technical", False, False, 0.02, "*"),
    ("BATCH_NOT_CLOSED",   "settlement_fault",   "Batch not closed, so not settled",
     10, "merchant", "technical", True, False, 0.01, CARD_PRESENT_MODES),
    ("SETTLEMENT_MISMATCH", "settlement_fault",  "Counts differ between systems",
     10, "pine_labs", "technical", True, False, 0.01, "*"),
]

# ── The second planted story: a 3-hour acquirer incident ────────────────────
# Deliberately placed OUTSIDE both the baseline and target weeks of the mix-shift
# story (2026-11-02 .. 2026-11-15). The two stories must be independently
# diagnosable: if the incident sat inside the comparison window it would leak a
# rate effect into the mix decomposition and verify.py's exact-identity assertion
# would be measuring both at once.
ACQUIRER_INCIDENT = {
    "date": "2026-10-28",
    "acquirer_bank_id": "B-53",        # ICICI Bank (Acquiring)
    "hour_from": 14,
    "hour_to": 17,                     # exclusive
    "success_to_failure_rate": 0.45,   # of successes in the window, flipped
    "codes": ("ACQUIRER_DOWN", "ISSUER_TIMEOUT"),
    "code_weights": (0.72, 0.28),
}


def failure_codes_by_class(decline_type: str) -> list[tuple]:
    return [c for c in JOURNEY_FAILURE_CODES if c[5] == decline_type]


def code_eligible(code_row: tuple, entry_mode: str) -> bool:
    modes = code_row[9]
    return modes == "*" or entry_mode in modes


def expected_decomposition() -> dict[str, float]:
    """
    The figures this calibration is solved to produce, computed from the constants
    above rather than hard-coded — so editing a rate or a share updates the target
    and data/verify.py immediately shows the drift.

        delta_pp = target_sr - baseline_sr
        mix_pp   = sum (w_target - w_base) * sr_base      traffic moved
        rate_pp  = sum  w_target * (sr_target - sr_base)  rails got worse
    """
    base_sr = {m: METHOD_SUCCESS_RATE[m][0] for m in METHOD_MIX_BASE}
    tgt_sr = {m: base_sr[m] + SR_DRIFT for m in METHOD_MIX_BASE}

    baseline = sum(METHOD_MIX_BASE[m] * base_sr[m] for m in METHOD_MIX_BASE)
    target = sum(METHOD_MIX_SHIFTED[m] * tgt_sr[m] for m in METHOD_MIX_SHIFTED)
    mix = sum((METHOD_MIX_SHIFTED[m] - METHOD_MIX_BASE[m]) * base_sr[m] for m in METHOD_MIX_BASE)
    rate = sum(METHOD_MIX_SHIFTED[m] * (tgt_sr[m] - base_sr[m]) for m in METHOD_MIX_BASE)

    delta = target - baseline
    return {
        "baseline_sr_pct": round(baseline * 100, 4),
        "target_sr_pct": round(target * 100, 4),
        "delta_pp": round(delta * 100, 4),
        "mix_effect_pp": round(mix * 100, 4),
        "rate_effect_pp": round(rate * 100, 4),
        "mix_share_pct": round(100 * mix / delta, 2) if delta else 0.0,
        "residual_pp": round((delta - mix - rate) * 100, 10),
    }


if __name__ == "__main__":
    cal = calibrate_declines()
    print(f"{'bank':<26} {'share':>7} {'BD%':>7} {'TD%':>6} {'approval':>9}")
    for bid, v in cal.items():
        print(f"{v['name']:<26} {v['upi_share']:>7.3f} {v['bd']:>7.2f} {v['td']:>6.2f} {v['approval']*100:>8.2f}%")
    wbd = sum(v["upi_share"] * v["bd"] for v in cal.values())
    wtd = sum(v["upi_share"] * v["td"] for v in cal.values())
    print(f"\nvolume-weighted BD {wbd:.4f}%  (NPCI published {UPI_BUSINESS_DECLINE_PCT}%)")
    print(f"volume-weighted TD {wtd:.4f}%  (NPCI published {UPI_TECHNICAL_DECLINE_PCT}%)")
    print(f"implied UPI approval {100-wbd-wtd:.4f}%  (derived {UPI_APPROVAL_PCT:.2f}%)")
    print("\nexpected decomposition of the planted incident:")
    for k, v in expected_decomposition().items():
        print(f"  {k:<20} {v}")
