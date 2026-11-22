# Dataset calibration — synthetic rows, real aggregates

## The problem

There is no public transaction-level bank payments data, because real payment logs are
private. What NPCI publishes is **monthly, bank-level aggregates** — decline rates split
into technical and business, and platform volumes.

So this dataset is **synthetic at row level and calibrated at aggregate level**. Every
constant lives in [`data/npci_calibration.py`](../data/npci_calibration.py) tagged with
its provenance, and [`data/verify.py`](../data/verify.py) asserts the generated data
actually reproduces the published figures.

The claim made in the demo is precisely this and nothing more:

> *The data is synthetic, but calibrated so its UPI decline rates and approval rate match
> NPCI's published figures for September 2026, using real bank names. Success rates for
> cards, netbanking, wallets and EMI are industry-typical assumptions, because NPCI does
> not publish them.*

That distinction is kept visible rather than letting all five rails look equally authoritative.

## Provenance tags

| Tag | Meaning |
|---|---|
| `[NPCI]` | Published NPCI figure, or reported directly from NPCI data |
| `[DERIVED]` | Arithmetic on an `[NPCI]` figure |
| `[MODELLED]` | Not published by NPCI; industry-typical, scaled to reproduce an `[NPCI]` aggregate |
| `[ASSUMED]` | A portfolio-shape choice with no published equivalent |

## What is real

### Decline rates — `[NPCI]`, September 2026, top 20 remitter banks

| Figure | Value |
|---|---|
| Technical decline rate (TD) | **0.24%** (up from 0.18% in August) |
| Business decline rate (BD) | **10.88%** (up from 10.85% in August) |
| Implied approval rate `[DERIVED]` | **88.88%** |
| Highest TD | **ICICI Bank, 0.73%** |
| Highest BD | **Fino Payments Bank, 24.26%** |
| Second highest BD | **Airtel Payments Bank, 23.20%** |

NPCI's definitions, which decide the denominator:

- **Technical decline** — failure on the system side: bank server down, network fault at
  the bank or at NPCI.
- **Business decline** — failure on the customer side: wrong PIN, insufficient funds,
  transaction over a stipulated limit.

Both are failures from a merchant's point of view, so `approval = 100% − BD − TD`. This is
why `semantic_model.yaml` defines `success_rate` over `SUCCESS + FAILED` and excludes
`PENDING` — a pending attempt has not resolved into either class yet.

Longer-run context, top 50 remitter banks: volume-weighted BD rose from 7.40% (Sep 2025)
to 10.81% (Aug 2026), while TD stayed between 0.20% and 0.42%. NPCI circular OC-149
(June 2022) is reported to target TD < 1% and BD < 5% — **unconfirmed against the circular
itself**, so it is carried as a soft benchmark only.

### Platform volumes — `[NPCI]` Retail Payments Statistics

Monthly, millions of transactions, most recent month in the published table:

| System | Volume (Mn) |
|---|---|
| UPI | 24,256.95 |
| IMPS | 1,419.38 |
| NFS ATM cash withdrawal | 988.44 |
| BBPS | 307.90 |
| AePS (inter-bank, micro-ATM) | 300.58 |
| RuPay card at POS | 192.13 |
| RuPay card at e-commerce | 82.64 |

### Bank names — real

All 15 issuers are real Indian banks, including three payments banks (Paytm, Airtel, Fino)
which really do carry much higher business-decline rates than scheduled commercial banks.
That gap is the single most realistic feature of the dataset.

## How the modelled interior is pinned to the real aggregate

NPCI publishes bank-wise decline rates, but reproducing its full table is neither necessary
nor verifiable here. Instead:

1. Each bank gets a **relative** decline profile (`bd_shape`, `td_shape`) — `[MODELLED]`.
2. The three `[NPCI]`-named outliers are **anchored** and held fixed.
3. `calibrate_declines()` solves a single scale factor `k` for all non-anchored banks such
   that the volume-weighted mean lands **exactly** on the published aggregate.

So the aggregate and the known extremes are real; only the distribution between them is
modelled. Verified:

```
volume-weighted BD  10.8800%   (NPCI published 10.88%)
volume-weighted TD   0.2400%   (NPCI published 0.24%)
implied UPI approval 88.8800%  (derived 88.88%)
```

