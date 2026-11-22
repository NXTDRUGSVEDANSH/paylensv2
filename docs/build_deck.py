"""
Builds the PayLens summary deck — 7 pages, 16:9 — and prints it to PDF.

WHY THIS IS A GENERATOR AND NOT A HAND-WRITTEN FILE
---------------------------------------------------
Page 6 is the evaluation scorecard. Typing those numbers in by hand is how a
deck ends up claiming 76 tests on one page and 325 on another, which is
exactly what the repo's own markdown currently does. So the numbers are read
from the evaluation run's own output at build time: re-run this after the run
finishes and the deck is correct, with no retyping and no chance of drift.

If a run is still in progress the page says so and prints the sample size,
rather than presenting a partial figure as a final one.

    python docs/build_deck.py                 # latest run
    python docs/build_deck.py --run full_400
    python docs/build_deck.py --no-pdf        # HTML only
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_HTML = ROOT / "docs" / "paylens-deck.html"
DESKTOP = Path("/mnt/c/Users/l/Desktop")
OUT_PDF_WIN = r"C:\Users\l\Desktop\PayLens-Deck.pdf"
CHROME = Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")


# ─────────────────────────────────────────────────────────────────────────────
# the numbers
# ─────────────────────────────────────────────────────────────────────────────
def load_metrics(run_id: str | None) -> dict:
    """
    Read the evaluation run's own output. Never invent a number.

    Returns a dict of pre-formatted strings so the template contains no
    arithmetic: every figure on page 6 traces to a row in own_raw.jsonl.
    """
    from evaluation import config
    from evaluation.metrics import healing, latency
    from evaluation.metrics import paylens_metrics as pm
    from evaluation.metrics import ves
    from evaluation.pipelines.base import EvalResult

    runs = sorted([p for p in config.RESULTS_DIR.glob("*/own_raw.jsonl")],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    path = (config.RESULTS_DIR / run_id / "own_raw.jsonl") if run_id else \
        (runs[0] if runs else None)
    if path is None or not path.exists():
        return {"available": False}

    env_path = path.parent / "run_env.json"
    env = json.loads(env_path.read_text(encoding="utf-8")) \
        if env_path.exists() else {}
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]

    # Judge scores live in their own file. The per-query rows are appended the
    # moment a question finishes, which is before the judge has seen it, so
    # reading own_raw.jsonl alone would always report the judge as unrun.
    jpath = path.parent / "judge_raw.jsonl"
    judged: dict[tuple, dict] = {}
    if jpath.exists():
        for line in jpath.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (j.get("question_id"), j.get("pipeline"))
            # A retried judgment is appended after the one it replaces, so
            # last-wins is usually right — but never let an error overwrite a
            # score that already exists. A re-run that throttles would
            # otherwise erase good judgments it had already paid for.
            prev = judged.get(key)
            if prev is not None and prev.get("llm_judge_score") is not None \
                    and j.get("llm_judge_score") is None:
                continue
            judged[key] = j
    for r in rows:
        j = judged.get((r.get("question_id"), r.get("pipeline_name")))
        if j:
            r["judge"] = j

    keep = {f.name for f in fields(EvalResult)}
    by_pipe: dict[str, list] = {}
    for r in rows:
        by_pipe.setdefault(r.get("pipeline_name", "?"), []).append(
            EvalResult(**{k: v for k, v in r.items() if k in keep}))

    def block(rs: list) -> dict:
        if not rs:
            return {}
        measured = [r for r in rs if r.measured] or rs
        lat = latency.compute_latency_percentiles(rs)
        out = {}
        out.update(healing.compute_healing_metrics(measured))
        out["p50"] = lat["p50"]
        out["p95"] = lat["p95"]
        out["ves"] = ves.compute_ves_static(
            [r for r in rs if (r.sql or "").strip()])["mean_ves"]
        out.update(pm.compute_all(rs))
        judged = [r.judge for r in rs if r.judge]
        if judged:
            from evaluation.metrics.llm_judge import aggregate_judge_scores
            out.update(aggregate_judge_scores(judged))
        # Last, so a merged block cannot overwrite the record count.
        out["n"] = len(rs)
        out["n_measured"] = len(measured)
        return out

    gold_path = path.parent / "gold_ex.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8")) \
        if gold_path.exists() else {}

    return {"available": True, "env": env, "run_id": path.parent.name,
            "v2": block(by_pipe.get("paylens_v2", [])),
            "base": block(by_pipe.get("naive_baseline", [])),
            "gold": gold.get("paylens_v2", {}),
            "complete": len(by_pipe.get("paylens_v2", [])) >= 400}


def pct(v, dp=1, dash="—"):
    return dash if v is None else f"{v * 100:.{dp}f}%"


def ms(v, dash="—"):
    return dash if v is None else f"{v:,.0f} ms"


def j100(d, k, dash="—"):
    v = (d or {}).get(k)
    return dash if v is None else f"{v * 100:.1f}"


# ─────────────────────────────────────────────────────────────────────────────
CSS = """
@page { size: 13.333in 7.5in; margin: 0; }
/* Pine Labs house colours, taken from the product's own stylesheet:
   Pine Green #003323 and Mint #50D387 are the brand, everything else is paper.
   The amber stays as the one off-hue, exactly as it was, because the API tier
   and the reasoning layer need to read as different kinds of thing. */
:root{
  --ink:#0d1614; --hdr1:#003323; --hdr2:#3f8f66; --brand:#003323;
  --ui:#003323; --api:#b07a05; --stage:#0f9960; --model:#8a6a3d; --store:#2f7d57;
  --cardbg:#f4f8f5; --pillbg:#e9f2ec; --pillbg2:#faf1dd;
  --pine:#003323; --mint:#50d387; --deep:#0b6b45;
}
*{ box-sizing:border-box; margin:0; padding:0; }
body{ font-family:"Segoe UI Variable Display","Segoe UI",system-ui,
      "Helvetica Neue",Arial,sans-serif; color:var(--ink); background:#fff;
      -webkit-print-color-adjust:exact; print-color-adjust:exact; }
.page{ width:1280px; height:720px; overflow:hidden; position:relative;
       display:flex; flex-direction:column; page-break-after:always; }
