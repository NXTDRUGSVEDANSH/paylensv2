"""
PayLens synthetic data generator — calibrated to NPCI published figures.

Synthetic at row level, calibrated at aggregate level. See data/npci_calibration.py
for every constant and its provenance, and datasets/CALIBRATION.md for the writeup.

Determinism
-----------
seed=42, and success counts per (day, method, issuer) cell are allocated by
    n_success = round(n_cell * sr_cell)
rather than by Bernoulli sampling. With ~42k attempts per week the sampling error
on a segment success rate is ~0.15pp, which is larger than the -0.10pp rate effect
this dataset exists to exhibit — random sampling would blur the very signal the
driver engine must detect, and the headline "92.5% of the decline is mix" would
wander between runs. Row-level realism is untouched; only the aggregate is pinned.

Per-method success rates are renormalised after issuer adjustment so each rail's
volume-weighted rate lands exactly on its calibrated target. That keeps the
decomposition arithmetic exact while still giving issuer_bank genuine variation,
so ranking dimensions by explained share is a real operation.

Outputs
-------
    datasets/*.csv        human-inspectable, what a reviewer opens first
    datasets/*.parquet    typed, what the loader actually reads
    data/paylens.duckdb   the queryable database
"""

from __future__ import annotations

import argparse
import functools
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from npci_calibration import (
    ACQUIRER_BANKS, ACQUIRER_INCIDENT, CARD_PRESENT_MODES,
    DEGRADED_TERMINAL_TECH_BOOST, ENTRY_MODE_MIX, ISSUER_BANKS,
    JOURNEY_FAILURE_CODES, JOURNEY_STEPS, METHOD_ISSUER_SENSITIVITY,
    METHOD_MIX_BASE, METHOD_MIX_SHIFTED, METHOD_SUCCESS_RATE, PENDING_RATE,
    SIM_TERMINAL_TECH_BOOST, SR_DRIFT, TECH_SHARE_OF_FAILURES, UPI_APPROVAL_PCT,
    calibrate_declines, code_eligible, expected_decomposition,
)

SEED = 42
DAYS = 120
END_DATE = date(2026, 11, 20)
START_DATE = END_DATE - timedelta(days=DAYS - 1)

# Aligned to ISO week boundaries (Mon..Sun) so that a user asking "last week"
# lands exactly on the incident window, with "the week before" as its baseline.
# 2026-11-20 is a Friday, so the last COMPLETE week is Mon 11-09 .. Sun 11-15.
BASELINE_WEEK = (date(2026, 11, 2), date(2026, 11, 8))
TARGET_WEEK = (date(2026, 11, 9), date(2026, 11, 15))
SHIFT_START = TARGET_WEEK[0]

BASE_ATTEMPTS_PER_DAY = 6_000
WEEKDAY_VOLUME = np.array([1.05, 1.08, 1.06, 1.04, 1.12, 0.88, 0.77])  # Mon..Sun
N_MERCHANTS = 240

# ── Planted defects, one per required failure scenario ───────────────────────
DUP_DAY_OFFSET = 83   # double-ingest window -> 2026-10-15, inside the month the
#                       data-quality demo question asks about
DUP_ROWS = 1_800
NULL_BANK_RATE = 0.023
INCOMPLETE_DAYS = 2
INCOMPLETE_FRACTION = 0.40
NEGATIVE_AMOUNT_ROWS = 47
SETTLEMENT_MISMATCH = {"M-1042": 0.070, "M-1108": 0.043, "M-1187": 0.088}

# Netbanking concentrates on public-sector bank pages, which also decline more.
# Makes issuer_bank a genuine *secondary* explanator of the planted incident.
NETBANKING_BIAS = {"B-01": 3.2, "B-06": 2.8, "B-07": 2.4, "B-10": 2.1, "B-08": 1.9}