And re-measured from the generated rows, not just the constants:

```
[PASS] UPI business decline rate    actual 10.8921%   expected 10.8800%
[PASS] UPI technical decline rate   actual  0.2357%   expected  0.2400%
[PASS] UPI approval rate            actual 88.8722%   expected 88.8800%
```

## The one place the journey model departs from the published figure

The failure taxonomy was rebuilt around the ten steps of a payment journey, which
introduced technical failure modes NPCI does not measure. Resolved explicitly:

NPCI's technical decline covers faults **at the bank or at NPCI** — issuer down,
issuer timeout, response lost. A logged-out terminal, a faulty card reader, a dead
SIM or a gateway bug never reaches NPCI's switch, so none of them is in that 0.24%.
A card-present estate therefore has strictly more technical failure, and quoting
0.24% as the technical decline of a POS estate would understate it ~5x.

So UPI keeps the NPCI anchors exactly, card-present rails carry the terminal- and
gateway-side codes inside their own modelled failure budget, and
`dim_failure_code.npci_td_scope` marks the codes NPCI would actually count.

| | measured | published |
|---|---|---|
| UPI business decline | 10.89% | **10.88%** `[NPCI]` |
| UPI technical decline | 0.247% | **0.24%** `[NPCI]` |
| UPI approval rate | 88.86% | **88.88%** `[DERIVED]` |
| Overall technical decline, all rails | 1.47% | not published — `[MODELLED]` |
| …of which inside NPCI's definition | 0.47pp | — |

`data/verify.py` asserts each line separately, and the `technical_decline_rate`
metric states in its own `denominator_note` that it is broader than the published
figure. Every invented weight is listed in
[`../ASSUMPTIONS.md`](../ASSUMPTIONS.md#1b-the-payment-journey-and-every-invented-share).

## What is not real, and why

### Non-UPI success rates — `[MODELLED]`

NPCI does not publish success rates for cards, netbanking, wallets or EMI. These are
industry-typical:

| Rail | Rate | Basis |
|---|---|---|
| UPI | 88.88% | `[NPCI]` derived |
| CARD | 90.2% | blended POS (~95%) and e-commerce (~85%) |
| NETBANKING | 71.2% | bank-page redirects; the worst mainstream rail |
| WALLET | 95.1% | pre-funded, so few insufficient-funds declines |
| EMI | 85.4% | extra eligibility and bureau checks |

### Portfolio method mix — `[ASSUMED]`

| Rail | Share |
|---|---|
| UPI | 62% |
| CARD | 22% |
| NETBANKING | 8% |
| WALLET | 5% |
| EMI | 3% |

This is **not** national NPCI share. By national volume UPI is ~97% of NPCI rails, which
would make every other rail statistically invisible and the analysis pointless. This is a
mid-market **merchant-acquirer checkout mix**: UPI-led, cards material, netbanking small,
and EMI present because it is core to this business.

### Issuer sensitivity — `[MODELLED]`

How much a rail inherits its issuer's decline profile: UPI 1.00 (rides the issuer
directly), CARD 0.70, EMI 0.60, NETBANKING 0.55, WALLET 0.15 (pre-funded, barely cares).
After this adjustment each rail is shifted so its volume-weighted rate equals its target
**exactly**, so the decomposition arithmetic stays clean while `issuer_bank` still carries
genuine variation — which is what makes ranking dimensions by explained share a real
operation rather than a formality.

## The planted incident

A routing/checkout change on **2026-11-09** moves 7pp of traffic from UPI to netbanking.
Per-rail success rates stay flat (−0.1pp drift). Measured from the generated data:

| | Baseline week<br>Nov 2–8 | Target week<br>Nov 9–15 |
|---|---|---|
| UPI | 61.94% share @ 88.87% | 54.96% share @ 88.80% |
| CARD | 22.01% @ 90.14% | 22.01% @ 90.06% |
| NETBANKING | 8.01% @ 71.28% | **15.02%** @ 71.11% |
| WALLET | 5.02% @ 95.15% | 5.01% @ 95.02% |
| EMI | 3.01% @ 84.64% | 3.01% @ 84.56% |
| **Overall** | **87.93%** | **86.60%** |