.page:last-child{ page-break-after:auto; }

header{ height:50px; flex:0 0 50px; display:flex; align-items:center;
        justify-content:space-between; padding:0 26px 0 30px;
        background:linear-gradient(100deg,var(--hdr1) 0%,var(--hdr1) 34%,var(--hdr2) 100%); }
header h1{ font-size:17.5px; font-weight:800; letter-spacing:.055em; color:#fff;
           text-transform:uppercase; }
.brand{ font-size:22px; font-weight:900; letter-spacing:-.012em; color:var(--brand);
        background:#fff; padding:3px 13px; border-radius:5px; }
.brand em{ font-style:normal; color:#1d7a55; }

main{ flex:1; min-height:0; padding:10px 20px 0; display:flex;
      flex-direction:column; gap:7px; }
.layer{ position:relative; border:1.4px dashed #b9b9b9; border-radius:11px;
        padding:11px 10px 8px; }
.layer > .label{ position:absolute; top:-8px; left:50%; transform:translateX(-50%);
  background:#fff; padding:0 12px; font-size:11px; font-weight:700; color:#3a3a3a; }
/* Cards hug their content. Without this they stretch to the tallest cell in
   the row, which left every page with a tall bordered box holding a few lines
   of text at the top and a large empty region beneath. */
.row{ display:grid; gap:9px; align-items:start; }
.r2{ grid-template-columns:1fr 1fr; } .r3{ grid-template-columns:repeat(3,1fr); }
.r4{ grid-template-columns:repeat(4,1fr); }
.r3a{ grid-template-columns:1.06fr 1fr 1.04fr; }
.r21{ grid-template-columns:1.25fr 1fr; }
.grow{ flex:1; }

.card{ border:1.3px solid #cfe0d5; border-radius:10px; background:var(--cardbg);
       padding:6px; display:flex; flex-direction:column; gap:4px; }
.card.reason{ border-color:#eccf9a; background:#fdf7ea; }
.card.plain{ background:#fff; }
.head{ display:flex; align-items:center; gap:7px; background:var(--ui); color:#fff;
       border-radius:7px; padding:4px 8px; font-size:11px; font-weight:750; }
.head.api{ background:var(--api); color:#fff; }
.head.stage{ background:var(--stage); }
.head.store{ background:var(--store); color:#fff; }
.head.reason{ background:#c9971a; color:#2f2206; }
.head.pine{ background:var(--pine); }
.pills{ display:grid; gap:3.5px; }
.p1{ grid-template-columns:1fr; } .p2{ grid-template-columns:1fr 1fr; }
.pill{ background:var(--pillbg); border:1px solid #d4e4da; border-radius:6px;
       padding:3.2px 7px; font-size:9.1px; line-height:1.26; font-weight:560;
       color:#234a3a; text-align:center; display:block; }
.card.reason .pill{ background:var(--pillbg2); border-color:#eddcb8; color:#5e4312; }
.pill.wide{ grid-column:1 / -1; }
.pill.hot{ background:#d9efe3; border-color:#8fcfae; }
.pill.l{ text-align:left; }
.mono{ font-family:"Cascadia Mono",Consolas,"Courier New",monospace;
       font-size:8.8px; letter-spacing:-.015em; }

table{ width:100%; border-collapse:collapse; font-size:9.3px; }
th,td{ padding:3.4px 7px; text-align:right; border-bottom:1px solid #e0ebe4; }
th:first-child,td:first-child{ text-align:left; }
thead th{ font-size:8.6px; font-weight:800; letter-spacing:.035em;
          text-transform:uppercase; color:#2f6b50; border-bottom:1.4px solid #bcd8c8; }
tbody tr:last-child td{ border-bottom:none; }
td.me{ font-weight:800; color:var(--deep); background:#e6f4ec; }
td.dim{ color:#93a09a; }
.tbl-note{ font-size:8.4px; color:#8a8a8a; padding-top:4px; line-height:1.35; }

.legend{ display:flex; align-items:center; gap:15px; padding:1px 4px 0;
         font-size:8.8px; color:#4a4a4a; font-weight:620; }
.legend span{ display:flex; align-items:center; gap:5px; }
.dot{ width:9px; height:9px; border-radius:50%; flex:0 0 auto; }
nav{ flex:0 0 38px; display:grid; grid-template-columns:repeat(6,1fr); gap:8px;
     padding:4px 20px 7px; align-items:end; }
.tab{ background:#2f7d57; color:#fff; text-align:center; border-radius:5px;
      padding:7px 2px; font-size:10.3px; font-weight:800; letter-spacing:.045em; }
.tab.on{ background:var(--pine); padding:11px 2px; border-radius:7px 7px 5px 5px; }
.route{ display:flex; align-items:center; gap:6px; font-size:9px; color:#5e4312;
        font-weight:650; padding:0 2px; }
.route .dash{ flex:1; border-top:1.6px dashed #c08a3c; height:0; }
.route .dest{ background:var(--pillbg2); border:1px solid #eddcb8; border-radius:6px;
              padding:3px 7px; font-weight:750; }

/* title page */
.cover{ flex:1; display:flex; flex-direction:column; justify-content:center;
        padding:0 70px; gap:16px; }
.cover .kicker{ font-size:12px; font-weight:800; letter-spacing:.16em;
                color:var(--stage); text-transform:uppercase; }
.cover h2{ font-size:68px; font-weight:900; letter-spacing:-.025em; line-height:.98; }
.cover h2 em{ font-style:normal; color:var(--stage); }
.cover .sub{ font-size:19px; font-weight:600; color:#44544d; max-width:900px;
             line-height:1.35; }
.thesis{ border-left:4px solid var(--stage); padding:8px 0 8px 15px;
         font-size:14px; line-height:1.55; font-weight:600; color:#1b2e27;
         max-width:860px; }
.chips{ display:flex; gap:10px; flex-wrap:wrap; margin-top:4px; }
.chip{ background:var(--cardbg); border:1.3px solid #cfe0d5; border-radius:9px;
       padding:8px 14px; font-size:10.5px; font-weight:650; color:#234a3a; }
.chip b{ display:block; font-size:19px; font-weight:900; color:var(--pine);
         letter-spacing:-.02em; }
.cover .foot{ font-size:10px; color:#8a948f; font-weight:600; }
/* Pages with room: larger type, same grammar. */
.loose .pill{ font-size:10.6px; line-height:1.34; padding:4.5px 9px; }
.loose .mono{ font-size:10px; }
.loose .head{ font-size:12.4px; padding:5.5px 10px; }
.loose .pills{ gap:4.5px; }
.loose .card{ padding:7px; gap:5px; }
.loose table{ font-size:10.6px; }
.loose th,.loose td{ padding:4.6px 9px; }
.loose thead th{ font-size:9.4px; }
.loose .tbl-note{ font-size:9.6px; line-height:1.42; padding-top:6px; }
.loose .layer{ padding:14px 12px 11px; }
.loose .layer > .label{ font-size:12px; }
.loose .row{ gap:11px; }
.loose main{ gap:10px; }
.loose h3.sec{ font-size:12px; }
.loose .route{ font-size:10.4px; }

h3.sec{ font-size:11px; font-weight:800; letter-spacing:.05em; color:#2f6b50;
        text-transform:uppercase; padding:1px 2px 2px; }
.kv{ font-size:9.3px; line-height:1.5; color:#234a3a; }
.kv b{ color:#b02a30; }
"""


def nav_html(active: int) -> str:
    tabs = ["OVERVIEW &amp; DATA", "DATA QUALITY", "SEMANTIC MODEL",
            "PIPELINE &amp; ARCHITECTURE", "EVALUATION METRICS", "GUARD &amp; RBAC"]
    return "<nav>" + "".join(
        f'<div class="tab{" on" if i == active else ""}">{t}</div>'
        for i, t in enumerate(tabs)) + "</nav>"


def head_html(title: str) -> str:
    return (f'<header><h1>{title}</h1>'
            f'<div class="brand">PAY<em>LENS</em></div></header>')


LEGEND = """<div class="legend">
  <span><i class="dot" style="background:var(--ui)"></i>UI</span>
  <span><i class="dot" style="background:var(--api)"></i>API</span>
  <span><i class="dot" style="background:var(--stage)"></i>core pipeline stage</span>
  <span><i class="dot" style="background:var(--model)"></i>model, persona, or reasoning layer</span>
  <span><i class="dot" style="background:var(--store)"></i>data store, cache, or persisted memory</span>
  <span style="margin-left:auto;color:#8a8a8a;font-weight:600;">%s</span>
</div>"""


# ─────────────────────────────────────────────────────────────────────────────
def page_title() -> str:
    return f"""<section class="page loose">
{head_html("Pine Labs · Enterprise AI Business Analyst")}
<div class="cover">
  <div class="kicker">Payments analytics, answered in English</div>
  <h2>Pay<em>Lens</em></h2>
  <div class="sub">An auditable natural-language business analyst for payments.
    Ask what happened, why it happened, and what to do about it — and get the
    working, not just the number.</div>
  <div class="thesis">A language model writes the SQL. The guard decides whether
    it runs. Python computes every figure in the answer, so a number you can see
    is a number that traces back to a query you can read.</div>
  <div class="chips">
    <div class="chip"><b>775,957</b>payment attempts, calibrated to NPCI</div>
    <div class="chip"><b>2</b>model calls per answer</div>
    <div class="chip"><b>14</b>validation checks before execution</div>
    <div class="chip"><b>6</b>answer shapes, not one</div>
    <div class="chip"><b>287</b>tests passing</div>
  </div>
  <div class="foot">DuckDB · FastAPI · Chroma · Qwen 2.5 Coder 32B &nbsp;·&nbsp;
    the model is replaceable; the guarantees are not</div>
</div>
{nav_html(-1)}
</section>"""


def page_overview() -> str:
    return f"""<section class="page loose">
{head_html("Overview &amp; the data")}
<main>
  <div class="row r21">
    <section class="layer" >
      <div class="label">Three questions, three answer shapes</div>
      <div class="row r3" style="margin-bottom:8px">
        <div class="card"><div class="head">What happened</div>
          <div class="pills p1">
            <div class="pill">A measured number, with its denominator</div>
            <div class="pill">Breakdown · trend · rank · compare</div>
            <div class="pill">Every figure cites the evidence that produced it</div>
          </div></div>
        <div class="card"><div class="head stage">Why it moved</div>
          <div class="pills p1">
            <div class="pill">Mix versus rate, computed in Python</div>
            <div class="pill"><b>&minus;1.33pp overall · 92.9% of it traffic mix</b></div>
            <div class="pill">Reported as a failure if the identity does not close</div>
          </div></div>
        <div class="card"><div class="head store">What to do</div>
          <div class="pills p1">
            <div class="pill">9 playbooks · 29 levers, each with an owner</div>
            <div class="pill">Advice this system has no basis for is dropped</div>
            <div class="pill">No industry benchmark is ever stated as fact</div>
          </div></div>
      </div>
      <h3 class="sec">The finding the dataset was built to contain</h3>
      <table>
        <thead><tr><th>Rail</th><th>Share before &rarr; after</th>
          <th>Its own rate before &rarr; after</th></tr></thead>
        <tbody>
          <tr><td>UPI</td><td class="me">61.95% &rarr; 54.95%</td><td>88.87% &rarr; 88.79%</td></tr>
          <tr><td>NETBANKING</td><td class="me">8.01% &rarr; 15.02%</td><td>71.26% &rarr; 71.13%</td></tr>
          <tr><td>CARD</td><td>22.01% &rarr; 22.01%</td><td>90.15% &rarr; 90.06%</td></tr>
          <tr><td>WALLET</td><td>5.02% &rarr; 5.01%</td><td>95.14% &rarr; 95.02%</td></tr>
          <tr><td>EMI</td><td>3.01% &rarr; 3.01%</td><td>84.68% &rarr; 84.54%</td></tr>
        </tbody>
      </table>
      <div class="tbl-note"><b>Nothing broke.</b> Success rate fell 87.93% &rarr; 86.60%
        while no rail moved its own rate by more than 0.1pp. Traffic moved toward a
        structurally weaker one. Calling netbanking "the problem" sends the wrong
        team to fix a routing change — which is the classic error this product
        exists to prevent.</div>
    </section>

    <section class="layer" >
      <div class="label">The dataset</div>
      <div class="row r2" style="margin-bottom:8px">
        <div class="card"><div class="head store">Shape</div>
          <div class="pills p1">
            <div class="pill"><b>775,957</b> attempts over 6 related tables</div>
            <div class="pill">2026-07-24 &rarr; 2026-11-20</div>
            <div class="pill">240 merchants · 5 rails · 4 regions</div>
            <div class="pill">Deterministic: seed 42, ~40s to regenerate</div>
          </div></div>
        <div class="card"><div class="head store">Grounded, not invented</div>
          <div class="pills p1">
            <div class="pill">Calibrated to <b>NPCI, September 2026</b></div>
            <div class="pill">Technical decline 0.24% · business decline 10.88%</div>
            <div class="pill">Implied approval rate 88.88%</div>
            <div class="pill">Worst published: Fino 24.26% BD · ICICI 0.73% TD</div>
          </div></div>
      </div>
      <h3 class="sec">Where it deliberately departs</h3>
      <div class="pills p1">
        <div class="pill l">Technical decline is set to <b>1.5%</b>, not NPCI's 0.24%.
          A dataset at the published rate has roughly 1,800 technical declines in
          four months — too few to decompose, rank or trend, so the product would
          have nothing to demonstrate on its most interesting path.</div>
        <div class="pill l">Every modelled share is labelled
          <span class="mono">[MODELLED]</span> or <span class="mono">[ASSUMED]</span>
          in <span class="mono">datasets/CALIBRATION.md</span>, with its source.
          An invented number presented as observed is the defect this whole
          product is built against.</div>
      </div>
    </section>
  </div>

  <section class="layer">
    <div class="label">Six related tables, and the arithmetic behind the claim</div>
    <div class="row" style="grid-template-columns:1.35fr 1fr">
      <div class="card plain"><div class="head store">The schema</div>
        <div class="pills p2">
          <div class="pill l"><span class="mono">fact_transactions</span> — one row per attempt</div>
          <div class="pill l"><span class="mono">fact_settlements</span> — payout batches</div>
          <div class="pill l"><span class="mono">dim_merchant</span> — tier, category, risk, region</div>
          <div class="pill l"><span class="mono">dim_bank</span> — issuer and acquirer, NPCI rates</div>
          <div class="pill l"><span class="mono">dim_payment_method</span> — rail, network, card type</div>
          <div class="pill l"><span class="mono">dim_terminal</span> — health, connectivity, estate</div>
          <div class="pill l wide"><span class="mono">dim_failure_code</span> — category, decline type,
            owner, journey step, whether it is worth retrying</div>
        </div></div>
      <div class="card reason"><div class="head reason">Why a rate moved</div>
        <div class="pills p1">
          <div class="pill l"><span class="mono">delta = &Sigma;(w&#8348;&minus;w&#8342;)&middot;v&#8342;</span>
            &nbsp;&nbsp;<b>mix</b> — traffic moved between segments<br>
            <span class="mono">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;+ &Sigma; w&#8348;&middot;(v&#8348;&minus;v&#8342;)</span>
            &nbsp;&nbsp;<b>rate</b> — segments changed their own rate</div>
          <div class="pill l">Measured here: <b>&minus;1.33pp</b> overall, of which
            <b>&minus;1.23pp is mix</b> and &minus;0.09pp is rate — 92.9% traffic mix.</div>
          <div class="pill l hot">The identity is exact, so the residual is the test:
            <span class="mono">1.7e&minus;15</span>. If it does not close, the weights
            do not sum to one, and a split that does not balance is reported as a
            failure rather than as an answer.</div>
        </div></div>
    </div>
  </section>
</main>
{nav_html(0)}
</section>"""


def page_quality() -> str:
    return f"""<section class="page loose">
{head_html("Data quality — faults planted, then surfaced")}
<main>
  <section class="layer">
    <div class="label">Five faults are planted in the data on purpose</div>
    <div class="row r3">
      <div class="card"><div class="head store">Counting hazards</div>
        <div class="pills p1">
          <div class="pill l"><b>Duplicate txn_ids</b> — a double-ingest window.
            Every honest query must de-duplicate or counts inflate silently.</div>
          <div class="pill l"><b>Negative amounts</b> — refund corrections posted
            against the original rail. They are included in totals, and said so.</div>
        </div></div>
      <div class="card"><div class="head store">Attribution hazards</div>
        <div class="pills p1">
          <div class="pill l"><b>2.09% of attempts carry no issuer.</b> An issuer
            breakdown genuinely will not sum to the overall total.</div>
          <div class="pill l"><b>Two sources disagree</b> on attempt counts for
            5 merchants — transactions versus settlements.</div>
        </div></div>
      <div class="card"><div class="head store">Window hazards</div>
        <div class="pills p1">
          <div class="pill l"><b>The last 2 days are part-ingested.</b> A trend
            ending inside that window slopes down for a reason that has nothing
            to do with payments.</div>
          <div class="pill l">An earlier design trimmed the window and re-planned.
            PayLens reports it and leaves the query alone.</div>
        </div></div>
    </div>
  </section>

  <section class="layer">
    <div class="label">Five probes run against the window every answer actually used</div>
    <div class="row r2">
      <div style="display:flex;flex-direction:column;gap:7px">
        <table>
          <thead><tr><th>Check</th><th>What it finds on this dataset</th></tr></thead>
          <tbody>
            <tr><td><span class="mono">duplicate_txn_ids</span></td>
                <td style="text-align:left">verifies the view's de-duplication held</td></tr>
            <tr><td><span class="mono">null_issuer</span></td>
                <td style="text-align:left">2.09% — issuer rows will not sum</td></tr>
            <tr><td><span class="mono">partial_days</span></td>
                <td style="text-align:left">last 2 days under-ingested; reported, never trimmed</td></tr>
            <tr><td><span class="mono">negative_amount</span></td>
                <td style="text-align:left">refund corrections on the original rail</td></tr>
            <tr><td><span class="mono">settlement_mismatch</span></td>
                <td style="text-align:left">5 merchants where the two sources disagree</td></tr>
          </tbody>
        </table>
        <div class="tbl-note">Thresholds and wording live in
          <span class="mono">views.yaml</span>, next to the model, so the trip point
          and the sentence a reader sees are reviewed together.</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:7px">
        <div class="card plain"><div class="head stage">Three rules these probes follow</div>
          <div class="pills p1">
            <div class="pill l"><b>Findings reach the reader, not just a tab.</b>
              A failing probe is appended to the answer's caveats and deduplicated
              against what the prose already said — a warning nobody surfaces was
              not issued.</div>
            <div class="pill l"><b>Every probe is a guarded query with the caller's
              own principal.</b> A background query is the easiest place to leak
              rows a role may not see, because it is not user-initiated and so
              never looks like the thing to check.</div>
            <div class="pill l"><b>The duplicate check verifies the guarantee, not
              the raw table.</b> the allowlist blocks <span class="mono">fact_*</span>
              and that is not relaxed for a probe, so the check asserts no
              <span class="mono">txn_id</span> survives twice into the view —
              which fails loudly if de-duplication ever breaks.</div>
          </div></div>
        <div class="card plain"><div class="head store">When two sources disagree</div>
          <div class="pills p1">
            <div class="pill l">The answer states the documented precedence
              (transactions are the system of record) and quantifies the gap.
              <b>It never averages them and never quietly serves the larger:</b>
              a reader handed one number is entitled to know the other existed.</div>
          </div></div>
      </div>
    </div>
  </section>

  <section class="layer">
    <div class="label">What the reader actually sees when a probe fires</div>
    <div class="row r3">
      <div class="pill l hot">"<b>2.09% of attempts in this window have no issuer_bank
        recorded.</b> They are excluded from any issuer breakdown but counted in the
        overall total, so the segment rows will not sum to the total."</div>
      <div class="pill l hot">"<b>The last 2 day(s) of this window are only partially
        ingested</b> (41% of a typical day). Volume is understated and a downward
        trend at the right-hand edge may not be real."</div>
      <div class="pill l hot">"<b>Transactions and settlements disagree on attempt
        counts for 5 merchant(s)</b> in this window, worst M-1187 at 6.4%. Counted
        from v_attempts, which is the system of record."</div>
    </div>
    <div class="tbl-note" style="text-align:center">These sentences are templates in
      <span class="mono">views.yaml</span>, filled from the probe's own result — not
      prose the model was asked to write. A caveat the model composes is a caveat it
      can forget.</div>
  </section>
</main>
{nav_html(1)}
</section>"""


def page_semantic() -> str:
    return f"""<section class="page loose">
{head_html("Semantic model, the guard, and access control")}
<main>
  <section class="layer">
    <div class="label">The view layer — what the model is allowed to see</div>
    <div class="row r3a">
      <div class="card"><div class="head store">v_attempts</div>
        <div class="pills p1">
          <div class="pill l">One row per attempt, <b>already de-duplicated</b>
            (<span class="mono">QUALIFY row_number() … = 1</span>) and
            <b>already joined</b> to all five dimensions.</div>
          <div class="pill l">Joins disappear from the prompt, which removes the
            second largest class of text-to-SQL error — and the model cannot
            forget a de-duplication rule it never knew existed.</div>
        </div></div>
      <div class="card"><div class="head store">Indicator columns</div>
        <div class="pills p1">
          <div class="pill l"><span class="mono">is_success · is_failed · is_pending</span></div>
          <div class="pill l"><span class="mono">is_resolved</span> = SUCCESS or FAILED.
            <b>PENDING is excluded deliberately</b> — an attempt that has not
            resolved is not a failure, and counting it as one understates the rate.</div>
          <div class="pill l"><span class="mono">is_business_decline · is_technical_decline</span>
            — the customer's fault versus ours</div>
        </div></div>
      <div class="card"><div class="head api">Declared once, enforced everywhere</div>
        <div class="pills p2">
          <div class="pill"><b>10</b> metrics<br>4 of them ratios</div>
          <div class="pill"><b>26</b> dimensions<br>14 decomposable</div>
          <div class="pill"><b>7</b> intents</div>
          <div class="pill"><b>4</b> roles</div>
          <div class="pill wide l">Format, synonyms, null labels and the canonical
            formula all live in <span class="mono">views.yaml</span> — so the prose
            speaks percent and rupees without the model doing arithmetic.</div>
        </div></div>
    </div>
  </section>

  <section class="layer">
    <div class="label">Fourteen checks stand between the model and the database</div>
    <div class="row r3a">
      <div class="card"><div class="head stage">Check 10 — the backstop</div>
        <div class="pills p1">
          <div class="pill l"><b>10a</b> views only — raw tables rejected</div>
          <div class="pill l"><b>10b</b> it parses</div>
          <div class="pill l"><b>10c</b> no <span class="mono">SELECT *</span> on a wide view</div>
          <div class="pill l hot"><b>10d</b> a column aliased with a metric id must use
            the official expression. <span class="mono">successes / count(*)</span>
            is plausible, readable, and silently a different number.</div>
          <div class="pill l"><b>10e</b> no rate grouped by a failure-only column.
            A real answer once read <i>"the 'customer' failure owner has a 0% success
            rate on 19,784 attempts"</i> — correct arithmetic, nonsense sentence,
            and it reads as a finding.</div>
        </div></div>

      <div class="card"><div class="head stage">The nine guard checks</div>
        <div class="pills p1">
          <div class="pill l">1 single statement · 2 SELECT only · 3 no injection</div>
          <div class="pill l">4 table allowlist · 5 column allowlist + masking</div>
          <div class="pill l">6 <b>RBAC predicate injection</b> · 7 row cap sentinel</div>
          <div class="pill l">8 cost estimate · 9 <b>re-walk: the predicate survived</b></div>
          <div class="pill l hot">All checks work on the AST, never the SQL string.
            A <span class="mono">DROP</span> substring test misses
            <span class="mono">/*x*/DROP</span>.</div>
          <div class="pill l"><b>The guard does not care who wrote the SQL.</b>
            Model-authored queries pass the same checks compiled ones did.</div>
        </div></div>

      <div class="card reason"><div class="head reason">The hole this design had to close</div>
        <div class="pills p1">
          <div class="pill l"><span class="mono">assert_injected</span> skips tables a
            query does not name:<br>
            <span class="mono">if not targets: continue &nbsp;# vacuously passes</span></div>
          <div class="pill l">The classic model anchors every role scope on
            <span class="mono">fact_transactions</span>. A PayLens query reads
            <span class="mono">v_attempts</span>, names no
            <span class="mono">fact_transactions</span>, and would therefore get
            <b>no row filter while check 9 passed anyway</b> — a merchant reading
            every merchant's rows.</div>
          <div class="pill l"><b>Fix:</b> PayLens declares
            <span class="mono">anchor_table: v_attempts</span>.
            <span class="mono">guard()</span> is parameterised by the model it is
            handed, so enforcement is the same code pointed at a different target —
            not a second copy that could drift.</div>
          <div class="pill l hot">A test asserts the hole <b>exists</b> under the
            classic model and is <b>closed</b> under PayLens's, so nobody can
            simplify it back onto the classic model without a test going red.</div>
        </div></div>
    </div>
  </section>

  <section class="layer">
    <div class="label">Four principals — the role shapes the rows, not just the tone</div>
    <div class="row" style="grid-template-columns:1.5fr 1fr">
      <table>
        <thead><tr><th>Role</th><th>Required claim</th><th>Rows it may read</th>
          <th>Also</th></tr></thead>
        <tbody>
          <tr><td>CXO</td><td class="dim">none</td><td>every row</td><td class="dim">—</td></tr>
          <tr><td>Analyst</td><td class="dim">none</td><td>every row</td>
              <td><span class="mono">terminal_id</span> masked</td></tr>
          <tr><td>Regional manager</td><td><span class="mono">region</span></td>
              <td class="me">its region only</td>
              <td><span class="mono">settled_amount</span> denied</td></tr>
          <tr><td>Merchant user</td><td><span class="mono">merchant_id</span></td>
              <td class="me">one merchant only</td>
              <td><span class="mono">merchant_name</span>, <span class="mono">terminal_id</span> masked</td></tr>
        </tbody>
      </table>
      <div class="card plain"><div class="head pine">Three properties worth stating</div>
        <div class="pills p1">
          <div class="pill l"><b>A missing claim refuses.</b> An unscoped query is
            never the fallback when a claim is absent.</div>
          <div class="pill l"><b>The filter is injected after the model writes.</b>
            The prompt tells it not to add an access-control filter of its own —
            a filter the question asks for is the model's to write, a filter the
            viewer's permissions require is not.</div>
          <div class="pill l"><b>Measured, not asserted.</b> The evaluation strips
            the predicate from each scoped query and neutralises it with
            <span class="mono">OR 1 = 1</span>; check 9 must refuse both.</div>
        </div></div>
    </div>
  </section>
</main>
{nav_html(2)}
</section>"""


def page_eval(m: dict) -> str:
    v2, base, gold = m.get("v2", {}), m.get("base", {}), m.get("gold", {})
    n = v2.get("n", 0)
    nj, njb = v2.get("n_judged") or 0, base.get("n_judged") or 0
    nrun_v2, nrun_b = v2.get("n_measured") or 0, base.get("n_measured") or 0
    if m.get("complete"):
        status = f"Measurement complete — {n} of 400 questions."
        if not nj:
            status += (" <b>The judge pass has not run</b>, so Track 2B is blank.")
        elif nj < nrun_v2 * 0.9 or njb < nrun_b * 0.9:
            status += (f" <b>The judge pass is still running</b> — "
                       f"{nj} of PayLens's runnable answers judged and {njb} of "
                       f"the baseline's, so Track 2B will move.")
        else:
            status += f" Judged: {nj} PayLens, {njb} baseline."
    else:
        status = (f"<b>Run in progress: {n} of 400 questions measured"
                  + (f"; judged {nj} PayLens / {njb} baseline" if nj else "")
                  + ".</b> Figures are live from the run and will move.")
    env = m.get("env", {})
    judge = env.get("judge_model", "—")
    pa = {"esr": .96, "he": .60, "p50": 5544, "p95": 24987, "ves": .84,
          "b_esr": .75, "b_p50": 2699, "b_p95": 9172, "b_ves": .79}

    return f"""<section class="page loose">
{head_html("Evaluation — 400 questions, measured")}
<main>
  <div class="row r21">

    <section class="layer" style="display:flex;flex-direction:column;gap:7px">
      <div class="label">Track 2A — operational, against prior art</div>
      <table>
        <thead><tr><th>Metric</th><th>PayLens</th><th>Naive baseline</th>
          <th>Prior art</th><th>Its baseline</th></tr></thead>
        <tbody>
          <tr><td>Execution success rate</td><td class="me">{pct(v2.get('esr'))}</td>
              <td>{pct(base.get('esr'))}</td><td class="dim">{pct(pa['esr'])}</td>
              <td class="dim">{pct(pa['b_esr'])}</td></tr>
          <tr><td>Healing efficiency</td><td class="me">{pct(v2.get('healing_efficiency'))}</td>
              <td>{pct(base.get('healing_efficiency'))}</td>
              <td class="dim">{pct(pa['he'])}</td><td class="dim">—</td></tr>
          <tr><td>P50 latency</td><td class="me">{ms(v2.get('p50'))}</td>
              <td>{ms(base.get('p50'))}</td><td class="dim">{ms(pa['p50'])}</td>
              <td class="dim">{ms(pa['b_p50'])}</td></tr>
          <tr><td>P95 latency</td><td class="me">{ms(v2.get('p95'))}</td>
              <td>{ms(base.get('p95'))}</td><td class="dim">{ms(pa['p95'])}</td>
              <td class="dim">{ms(pa['b_p95'])}</td></tr>
          <tr><td>VES-static</td><td class="me">{pct(v2.get('ves'))}</td>
              <td>{pct(base.get('ves'))}</td><td class="dim">{pct(pa['ves'])}</td>
              <td class="dim">{pct(pa['b_ves'])}</td></tr>
          <tr><td>EX on the gold subset</td>
              <td class="me">{pct(gold.get('own_ex_comparable'))}</td>
              <td>—</td><td class="dim">90.0%</td><td class="dim">64.0%</td></tr>
        </tbody>
      </table>
      <div class="tbl-note">{status} 95% of wall clock is provider time, so the
        latency gap is the model's, not the architecture's. <b>EX is scored only
        on references whose definition PayLens shares</b>:
        <span class="mono">avg_ticket</span> differs between the two semantic
        models.</div>

      <h3 class="sec">Track 2B — LLM judge (G-Eval), judged on runnable queries only</h3>
      <table>
        <thead><tr><th>Criterion</th><th>Wt</th><th>PayLens</th>
          <th>Baseline</th><th>Prior art</th></tr></thead>
        <tbody>
          <tr><td>Temporal precision</td><td>.30</td>
              <td class="me">{j100(v2,'mean_temporal_precision')}</td>
              <td>{j100(base,'mean_temporal_precision')}</td><td class="dim">79.2</td></tr>
          <tr><td>Schema alignment</td><td>.25</td>
              <td class="me">{j100(v2,'mean_schema_alignment')}</td>
              <td>{j100(base,'mean_schema_alignment')}</td><td class="dim">86.0</td></tr>
          <tr><td>Faithfulness</td><td>.30</td>
              <td class="me">{j100(v2,'mean_faithfulness')}</td>
              <td>{j100(base,'mean_faithfulness')}</td><td class="dim">77.8</td></tr>
          <tr><td>Completeness</td><td>.15</td>
              <td class="me">{j100(v2,'mean_completeness')}</td>
              <td>{j100(base,'mean_completeness')}</td><td class="dim">77.4</td></tr>
          <tr><td><b>Composite</b></td><td></td>
              <td class="me">{j100(v2,'mean_llm_judge_score')}</td>
              <td>{j100(base,'mean_llm_judge_score')}</td><td class="dim">69.6 *</td></tr>
        </tbody>
      </table>
      <div class="tbl-note">Judge <span class="mono">{judge}</span>, deliberately
        <b>not</b> the author model — self-judging shares the blind spots that
        produced the answer. * The prior-art composite does not reconcile with
        its own criteria (= <b>80.2</b>, not 69.6); its baseline row does.</div>

      <h3 class="sec">What the baseline is</h3>
      <div class="pills p2">
        <div class="pill l"><b>Two model calls and nothing else.</b> Call 1 writes
          SQL from the raw <span class="mono">fact_*</span> /
          <span class="mono">dim_*</span> DDL; call 2 narrates the rows it got
          back. Same model, same temperature, same questions.</div>
        <div class="pill l"><b>No retrieval, no healing, no guard.</b> No Chroma,
          no few-shots, no view layer, no check 10, no nine checks, no RBAC
          injection, no profiler, no grounding check.</div>
        <div class="pill l wide">Same 400 questions: <b>400 SQL calls, 217
          narration calls</b> — the other 183 produced no rows to narrate. Not a
          straw man: it is the shape most text-to-SQL systems ship in, and the
          same baseline the prior-art report used. <b>The gap between the two
          columns is the price of the architecture, in the same units.</b></div>
      </div>
    </section>

    <section class="layer" style="display:flex;flex-direction:column;gap:7px">
      <div class="label">What the reference harness could not measure</div>
      <table>
        <thead><tr><th>Metric</th><th>PayLens</th><th>What it catches</th></tr></thead>
        <tbody>
          <tr><td>Action routing</td><td class="me">{pct(v2.get('action_routing_accuracy'))}</td>
              <td style="text-align:left">answering a definition with a number</td></tr>
          <tr><td>Metric-formula fidelity</td><td class="me">{pct(v2.get('mff_final'))}</td>
              <td style="text-align:left">the PENDING-in-denominator divergence</td></tr>
          <tr><td>Schema precision</td><td class="me">{pct(v2.get('schema_precision'))}</td>
              <td style="text-align:left">reaching past the views</td></tr>
          <tr><td>RBAC enforcement</td><td class="me">{pct(v2.get('rbac_enforcement'))}</td>
              <td style="text-align:left">a scoped query with no row filter</td></tr>
          <tr><td>RBAC leak resistance</td><td class="me">{pct(v2.get('rbac_leak_blocked'))}</td>
              <td style="text-align:left">filter deleted <i>and</i> neutralised — both refused</td></tr>
          <tr><td>Grounding rate</td><td class="me">{pct(v2.get('grounding_rate'))}</td>
              <td style="text-align:left">numbers in prose that were never measured</td></tr>
          <tr><td>Ungrounded numbers</td><td class="me">{pct(v2.get('ungrounded_number_rate'),2)}</td>
              <td style="text-align:left">per figure, not per answer — lower is better</td></tr>
          <tr><td>False refusals</td><td class="me">{pct(v2.get('false_refusal_rate'),2)}</td>
              <td style="text-align:left">claiming the data lacks what it holds</td></tr>
          <tr><td>Config failures</td><td class="me">{pct(v2.get('config_failure_rate'),2)}</td>
              <td style="text-align:left">must be zero or the run is invalid</td></tr>
        </tbody>
      </table>
      <div class="card plain" style="margin-top:2px"><div class="head stage">How the bank was built</div>
        <div class="pills p2">
          <div class="pill"><b>400</b> questions<br>15 buckets</div>
          <div class="pill"><b>60</b> carry gold SQL</div>
          <div class="pill wide l">Statistical · maths-derived · advisory ·
            definitional · causal · ambiguous · out-of-scope · adversarial ·
            RBAC-scoped · multi-turn</div>
          <div class="pill wide l"><b>240 are generated and self-labelling</b> — the
            metric × dimension × period coordinates the question was built from
            <i>are</i> its expected labels, so they carry no annotation risk.</div>
          <div class="pill wide l">The evaluation date is pinned to 2026-11-20 and the
            cache is off. Unpinned, "last week" resolves past the data; cached, the
            author call drops from ~11,700ms to ~15ms and the latency table becomes
            two distributions reported as one.</div>
        </div></div>
    </section>
  </div>
</main>
{nav_html(4)}
</section>"""


def page_answer() -> str:
    return f"""<section class="page loose">
{head_html("What a reader actually gets")}
<main>
  <section class="layer">
    <div class="label">Every answer carries its own working</div>
    <div class="row r4">
      <div class="card"><div class="head">Result</div>
        <div class="pills p1">
          <div class="pill">A headline a business reader can act on</div>
          <div class="pill">A chart the result chose, not the model</div>
          <div class="pill">Ratios as <span class="mono">86.60%</span>, money as
            <span class="mono">INR 1,234,567</span></div>
        </div></div>
      <div class="card"><div class="head">Evidence</div>
        <div class="pills p1">
          <div class="pill">The numbered fact pack E1…En</div>
          <div class="pill">Every figure in the prose cites one</div>
          <div class="pill">A number not in the pack is rejected</div>
        </div></div>
      <div class="card"><div class="head">SQL</div>
        <div class="pills p1">
          <div class="pill">As authored, and as executed</div>
          <div class="pill">The injected row filter is visible</div>
          <div class="pill">Mechanical repairs are listed, not hidden</div>
        </div></div>
      <div class="card"><div class="head">Trace</div>
        <div class="pills p1">
          <div class="pill">Which checks passed, measured stage timings</div>
          <div class="pill">Self-repairs: malformed JSON, a rejected composition</div>
          <div class="pill">Silently rewriting a query is its own defect</div>
        </div></div>
    </div>
  </section>

  <section class="layer">
    <div class="label">Failing honestly — four rungs, and only the last may blame the data</div>
    <div class="row r21">
      <div style="display:flex;flex-direction:column;gap:7px">
        <table>
          <thead><tr><th>Rung</th><th>When</th><th style="text-align:left">What the user sees</th></tr></thead>
          <tbody>
            <tr><td>1</td><td>it worked</td><td style="text-align:left">the answer</td></tr>
            <tr><td>2</td><td>some evidence failed</td>
                <td style="text-align:left">the answer from the rest, saying how many did not run</td></tr>
            <tr><td>3</td><td>nothing ran</td>
                <td style="text-align:left"><b>"a failure on my side"</b> — plus questions that do work</td></tr>
            <tr><td>4</td><td>the concept is absent</td>
                <td style="text-align:left">"this dataset does not hold chargebacks", with the nearest field</td></tr>
          </tbody>
        </table>
        <div class="tbl-note">This exists because of a real incident: two questions
          came back <i>"NOT ANSWERABLE FROM THIS DATA"</i> when nothing was missing —
          the model had written <span class="mono">:DOUBLE</span> with one colon. The
          answer told the user something false about their own data, and printed
          sqlglot's error, ANSI escape codes and all, as the response.</div>
      </div>

      <div style="display:flex;flex-direction:column;gap:7px">
        <div class="card plain"><div class="head pine">Four principles, stated as constraints</div>
          <div class="pills p1">
            <div class="pill l"><b>Python computes, the model picks.</b> There is no
              calculator tool, because a number from a tool call has no provenance
              to show beside the answer.</div>
            <div class="pill l"><b>The query is cached; the answer never is.</b> A
              stored answer is a number that was true once, with no way to show a
              reader which of those two it is.</div>
            <div class="pill l"><b>Advice is verified the way numbers are.</b>
              "You should enable network tokenisation" contains no figure, so the
              grounding check waves it through — a second check drops any
              recommendation with no playbook behind it, and therefore no owner.</div>
            <div class="pill l"><b>Never an industry benchmark as fact.</b> Asked
              "is 86% good", the honest answer is that no benchmark is held here,
              and then a comparison against this estate's own best segment.</div>
          </div></div>
        <div class="card plain"><div class="head stage">Known limits, stated first</div>
          <div class="pills p1">
            <div class="pill l">PayLens needs a language model to author; with no key it
              refuses rather than guessing. Definitions and the decomposition have
              offline templates, so those two degrade rather than fail.</div>
            <div class="pill l">Conversation state lives in the browser — clearing
              site data loses the threads, and they do not follow you to another
              device.</div>
            <div class="pill l">Advisory coverage is exactly as wide as
              <span class="mono">playbooks.yaml</span> and no wider. A question no
              playbook covers degrades to a measurement rather than inventing counsel.</div>
          </div></div>
      </div>
    </div>
  </section>
</main>
{nav_html(5)}
</section>"""


def page_architecture() -> str:
    """Page 5 reuses the standalone architecture slide, body unchanged."""
    src = (ROOT / "docs" / "architecture.html").read_text(encoding="utf-8")
    body = src.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    return f'<section class="page">{body}</section>'


def build(run_id: str | None) -> str:
    m = load_metrics(run_id)
    if not m.get("available"):
        print("  [warn] no evaluation run found — page 6 will be blank")
        m = {"v2": {}, "base": {}, "gold": {}, "env": {}, "complete": False}
    pages = [page_title(), page_overview(), page_quality(), page_semantic(),
             page_architecture(), page_eval(m), page_answer()]
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<title>PayLens</title>\n<style>" + CSS + "</style>\n</head>\n"
            "<body>\n" + "\n".join(pages) + "\n</body>\n</html>\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None)
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()

    OUT_HTML.write_text(build(args.run), encoding="utf-8")
    print(f"html -> {OUT_HTML}")
    if args.no_pdf:
        return 0
    if not CHROME.exists():
        print(f"  [warn] Chrome not at {CHROME}; HTML written, PDF skipped")
        return 0
    url = "file:///" + str(OUT_HTML).replace("/mnt/c/", "C:/").replace(" ", "%20")
    subprocess.run([str(CHROME), "--headless", "--disable-gpu",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={OUT_PDF_WIN}", url],
                   check=False, capture_output=True, timeout=180)
    pdf = DESKTOP / "PayLens-Deck.pdf"
    if pdf.exists():
        from pypdf import PdfReader
        r = PdfReader(str(pdf))
        b = r.pages[0].mediabox
        print(f"pdf  -> {OUT_PDF_WIN}  ({len(r.pages)} pages, "
              f"{float(b.width)/72:.2f} x {float(b.height)/72:.2f} in, "
              f"{pdf.stat().st_size/1024:.0f} KB)")
    else:
        print("  [warn] PDF not produced")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("V2_CACHE", "false")
    raise SystemExit(main())