PM_ROWS = [
    ("PM-01", "UPI",        None,         None,     False),
    ("PM-02", "CARD",       "VISA",       "credit", True),
    ("PM-03", "CARD",       "VISA",       "debit",  False),
    ("PM-04", "CARD",       "MASTERCARD", "credit", True),
    ("PM-05", "CARD",       "RUPAY",      "debit",  False),
    ("PM-06", "NETBANKING", None,         None,     False),
    ("PM-07", "WALLET",     None,         None,     True),
    ("PM-08", "EMI",        "VISA",       "credit", True),
]
PM_BY_METHOD = {"UPI": ["PM-01"], "CARD": ["PM-02", "PM-03", "PM-04", "PM-05"],
                "NETBANKING": ["PM-06"], "WALLET": ["PM-07"], "EMI": ["PM-08"]}
CARD_PM_W = np.array([0.34, 0.28, 0.24, 0.14])

CATEGORIES = ["Groceries", "Electronics", "Fashion", "Travel", "Food Delivery",
              "Utilities", "Pharmacy", "Education", "Fuel", "Entertainment"]
STATE_REGION = {
    "Maharashtra": "West", "Gujarat": "West", "Rajasthan": "West",
    "Karnataka": "South", "Tamil Nadu": "South", "Telangana": "South", "Kerala": "South",
    "Delhi": "North", "Haryana": "North", "Punjab": "North", "Uttar Pradesh": "North",
    "West Bengal": "East", "Odisha": "East", "Bihar": "East", "Assam": "East",
}
CITY = {"Maharashtra": "Mumbai", "Gujarat": "Ahmedabad", "Rajasthan": "Jaipur",
        "Karnataka": "Bengaluru", "Tamil Nadu": "Chennai", "Telangana": "Hyderabad",
        "Kerala": "Kochi", "Delhi": "New Delhi", "Haryana": "Gurugram",
        "Punjab": "Ludhiana", "Uttar Pradesh": "Lucknow", "West Bengal": "Kolkata",
        "Odisha": "Bhubaneswar", "Bihar": "Patna", "Assam": "Guwahati"}


def _h(*p: object) -> int:
    return int(hashlib.md5("|".join(map(str, p)).encode()).hexdigest()[:8], 16)


def _apportion(total: int, weights: np.ndarray) -> np.ndarray:
    """
    Split `total` across `weights` by largest remainder (Hamilton), exactly.

    Used instead of rng.multinomial for the issuer split. Multinomial sampling
    perturbs the issuer mix inside each rail, and because success rate varies by
    issuer that perturbs the rail's own rate — which shows up as a spurious RATE
    effect and corrupts the mix/rate split this dataset exists to demonstrate.
    Deterministic apportionment removes that noise at source.
    """
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()
    exact = w * total
    base = np.floor(exact).astype(int)
    short = total - base.sum()
    if short > 0:
        order = np.argsort(-(exact - base))
        base[order[:short]] += 1
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Success-rate surface: method x issuer, renormalised to hit each rail's target
# ─────────────────────────────────────────────────────────────────────────────
def build_sr_surface(cal: dict) -> tuple[dict, dict]:
    """
    Returns (sr[method][bank_id], bank_weights[method][bank_id]).

    An issuer's own approval rate shifts a rail's rate in proportion to how much
    that rail depends on the issuer (METHOD_ISSUER_SENSITIVITY): UPI rides the
    issuer directly; a pre-funded wallet barely does. Each rail is then shifted
    so its volume-weighted rate equals METHOD_SUCCESS_RATE exactly.
    """
    bank_ids = [b[0] for b in ISSUER_BANKS]
    base_w = np.array([cal[b]["upi_share"] for b in bank_ids])
    base_w = base_w / base_w.sum()
    nb_w = np.array([base_w[i] * NETBANKING_BIAS.get(b, 1.0) for i, b in enumerate(bank_ids)])
    nb_w = nb_w / nb_w.sum()

    upi_mean = UPI_APPROVAL_PCT / 100.0
    sr: dict[str, dict[str, float]] = {}
    weights: dict[str, dict[str, float]] = {}

    for method, (target, _src) in METHOD_SUCCESS_RATE.items():
        w = nb_w if method == "NETBANKING" else base_w
        sens = METHOD_ISSUER_SENSITIVITY[method]
        raw = np.array([target + sens * (cal[b]["approval"] - upi_mean) for b in bank_ids])
        raw = np.clip(raw, 0.30, 0.995)
        raw = raw + (target - float(w @ raw))          # exact renormalisation
        raw = np.clip(raw, 0.30, 0.995)
        sr[method] = dict(zip(bank_ids, raw))
        weights[method] = dict(zip(bank_ids, w))
    return sr, weights