Windows are aligned to ISO weeks (Mon..Sun) so that a user asking *"last week"* lands
exactly on the incident, with *"the week before"* as its baseline — no date fudging in
the demo. 2026-11-20 is a Friday, so the last complete week is Mon 11-09 to Sun 11-15.

```
delta        -1.33pp
MIX effect   -1.23pp   92.9% of the decline   <- traffic moved to a worse rail
RATE effect  -0.10pp    7.1%                  <- rails themselves barely moved
residual      0.00pp
```

**Nothing in the payment stack got worse.** A system that groups by rail and sorts ascending
reports *"netbanking is failing"* — wrong, netbanking was always ~71%. A system that plots
per-rail trends reports *"nothing changed"* — also wrong, the overall number really did fall.
Only the decomposition produces the actionable answer: **look at what changed in routing or
checkout**, because the problem is upstream of payments entirely.

## Planted data defects

Each maps to a required failure scenario. All deliberate, all asserted by `data/verify.py`.

| # | Defect | Measured | Scenario |
|---|---|---|---|
| 1 | Double-ingest window on day 63 | 1,800 duplicate `txn_id` | Duplicated records |
| 2 | Unattributed issuer on UPI/wallet | 2.27% `NULL bank_id` | Missing records |
| 3 | Settlement undercount, 3 merchants | M-1042 7.0%, M-1187 8.8%, M-1108 4.3% | Two sources disagree |
| 4 | Refunds mislabelled as payments | 47 negative `amount` | Range violation |
| 5 | Partial ingest, last 2 days | ~45% of expected volume | Incomplete window |
| 6 | Mix shift from 2026-11-09 (ISO week start) | netbanking 8% → 15% | Decline explained by mix |

## Reproducing

```bash
python data/generate.py     # seed=42, deterministic
python data/verify.py       # asserts every figure above
```

`verify.py` exits non-zero if the data drifts from the calibration, so the demo's headline
claim cannot quietly become false.

## Files

| File | Rows | Grain |
|---|---|---|
| `fact_transactions.{csv,parquet}` | 777,757 | one payment attempt |
| `fact_settlements.{csv,parquet}` | 28,800 | one settlement batch |
| `dim_merchant.{csv,parquet}` | 240 | merchant |
| `dim_bank.{csv,parquet}` | 18 | bank (15 issuer + 3 acquirer) |
| `dim_payment_method.{csv,parquet}` | 8 | payment method variant |
| `dim_failure_code.{csv,parquet}` | 7 | failure code |

`dim_bank` carries `npci_business_decline_pct` and `npci_technical_decline_pct` as
first-class columns, so the calibration is queryable from inside the product rather than
buried in a generator script.

## Sources

- [UPI payment failures rise at top 20 banks in September — MediaNama](https://www.medianama.com/2026/11/223-upi-technical-decline-rate-rises-september-2026/)
- [UPI Technical Declines Rise at Top 20 Banks — BFSI Elets](https://bfsi.eletsonline.com/upi-technical-declines-banks/)
- [UPI Decline Rates by Bank: Aug 2026 (NPCI data) — ZyroAI](https://zyroai.com/upi-decline-rates/)
- [UPI Payment Success Rates — Product Growth](https://productgrowth.in/insights/fintech/upi-payment-success-rates/)
- [Retail Payments Statistics on NPCI Platforms (PDF) — NPCI](https://www.npci.org.in/uploads/Retail_Payments_Statistics_July25_a2a68deb79.pdf)
- [UPI hits new high in May 2026 — ANI](https://www.aninews.in/news/business/upi-hits-new-high-in-may-2026-with-232-billion-transactions-worth-rs-299-trillion-npci-data-shows20260602155337/)
- [Market Monitoring Tool for UPI (PDF) — Dvara Research](https://dvararesearch.com/wp-content/uploads/2023/12/Market-Monitoring-Tool-for-Unified-Payments-Interface-1.pdf)

**Source-quality caveat.** The decline-rate figures are NPCI data as reported by
third-party aggregators (MediaNama, BFSI Elets, ZyroAI), not read from NPCI's own BD/TD
page. The September (10.88%) and August (10.81%) BD figures come from different sources and
are not strictly comparable. NPCI's own *BD/TD & Uptime statistics* page is the
authoritative source and is where these should be re-checked before any external use.
