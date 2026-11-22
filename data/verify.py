"""
Verify the generated data reproduces what the calibration promises.

This is the acceptance test for Phase 1. If the mix share drifts away from the
expected value, the demo's headline claim is false and the driver engine will be
built against a signal that is not really there.

Run:  python data/verify.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from npci_calibration import (  # noqa: E402
    ACQUIRER_INCIDENT, JOURNEY_FAILURE_CODES, JOURNEY_STEPS,
    UPI_BUSINESS_DECLINE_PCT, UPI_TECHNICAL_DECLINE_PCT, expected_decomposition,
)
from generate import BASELINE_WEEK, TARGET_WEEK  # noqa: E402

DB = Path(__file__).resolve().parent / "paylens.duckdb"
DEDUP = """
  select * from (
    select *, row_number() over (partition by txn_id order by ingested_at desc) rn
    from fact_transactions) where rn = 1
"""
FAILS: list[str] = []


def check(label: str, actual: float, expected: float, tol: float, unit: str = "pp") -> None:
    ok = abs(actual - expected) <= tol
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label:<42} actual {actual:>9.4f}{unit}  expected {expected:>9.4f}{unit}  tol ±{tol}")
    if not ok:
        FAILS.append(label)


def segments(con, lo, hi) -> dict[str, tuple[float, float]]:
    """{method: (volume_share, success_rate)} over [lo, hi]."""
    rows = con.execute(f"""
        with t as ({DEDUP})
        select pm.method,
               count(*) filter (where t.status in ('SUCCESS','FAILED'))              as denom,
               count(*) filter (where t.status = 'SUCCESS')                          as num
        from t join dim_payment_method pm using (pm_id)
        where t.txn_ts >= ? and t.txn_ts < ? + interval 1 day
        group by 1
    """, [lo, hi]).fetchall()
    total = sum(r[1] for r in rows)
    return {r[0]: (r[1] / total, r[2] / r[1]) for r in rows}


def main() -> int:
    if not DB.exists():
        print(f"missing {DB} — run data/generate.py first")
        return 1
    con = duckdb.connect(str(DB), read_only=True)
    exp = expected_decomposition()

    print("\n=== 1. NPCI calibration reproduced in the data (UPI only) ===")
    bd, td, appr = con.execute(f"""
        with t as ({DEDUP})
        select 100.0 * count(*) filter (where t.status='FAILED' and fc.decline_type='business')
                     / count(*) filter (where t.status in ('SUCCESS','FAILED')),
               100.0 * count(*) filter (where t.status='FAILED' and fc.decline_type='technical')
                     / count(*) filter (where t.status in ('SUCCESS','FAILED')),
               100.0 * count(*) filter (where t.status='SUCCESS')
                     / count(*) filter (where t.status in ('SUCCESS','FAILED'))
        from t join dim_payment_method pm using (pm_id)
                left join dim_failure_code fc on fc.failure_code = t.failure_code
        where pm.method = 'UPI'
    """).fetchone()
    check("UPI business decline rate", bd, UPI_BUSINESS_DECLINE_PCT, 0.35, "%")
    check("UPI technical decline rate", td, UPI_TECHNICAL_DECLINE_PCT, 0.10, "%")

    # NPCI's technical decline counts faults at the bank or at NPCI only. The
    # journey model adds terminal-, gateway- and acquirer-side technical codes,
    # which NPCI would never see. Asserting the comparable SUBSET separately is
    # what keeps the published anchor meaningful instead of quietly redefining it.
    npci_td, all_td = con.execute(f'''
        with t as ({DEDUP})
        select 100.0 * count(*) filter (where t.status='FAILED' and fc.npci_td_scope)
                     / count(*) filter (where t.status in ('SUCCESS','FAILED')),
               100.0 * count(*) filter (where t.status='FAILED'
                                        and fc.decline_type='technical')
                     / count(*) filter (where t.status in ('SUCCESS','FAILED'))
        from t left join dim_failure_code fc on fc.failure_code = t.failure_code
    ''').fetchone()
    print(f"\n  overall technical decline {all_td:.3f}% of attempts, of which "
          f"{npci_td:.3f}pp is in NPCI's definition (bank/NPCI-side only)")
    check("overall technical decline in 1-2% band", all_td, 1.5, 0.5, "%")
    check("NPCI-scope TD is a strict subset", float(npci_td < all_td), 1.0, 0.0, "")
    check("UPI approval rate", appr, 100 - UPI_BUSINESS_DECLINE_PCT - UPI_TECHNICAL_DECLINE_PCT, 0.35, "%")

    print("\n=== 2. The planted incident: mix-vs-rate decomposition ===")
    base = segments(con, *BASELINE_WEEK)
    targ = segments(con, *TARGET_WEEK)

    base_sr = sum(w * s for w, s in base.values())
    targ_sr = sum(w * s for w, s in targ.values())
    mix = sum((targ[m][0] - base[m][0]) * base[m][1] for m in base)
    rate = sum(targ[m][0] * (targ[m][1] - base[m][1]) for m in base)
    delta = targ_sr - base_sr

    print(f"\n  {'rail':<12} {'share base':>11} {'share tgt':>10} {'SR base':>9} {'SR tgt':>9} {'SR drift':>9}")
    for m in sorted(base, key=lambda x: -base[x][0]):
        print(f"  {m:<12} {base[m][0]*100:>10.2f}% {targ[m][0]*100:>9.2f}% "
              f"{base[m][1]*100:>8.2f}% {targ[m][1]*100:>8.2f}% {(targ[m][1]-base[m][1])*100:>8.2f}pp")

    print()
    check("baseline week success rate", base_sr * 100, exp["baseline_sr_pct"], 0.25, "%")
    check("target week success rate", targ_sr * 100, exp["target_sr_pct"], 0.25, "%")
    check("overall delta", delta * 100, exp["delta_pp"], 0.20)
    check("MIX effect", mix * 100, exp["mix_effect_pp"], 0.20)
    check("RATE effect", rate * 100, exp["rate_effect_pp"], 0.20)
    check("decomposition residual (must be ~0)", (delta - mix - rate) * 100, 0.0, 1e-9)
    share = 100 * mix / delta if delta else 0
    check("share of decline explained by MIX", share, exp["mix_share_pct"], 4.0, "%")

    print("\n=== 3. Per-rail rates are FLAT (the whole point) ===")
    worst = max(abs(targ[m][1] - base[m][1]) * 100 for m in base)
    check("largest per-rail SR drift", worst, 0.10, 0.22)

    print("\n=== 4. Planted defects present ===")
    q = lambda s: con.execute(s).fetchone()[0]
    for label, sql, exp_v, tol in [
        ("duplicate txn_ids", "select count(*) from (select txn_id from fact_transactions group by txn_id having count(*)>1)", 1800, 0),
        ("null bank_id rate", "select 100.0*count(*) filter (where bank_id is null)/count(*) from fact_transactions", 2.3, 0.3),
        ("negative amounts", "select count(*) from fact_transactions where amount<0", 47, 0),
        ("settlement-mismatch merchants", """
            with t as (select merchant_id, count(*) n from fact_transactions
                       where status='SUCCESS' and amount>0 group by 1),
                 s as (select merchant_id, sum(txn_count) n from fact_settlements group by 1)
            select count(*) from t join s using (merchant_id) where abs(t.n-s.n) > 0.02*t.n""", 3, 0),
    ]:
        check(label, float(q(sql)), float(exp_v), tol, "")

    lag = q("select datediff('day', max(txn_ts)::date, (select max(txn_ts)::date from fact_transactions)) from fact_transactions")
    full, partial = con.execute("""
        with d as (select txn_ts::date d, count(*) n from fact_transactions group by 1)
        select median(n) filter (where d < (select max(d) from d) - 1),
               avg(n)    filter (where d >= (select max(d) from d) - 1) from d
    """).fetchone()
    check("recent-day ingest fraction", partial / full, 0.40, 0.08, "")

    print("\n=== 5. Failure journey taxonomy ===")
    n_codes, n_steps, n_owners = con.execute('''
        select count(*), count(distinct journey_step), count(distinct owner)
        from dim_failure_code''').fetchone()
    check("failure codes defined", float(n_codes), float(len(JOURNEY_FAILURE_CODES)), 0, "")
    check("journey steps covered", float(n_steps), float(len(JOURNEY_STEPS)), 0, "")
    check("distinct failure owners", float(n_owners), 7.0, 0, "")

    unused = con.execute('''
        select count(*) from dim_failure_code fc
        where not exists (select 1 from fact_transactions t
                          where t.failure_code = fc.failure_code)''').fetchone()[0]
    check("codes with zero observations", float(unused), 0.0, 0, "")

    # A code must never appear on an entry mode where it is physically impossible.
    # This is the check that catches an eligibility-table edit going wrong: the
    # row counts would still look healthy while the data told an impossible story.
    impossible = con.execute('''
        select count(*) from fact_transactions t
        where (t.failure_code in ('CHIP_READ_ERR') and t.entry_mode not in ('chip','swipe'))
           or (t.failure_code = 'TAP_LIMIT'        and t.entry_mode <> 'tap')
           or (t.failure_code = 'QR_GEN_FAIL'      and t.entry_mode <> 'qr')
           or (t.failure_code in ('NETWORK_TIMEOUT','TERM_OFFLINE','READER_FAULT',
                                  'TERM_LOGGED_OUT')
               and t.entry_mode not in ('tap','chip','swipe'))
           or (t.failure_code = 'EMI_INELIGIBLE'   and t.is_emi = false)''').fetchone()[0]
    check("physically impossible code/mode pairs", float(impossible), 0.0, 0, "")

    orphan_terms = con.execute('''
        select count(*) from fact_transactions t
        left join dim_terminal dt using (terminal_id)
        where t.terminal_id is not null and dt.terminal_id is null''').fetchone()[0]
    check("transactions on unknown terminals", float(orphan_terms), 0.0, 0, "")

    # A terminal belongs to a merchant, so the two tables must agree on region.
    mismatch = con.execute('''
        select count(*) from fact_transactions t
        join dim_terminal dt using (terminal_id)
        join dim_merchant m on m.merchant_id = t.merchant_id
        where dt.region <> m.region''').fetchone()[0]
    check("terminal/merchant region disagreement", float(mismatch), 0.0, 0, "")

    print("\n=== 6. The acquirer incident (second planted story) ===")
    inc = ACQUIRER_INCIDENT
    in_sr, out_sr, tech_share = con.execute(f'''
        with t as ({DEDUP}),
        w as (select *,
                (txn_ts::date = date '{inc['date']}'
                 and acquirer_bank_id = '{inc['acquirer_bank_id']}'
                 and hour(txn_ts) >= {inc['hour_from']}
                 and hour(txn_ts) <  {inc['hour_to']}) as in_window
              from t where status in ('SUCCESS','FAILED'))
        select 100.0 * count(*) filter (where in_window and status='SUCCESS')
                     / nullif(count(*) filter (where in_window), 0),
               100.0 * count(*) filter (where not in_window and status='SUCCESS')
                     / nullif(count(*) filter (where not in_window), 0),
               100.0 * count(*) filter (where in_window and failure_code in
                          {tuple(inc['codes'])}) / nullif(count(*) filter (where in_window), 0)
        from w''').fetchone()
    print(f"  success rate inside the 3-hour window {in_sr:.2f}%  "
          f"vs {out_sr:.2f}% everywhere else")
    check("incident window success rate", in_sr, 48.0, 12.0, "%")
    check("incident is a real dip (>25pp)", float(out_sr - in_sr > 25), 1.0, 0.0, "")
    check("window carries the incident codes", float(tech_share > 20), 1.0, 0.0, "")

    # The two stories must stay independent: the incident is outside the weeks the
    # mix decomposition compares, so it cannot contaminate that arithmetic.
    leak = con.execute(f'''
        select count(*) from fact_transactions
        where txn_ts::date = date '{inc['date']}'
          and txn_ts::date between date '{BASELINE_WEEK[0]}' and date '{TARGET_WEEK[1]}'
    ''').fetchone()[0]
    check("incident leaks into the mix-shift weeks", float(leak), 0.0, 0, "")

    print("\n" + "=" * 78)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("ALL CHECKS PASSED — data matches the NPCI calibration and the planted incident.")
    print(f"\nHeadline for the demo: success rate fell {abs(delta)*100:.2f}pp "
          f"({base_sr*100:.2f}% → {targ_sr*100:.2f}%), of which {share:.1f}% is TRAFFIC MIX "
          f"and only {abs(rate)*100:.2f}pp is rails actually degrading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