def _weekday_wobble(volumes: np.ndarray) -> np.ndarray:
    """Per-weekday rate wobble, re-centred so its volume-weighted mean is zero."""
    w = np.array([0.0012, 0.0005, -0.0003, -0.0008, -0.0015, 0.0009, 0.0004])
    return w - (volumes @ w) / volumes.sum()


# ─────────────────────────────────────────────────────────────────────────────
# Dimensions
# ─────────────────────────────────────────────────────────────────────────────
def dim_payment_method() -> pd.DataFrame:
    return pd.DataFrame(PM_ROWS, columns=["pm_id", "method", "card_network",
                                          "card_type", "is_tokenized"])


def dim_bank(cal: dict) -> pd.DataFrame:
    rows = [dict(bank_id=b, bank_name=v["name"], bank_type="issuer",
                 ifsc_prefix=v["name"].split()[0][:4].upper(),
                 is_payments_bank=v["is_payments_bank"],
                 npci_business_decline_pct=v["bd"], npci_technical_decline_pct=v["td"])
            for b, v in cal.items()]
    rows += [dict(bank_id=b, bank_name=n, bank_type="acquirer",
                  ifsc_prefix=n.split()[0][:4].upper(), is_payments_bank=False,
                  npci_business_decline_pct=None, npci_technical_decline_pct=None)
             for b, n, _ in ACQUIRER_BANKS]
    return pd.DataFrame(rows)


def dim_merchant() -> pd.DataFrame:
    states = list(STATE_REGION)
    rows = []
    for i in range(N_MERCHANTS):
        mid = f"M-{1000 + i}"
        st = states[_h(mid, "st") % len(states)]
        cat = CATEGORIES[_h(mid, "cat") % len(CATEGORIES)]
        rows.append(dict(
            merchant_id=mid, merchant_name=f"{cat} Store {i + 1:03d}",
            mcc=str(5000 + _h(mid, "mcc") % 900), category=cat,
            city=CITY[st], state=st, region=STATE_REGION[st],
            tier=["enterprise", "mid", "sme"][min(2, _h(mid, "t") % 10 // 3)],
            onboarded_at=START_DATE - timedelta(days=int(_h(mid, "o") % 900) + 30),
            risk_band=["low", "low", "medium", "high"][_h(mid, "r") % 4]))
    return pd.DataFrame(rows)


def dim_failure_code() -> pd.DataFrame:
    """One row per journey failure code. `journey_step` is what makes 'where in the
    journey does it break' a groupable question rather than a narrative."""
    return pd.DataFrame([
        dict(failure_code=code, category=cat, description=desc,
             journey_step=step, journey_step_label=JOURNEY_STEPS[step],
             owner=owner, decline_type=dt, is_retriable=retr,
             npci_td_scope=npci)
        for code, cat, desc, step, owner, dt, retr, npci, _w, _m in JOURNEY_FAILURE_CODES])


# ─────────────────────────────────────────────────────────────────────────────
# Terminals
# ─────────────────────────────────────────────────────────────────────────────
TERMINALS_BY_TIER = {"enterprise": 6, "mid": 3, "sme": 1}
SOFTWARE_VERSIONS = ["4.8.2", "4.7.1", "4.6.0", "3.9.4"]   # newest .. oldest
METRO_CITIES = {"Mumbai", "New Delhi", "Bengaluru", "Chennai", "Hyderabad",
                "Kolkata", "Ahmedabad"}


def dim_terminal(merchants: pd.DataFrame) -> pd.DataFrame:
    """
    A terminal estate hung off the merchants, so region and city are consistent
    between the two tables. Health is correlated with software version and with
    connectivity, because that is the actual causal story an estate manager acts
    on: old firmware on a SIM link in a non-metro city is where timeouts live.
    """
    rows = []
    for m in merchants.itertuples():
        for k in range(TERMINALS_BY_TIER[m.tier]):
            tid = f"TERM-{m.merchant_id[2:]}-{k + 1}"
            metro = m.city in METRO_CITIES
            # [ASSUMED] metros are mostly on broadband; smaller cities lean on SIM.
            conn = "wifi" if (_h(tid, "c") % 100) < (82 if metro else 38) else "sim"
            vi = _h(tid, "v") % 100
            ver = SOFTWARE_VERSIONS[0 if vi < 46 else 1 if vi < 74 else 2 if vi < 91 else 3]
            stale = ver in SOFTWARE_VERSIONS[2:]
            p_degraded = 10 + (14 if conn == "sim" else 0) + (18 if stale else 0) \
                            + (0 if metro else 6)
            rows.append(dict(
                terminal_id=tid, merchant_id=m.merchant_id, city=m.city,
                state=m.state, region=m.region, connectivity=conn,
                software_version=ver, is_software_stale=stale,
                health="degraded" if (_h(tid, "h") % 100) < p_degraded else "ok",
                installed_at=m.onboarded_at + timedelta(days=int(_h(tid, "i") % 60))))
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Failure-code eligibility
# ─────────────────────────────────────────────────────────────────────────────
# Two codes need a restriction the entry mode alone cannot express: an EMI
# ineligibility cannot occur on a non-EMI sale, and a card cannot be blocked when
# no card was presented. Everything else is fully determined by entry mode.
METHOD_ONLY = {"EMI_INELIGIBLE": {"EMI"}, "CARD_BLOCKED": {"CARD", "EMI"}}


@functools.lru_cache(maxsize=None)
def _code_dist(method: str, entry_mode: str, decline_type: str) -> tuple:
    """(codes, probabilities) for one (rail, entry mode, class). Memoised: there are
    at most 5 x 5 x 2 of these and the inner loop asks for them ~9,000 times."""
    rows = [r for r in JOURNEY_FAILURE_CODES
            if r[5] == decline_type
            and code_eligible(r, entry_mode)
            and method in METHOD_ONLY.get(r[0], {method})]
    if not rows:
        # Can only happen if a rail has no eligible code of this class at all.
        # Fail loudly: silently returning None would write NULL failure codes onto
        # FAILED rows, which reads downstream as "unclassified failure" forever.
        raise ValueError(f"no {decline_type} failure code is possible for "
                         f"{method}/{entry_mode} — the eligibility table is wrong")
    w = np.array([r[8] for r in rows], dtype=float)
    return tuple(r[0] for r in rows), tuple(w / w.sum())


def build_transactions(rng, cal: dict, merchants: pd.DataFrame,
                       terminals: pd.DataFrame) -> pd.DataFrame:
    sr_surface, bank_w = build_sr_surface(cal)
    wobble = _weekday_wobble(WEEKDAY_VOLUME)
    bank_ids = [b[0] for b in ISSUER_BANKS]

    mids = merchants["merchant_id"].to_numpy()
    mw = np.where(merchants["tier"].to_numpy() == "enterprise", 3.0,
                  np.where(merchants["tier"].to_numpy() == "mid", 1.6, 1.0))
    mw = mw / mw.sum()

    # Terminal lookup as flat arrays: offset + (hash % count) picks a terminal that
    # genuinely belongs to the merchant on the row, so terminal.region always
    # agrees with merchant.region. A hash over the row id (the previous approach)
    # produced terminals in the wrong city for 93% of rows.
    t_order = terminals.sort_values("merchant_id").reset_index(drop=True)
    m_pos = {m: i for i, m in enumerate(mids)}
    t_ids = t_order["terminal_id"].to_numpy()
    t_degraded = (t_order["health"].to_numpy() == "degraded")
    t_sim = (t_order["connectivity"].to_numpy() == "sim")
    counts_by_m = t_order.groupby("merchant_id").size()
    t_count = np.array([counts_by_m[m] for m in mids])
    starts = np.zeros(len(mids), dtype=int)
    running = {}
    for i, m in enumerate(t_order["merchant_id"].to_numpy()):
        running.setdefault(m, i)
    for m, i in running.items():
        starts[m_pos[m]] = i

    acq_ids = [a[0] for a in ACQUIRER_BANKS]
    acq_w = np.array([a[2] for a in ACQUIRER_BANKS])

    lat_base = {"UPI": 900, "CARD": 1900, "NETBANKING": 3400, "WALLET": 1100, "EMI": 2600}
    chunks: list[pd.DataFrame] = []
    seq = 0

    for d in range(DAYS):
        day = START_DATE + timedelta(days=d)
        wd = day.weekday()
        shifted = day >= SHIFT_START
        mix = METHOD_MIX_SHIFTED if shifted else METHOD_MIX_BASE
        drift = SR_DRIFT if shifted else 0.0

        n_day = int(BASE_ATTEMPTS_PER_DAY * WEEKDAY_VOLUME[wd] * (1.0 + 0.0015 * d))
        if (END_DATE - day).days < INCOMPLETE_DAYS:
            n_day = int(n_day * INCOMPLETE_FRACTION)     # planted: partial ingest

        for method, share in mix.items():
            n_m = int(round(n_day * share))
            if n_m <= 0:
                continue
            modes = list(ENTRY_MODE_MIX[method])
            mode_p = np.array([ENTRY_MODE_MIX[method][m] for m in modes])
            w = np.array([bank_w[method][b] for b in bank_ids])
            counts = _apportion(n_m, w)

            for bi, n_cell in enumerate(counts):
                if n_cell <= 0:
                    continue
                bid = bank_ids[bi]
                sr = float(np.clip(sr_surface[method][bid] + drift + wobble[wd], 0.05, 0.999))
                n_pend = int(round(n_cell * PENDING_RATE))
                n_res = n_cell - n_pend
                n_succ = int(round(n_res * sr))          # deterministic — see docstring
                n_fail = n_res - n_succ

                status = np.array(["SUCCESS"] * n_succ + ["FAILED"] * n_fail
                                  + ["PENDING"] * n_pend, dtype=object)
                rng.shuffle(status)

                m_idx = rng.choice(len(mids), n_cell, p=mw)
                entry_mode = rng.choice(modes, n_cell, p=mode_p)
                card_present = np.isin(entry_mode, CARD_PRESENT_MODES)

                # Terminal: present for anything at the counter (card read or QR on
                # a soundbox), absent for an online checkout. NULL here is real
                # missing-by-design, not a data-quality defect, and the DQ panel
                # distinguishes the two.
                t_pick = starts[m_idx] + (
                    np.array([_h(int(i), "tp") for i in m_idx + seq]) % t_count[m_idx])
                has_term = entry_mode != "online"
                terminal_id = np.where(has_term, t_ids[t_pick], None)

                # Terminal condition changes WHY a payment fails, not how often —
                # the success-rate surface is untouched, so the mix/rate
                # decomposition stays exactly as calibrated. Only card-present
                # modes are affected: a reader fault is impossible on a QR scan,
                # and boosting UPI here would break the NPCI 0.24% anchor.
                boost = np.where(card_present & t_degraded[t_pick],
                                 DEGRADED_TERMINAL_TECH_BOOST, 1.0)
                boost *= np.where(card_present & t_sim[t_pick],
                                  SIM_TERMINAL_TECH_BOOST, 1.0)

                if method == "UPI":
                    # Per-bank NPCI ratio, preserved exactly (ICICI's 0.73% TD etc).
                    base_tech = cal[bid]["td"] / max(cal[bid]["bd"] + cal[bid]["td"], 1e-9)
                else:
                    base_tech = TECH_SHARE_OF_FAILURES[method]
                tech_share = np.clip(base_tech * boost, 0.0, 0.85)
                is_tech = rng.random(n_cell) < tech_share

                fcode = np.full(n_cell, None, dtype=object)
                failed = status == "FAILED"
                for mode in modes:
                    for dt, want_tech in (("technical", True), ("business", False)):
                        sel = failed & (entry_mode == mode) & (is_tech == want_tech)
                        k = int(sel.sum())
                        if k:
                            codes, probs = _code_dist(method, mode, dt)
                            fcode[sel] = rng.choice(codes, k, p=probs)

                pm = (rng.choice(PM_BY_METHOD[method], n_cell, p=CARD_PM_W)
                      if method == "CARD" else np.full(n_cell, PM_BY_METHOD[method][0]))
                # Unattributed issuer, only on rails that genuinely may lack one.
                null_bank = (rng.random(n_cell) < NULL_BANK_RATE / 0.67
                             if method in ("UPI", "WALLET") else np.zeros(n_cell, bool))
                secs = rng.integers(0, 86400, n_cell)
                ts = np.array([datetime(day.year, day.month, day.day)
                               + timedelta(seconds=int(s)) for s in secs])
                retry = rng.random(n_cell) < 0.055
                ids = np.arange(seq + 1, seq + 1 + n_cell)
                seq += n_cell

                chunks.append(pd.DataFrame(dict(
                    txn_id=[f"T-{i:08d}" for i in ids],
                    txn_ts=ts,
                    merchant_id=mids[m_idx],
                    bank_id=np.where(null_bank, None, bid),
                    acquirer_bank_id=rng.choice(acq_ids, n_cell, p=acq_w),
                    pm_id=pm,
                    terminal_id=terminal_id,
                    entry_mode=entry_mode,
                    is_emi=(method == "EMI"),
                    amount=np.round(np.exp(rng.normal(6.6, 0.95, n_cell)), 2),
                    currency="INR",
                    status=status,
                    failure_code=fcode,
                    latency_ms=np.maximum(80, rng.normal(
                        lat_base[method], lat_base[method] * 0.3, n_cell)).astype(int),
                    is_retry=retry,
                    original_txn_id=np.where(
                        retry, [f"T-{max(1, i - (_h(i, 'o') % 50 + 1)):08d}" for i in ids], None),
                    settlement_id=None,
                    ingested_at=pd.DatetimeIndex(ts) + pd.to_timedelta(
                        np.array([_h(i, "ing") % 120 + 5 for i in ids]), unit="m"),
                )))

    return pd.concat(chunks, ignore_index=True)


def plant_acquirer_incident(df: pd.DataFrame, rng) -> dict:
    """
    Three hours of one acquirer being unavailable. The second planted story, and
    the one that answers a different question from the mix shift: this is a RATE
    failure localised to an owner, where the mix shift is a composition change with
    no rail actually degrading.

    Placed outside the mix-shift comparison weeks on purpose (see ACQUIRER_INCIDENT).
    Successes in the window are flipped to FAILED and relabelled, and failures
    already there are relabelled too, so the incident shows up as both a
    success-rate dip and a technical-share spike for that one acquirer.
    """
    inc = ACQUIRER_INCIDENT
    day = date.fromisoformat(inc["date"])
    hour = df["txn_ts"].dt.hour
    win = ((df["txn_ts"].dt.date == day)
           & (df["acquirer_bank_id"] == inc["acquirer_bank_id"])
           & (hour >= inc["hour_from"]) & (hour < inc["hour_to"]))

    codes, cw = list(inc["codes"]), np.array(inc["code_weights"], dtype=float)
    cw = cw / cw.sum()

    succ = df.index[win & (df["status"] == "SUCCESS")]
    n_flip = int(round(len(succ) * inc["success_to_failure_rate"]))
    flip = rng.choice(succ, size=n_flip, replace=False) if n_flip else np.array([], dtype=int)
    df.loc[flip, "status"] = "FAILED"

    relabel = df.index[win & (df["status"] == "FAILED")]
    # Pre-existing business declines in the window are left alone — a customer
    # still types the wrong PIN during an outage, and wiping them out would make
    # the incident look cleaner than any real one does.
    tech_or_flipped = np.union1d(
        np.asarray(flip, dtype=int),
        np.asarray(relabel[df.loc[relabel, "failure_code"].isin(
            [r[0] for r in JOURNEY_FAILURE_CODES if r[5] == "technical"])], dtype=int))
    if len(tech_or_flipped):
        df.loc[tech_or_flipped, "failure_code"] = rng.choice(
            codes, len(tech_or_flipped), p=cw)

    return {"date": str(day), "acquirer": inc["acquirer_bank_id"],
            "window": f"{inc['hour_from']:02d}:00-{inc['hour_to']:02d}:00",
            "in_window": int(win.sum()), "flipped": int(n_flip),
            "relabelled": int(len(tech_or_flipped))}


def plant_duplicates(df: pd.DataFrame, rng) -> tuple[pd.DataFrame, int]:
    """Double-ingest window: exact duplicate txn_ids differing only in ingested_at."""
    day = START_DATE + timedelta(days=DUP_DAY_OFFSET)
    mask = df["txn_ts"].dt.date == day
    idx = df.index[mask]
    if len(idx) == 0:
        return df, 0
    pick = rng.choice(idx, size=min(DUP_ROWS, len(idx)), replace=False)
    dup = df.loc[pick].copy()
    dup["ingested_at"] = dup["ingested_at"] + pd.Timedelta(hours=6)
    return pd.concat([df, dup], ignore_index=True), len(pick)


def plant_negative_amounts(df: pd.DataFrame, rng) -> int:
    """Refunds mislabelled as payments."""
    cand = df.index[(df["status"] == "SUCCESS") & (df["amount"] > 100)]
    pick = rng.choice(cand, size=min(NEGATIVE_AMOUNT_ROWS, len(cand)), replace=False)
    df.loc[pick, "amount"] = -df.loc[pick, "amount"].abs()
    return len(pick)


def build_settlements(df: pd.DataFrame) -> pd.DataFrame:
    """
    One batch per merchant per day. txn_count is deliberately UNDERCOUNTED for three
    merchants, so two sources disagree on the same metric and the precedence rule in
    semantic_model.yaml must be applied and explained rather than silently chosen.
    """
    ok = df[(df["status"] == "SUCCESS") & (df["amount"] > 0)].copy()
    ok["d"] = ok["txn_ts"].dt.date
    g = ok.groupby(["merchant_id", "d"], as_index=False).agg(
        txn_count=("txn_id", "size"), gross_amount=("amount", "sum"))
    # Apply the shortfall on each merchant's TOTAL, then apportion it back across
    # days. Rounding per day silently erased the gap for low-volume merchants,
    # which made the "two sources disagree" scenario untestable.
    g["txn_count"] = g["txn_count"].astype(int)
    for mid, pct in SETTLEMENT_MISMATCH.items():
        rows = g.index[g["merchant_id"] == mid]
        if len(rows) == 0:
            raise ValueError(f"SETTLEMENT_MISMATCH references unknown merchant {mid}")
        daily = g.loc[rows, "txn_count"].to_numpy()
        target = int(round(daily.sum() * (1 - pct)))
        g.loc[rows, "txn_count"] = _apportion(target, daily)
    g["gross_amount"] = g["gross_amount"].round(2)
    g["fees"] = (g["gross_amount"] * 0.0195).round(2)
    g["tax"] = (g["fees"] * 0.18).round(2)
    g["net_amount"] = (g["gross_amount"] - g["fees"] - g["tax"]).round(2)
    g["settlement_id"] = [f"S-{_h(m, d):08x}" for m, d in zip(g["merchant_id"], g["d"])]
    g["settlement_date"] = [d + timedelta(days=2) for d in g["d"]]
    g["status"] = ["SETTLED" if (END_DATE - d).days > 3 else "PENDING" for d in g["d"]]
    return g[["settlement_id", "merchant_id", "settlement_date", "txn_count",
              "gross_amount", "fees", "tax", "net_amount", "status"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--db", default=str(root / "data" / "paylens.duckdb"))
    ap.add_argument("--datasets", default=str(root / "datasets"))
    args = ap.parse_args()

    rng = np.random.default_rng(SEED)
    cal = calibrate_declines()
    print(f"PayLens generator — {START_DATE} .. {END_DATE} ({DAYS} days), seed={SEED}")
    print(f"calibrated to NPCI: UPI approval {UPI_APPROVAL_PCT:.2f}% "
          f"(BD {100 - UPI_APPROVAL_PCT - 0.24:.2f}% + TD 0.24%)")

    pms, banks, merchants, fcodes = (dim_payment_method(), dim_bank(cal),
                                     dim_merchant(), dim_failure_code())
    terms = dim_terminal(merchants)
    print(f"  terminals            {len(terms):>9,}  "
          f"({(terms['health'] == 'degraded').sum()} degraded, "
          f"{(terms['connectivity'] == 'sim').sum()} on SIM)")
    txns = build_transactions(rng, cal, merchants, terms)
    print(f"  clean attempts       {len(txns):>9,}")
    incident = plant_acquirer_incident(txns, rng)
    txns, n_dup = plant_duplicates(txns, rng)
    n_neg = plant_negative_amounts(txns, rng)
    setts = build_settlements(txns)
    print(f"  with duplicates      {len(txns):>9,}")
    print(f"  settlement batches   {len(setts):>9,}")

    tables = {"dim_payment_method": pms, "dim_bank": banks, "dim_merchant": merchants,
              "dim_failure_code": fcodes, "dim_terminal": terms,
              "fact_transactions": txns, "fact_settlements": setts}

    ds = Path(args.datasets)
    ds.mkdir(parents=True, exist_ok=True)
    CSV_FULL_MAX = 100_000      # keep the repo cloneable
    for name, frame in tables.items():
        frame.to_parquet(ds / f"{name}.parquet", index=False)
        if len(frame) <= CSV_FULL_MAX:
            frame.to_csv(ds / f"{name}.csv", index=False)
        else:
            # Full CSV of fact_transactions is ~91MB. Parquet holds everything;
            # the CSV sample is for a human to eyeball the columns.
            frame.head(5_000).to_csv(ds / f"{name}.sample.csv", index=False)

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    con = duckdb.connect(str(db))
    for name, frame in tables.items():
        con.register(f"_{name}", frame)
        con.execute(f"create table {name} as select * from _{name}")
    con.execute("checkpoint")

    q = lambda s: con.execute(s).fetchone()[0]
    print("\n── Planted defects (each maps to a required failure scenario) ──")
    print(f"  1 duplicate txn_ids        {q('select count(*) from (select txn_id from fact_transactions group by txn_id having count(*)>1)'):>9,}  (+{n_dup:,} rows)")
    print(f"  2 null bank_id             {q('select count(*) from fact_transactions where bank_id is null'):>9,}  ({q('select round(100.0*count(*) filter (where bank_id is null)/count(*),2) from fact_transactions')}%)")
    print(f"  3 settlement disagreement  {len(SETTLEMENT_MISMATCH):>9}  {', '.join(SETTLEMENT_MISMATCH)}")
    print(f"  4 negative amounts         {q('select count(*) from fact_transactions where amount<0'):>9,}")
    print(f"  5 partial ingest (days)    {INCOMPLETE_DAYS:>9}  (~{int(INCOMPLETE_FRACTION*100)}% of expected volume)")
    print(f"  6 mix shift from {SHIFT_START}  netbanking 8% → 15%")
    print(f"  7 acquirer incident        {incident['date']} {incident['window']} "
          f"acquirer={incident['acquirer']}")
    print(f"      {incident['in_window']:,} attempts in window, "
          f"{incident['flipped']:,} successes flipped, "
          f"{incident['relabelled']:,} relabelled to ACQUIRER_DOWN/ISSUER_TIMEOUT")

    print("\n── Failure journey ──")
    for step, label, n, codes in con.execute('''
        select fc.journey_step, fc.journey_step_label, count(*) as n,
               count(distinct fc.failure_code) as codes
        from fact_transactions t join dim_failure_code fc using (failure_code)
        group by 1, 2 order by 1''').fetchall():
        print(f"  step {step:>2} {label:<24} {n:>8,} failures  ({codes} codes)")
    print("  " + "-" * 56)
    for dt, n, pct in con.execute('''
        with t as (select * from fact_transactions
                   qualify row_number() over (partition by txn_id
                                              order by ingested_at desc) = 1)
        select fc.decline_type, count(*),
               round(100.0 * count(*) / (select count(*) from t
                                         where status in ('SUCCESS','FAILED')), 3)
        from t join dim_failure_code fc using (failure_code)
        group by 1 order by 2 desc''').fetchall():
        print(f"  {dt:<12} declines {n:>8,}  = {pct}% of attempts")

    print("\n── Row counts ──")
    for t in tables:
        print(f"  {t:<22} {q(f'select count(*) from {t}'):>9,}")
    con.close()
    print(f"\nduckdb   {db}")
    print(f"datasets {ds}  (csv + parquet)")
    print("\nexpected decomposition (from calibration constants):")
    for k, v in expected_decomposition().items():
        print(f"  {k:<20} {v}")


if __name__ == "__main__":
    main()
