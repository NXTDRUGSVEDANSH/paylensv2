"""
Terminal report renderer for evaluation results.
Produces formatted tables for both BIRD and own-query evaluation.
"""
from __future__ import annotations


def _bar(value: float, width: int = 20) -> str:
    filled = round(value * width)
    return "#" * filled + "." * (width - filled)


def _pct(v, fmt="{:6.1f}%"):
    if v is None:
        return "   N/A"
    return fmt.format(v * 100)


def print_bird_report(pipeline_metrics: dict[str, dict]) -> None:
    print("\n" + "=" * 70)
    print("  BIRD EVALUATION REPORT")
    print("=" * 70)

    for name, m in pipeline_metrics.items():
        print(f"\n  Pipeline: {name}")
        print(f"  {'-' * 50}")
        ex = m.get("ex", 0)
        fs_ex = m.get("first_shot_ex", 0)
        healed = m.get("healed_ex")
        ves = m.get("ves", 0)
        n = m.get("n_total", 0)

        print(f"  EX (Execution Accuracy) : {ex*100:6.1f}%  {_bar(ex)}")
        print(f"  First-shot EX           : {fs_ex*100:6.1f}%  {_bar(fs_ex)}")
        if healed is not None:
            print(f"  Healed EX               : {healed*100:6.1f}%  {_bar(healed)}")
        print(f"  VES                     : {ves*100:6.1f}%  {_bar(ves)}")
        print(f"  Queries evaluated       : {n}")

        eb = m.get("error_breakdown", {})
        if eb:
            print(f"  Error breakdown:")
            for etype, count in eb.items():
                if etype not in ("total_errors", "error_rate"):
                    print(f"    {etype:<22}: {count}")

    print("\n" + "=" * 70)


def print_own_report(pipeline_metrics: dict[str, dict]) -> None:
    """
    pipeline_metrics: {pipeline_name: {esr, healing_efficiency, p50_ms, ...}}
    Multi-pipeline comparison.
    """
    print("\n" + "=" * 70)
    print("  OWN QUERY EVALUATION REPORT")
    print("=" * 70)

    for name, m in pipeline_metrics.items():
        print(f"\n  Pipeline: {name}")
        print(f"  {'-' * 50}")

        esr = m.get("esr", 0)
        he  = m.get("healing_efficiency")
        p50 = m.get("p50_ms", 0)
        p95 = m.get("p95_ms", 0)
        mean_ves = m.get("mean_ves_static", 0)
        n   = m.get("n_total", 0)

        print(f"  ESR                     : {esr*100:6.1f}%  {_bar(esr)}")
        if he is not None:
            print(f"  Healing Efficiency      : {he*100:6.1f}%  {_bar(he)}")
        else:
            print(f"  Healing Efficiency      :    N/A  (no healing)")
        print(f"  P50 Latency             : {p50:7.1f} ms")
        print(f"  P95 Latency             : {p95:7.1f} ms")
        print(f"  VES (static, mean)      : {mean_ves*100:6.1f}%  {_bar(mean_ves)}")
        print(f"  Queries evaluated       : {n}")

        n_perfect = m.get("n_ves_perfect", 0)
        n_pen     = m.get("n_ves_penalised", 0)
        print(f"  VES perfect SQL         : {n_perfect}")
        print(f"  VES penalised SQL       : {n_pen}")

        judge_score = m.get("mean_llm_judge_score")
        if judge_score is not None:
            print(f"\n  -- LLM Judge (G-Eval, Qwen) --")
            print(f"  Composite Judge Score   : {judge_score*100:6.1f}%  {_bar(judge_score)}")
            tp = m.get("mean_temporal_precision")
            sa = m.get("mean_schema_alignment")
            fa = m.get("mean_faithfulness")
            co = m.get("mean_completeness")
            if tp is not None: print(f"    Temporal Precision    : {tp*100:6.1f}%  {_bar(tp)}")
            if sa is not None: print(f"    Schema Alignment      : {sa*100:6.1f}%  {_bar(sa)}")
            if fa is not None: print(f"    Faithfulness          : {fa*100:6.1f}%  {_bar(fa)}")
            if co is not None: print(f"    Completeness          : {co*100:6.1f}%  {_bar(co)}")
            n_judged = m.get("n_judged", 0)
            n_jerr   = m.get("n_judge_errors", 0)
            print(f"  Judged / errors         : {n_judged} / {n_jerr}")

    # Comparison table if multiple pipelines
    names = list(pipeline_metrics.keys())
    if len(names) >= 2:
        print(f"\n  {'='*50}")
        print(f"  PIPELINE COMPARISON")
        print(f"  {'='*50}")

        # Header
        col_w = 14
        header = f"  {'Metric':<26}"
        for n in names:
            header += f" {n:>{col_w}}"
        print(header)
        print(f"  {'-'*26}" + f" {'-'*col_w}" * len(names))

        # Rows
        rows = [
            ("ESR",              "esr",              True),
            ("Healing Eff.",     "healing_efficiency",True),
            ("P50 Latency (ms)", "p50_ms",           False),
            ("P95 Latency (ms)", "p95_ms",           False),
            ("VES Static",       "mean_ves_static",  True),
            ("Judge Composite",  "mean_llm_judge_score", True),
            ("  Temporal",       "mean_temporal_precision", True),
            ("  Schema Align",   "mean_schema_alignment", True),
            ("  Faithfulness",   "mean_faithfulness", True),
            ("  Completeness",   "mean_completeness", True),
        ]

        for label, key, is_pct in rows:
            line = f"  {label:<26}"
            for n in names:
                v = pipeline_metrics[n].get(key)
                if v is None:
                    line += f" {'N/A':>{col_w}}"
                elif is_pct:
                    line += f" {v*100:>{col_w-1}.1f}%"
                else:
                    line += f" {v:>{col_w}.1f}"
            print(line)

    print("\n" + "=" * 70)


def save_json_report(data: dict, path: str) -> None:
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"[report] saved -> {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Markdown and CSV emitters, added for the PayLens report.
#
# Tables are built by hand rather than with `tabulate`: it is not in the venv,
# the venv has no pip, and a report generator that cannot run because of a
# missing formatting library is a bad trade for two-space alignment.
# ─────────────────────────────────────────────────────────────────────────────
from evaluation.runners.prior_art import PRIOR_ART  # noqa: E402


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    aligns = ["---" if i == 0 else "---:" for i in range(len(headers))]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(aligns) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _pctv(v, dp=2):
    return "n/a" if v is None else f"{v * 100:.{dp}f}%"


def _msv(v):
    return "n/a" if v is None else f"{v:,.0f} ms"


def _num(v, dp=1):
    return "n/a" if v is None else f"{v:.{dp}f}"


def write_markdown(out_dir, env: dict, *, own: dict | None = None,
                   bird: dict | None = None, cache: dict | None = None) -> None:
    """The three reference tables plus a fourth the earlier harness could not fill."""
    from pathlib import Path
    v2 = (own or {}).get("paylens_v2", {})
    base = (own or {}).get("naive_baseline", {})
    bv2 = (bird or {}).get("paylens_v2_author_guard", {})
    bbase = (bird or {}).get("naive_baseline", {})
    pa = PRIOR_ART

    L = ["## EVALUATION METRICS", ""]
    L.append(f"Run `{env.get('run_id')}` · evaluation date pinned to "
             f"`{env.get('eval_today')}` · NLQ→SQL cache "
             f"{'ON' if env.get('v2_cache_enabled') else 'off'} · KB write-back off "
             f"(corpus frozen at {env.get('kb_count_before')} records"
             + (", unchanged" if env.get("kb_unchanged") else "") + ")")
    L.append("")
    L.append(f"author/synth `{env.get('author_model')}` · judge "
             f"`{env.get('judge_model')}` · semantic model "
             f"`{env.get('semantic_model')}` · commit `{env.get('git_sha')}`"
             + ("  \n**PARTIAL RUN** — interrupted before completion."
                if env.get("partial") else ""))
    L.append("")

    # ── Track 1 ─────────────────────────────────────────────────────────────
    L += ["### Track 1 — BIRD mini-dev", ""]
    if bv2:
        L.append(f"{bv2.get('n_total', 0)} questions over "
                 f"{bv2.get('n_databases', '?')} databases.")
        L.append("")
        L.append(_md_table(
            ["Metric", "PayLens v2", "Naive baseline", "Prior art: ours",
             "Prior art: baseline"],
            [["EX", _pctv(bv2.get("ex")), _pctv(bbase.get("ex")),
              _pctv(pa["track1"]["ours"]["ex"]), _pctv(pa["track1"]["baseline"]["ex"])],
             ["First-shot EX", _pctv(bv2.get("first_shot_ex")),
              _pctv(bbase.get("first_shot_ex")),
              _pctv(pa["track1"]["ours"]["first_shot_ex"]),
              _pctv(pa["track1"]["baseline"]["first_shot_ex"])],
             ["Healed EX", _pctv(bv2.get("healed_ex")), "n/a",
              _pctv(pa["track1"]["ours"]["healed_ex"]) + " \\*", "n/a"],
             ["VES", _pctv(bv2.get("ves")), _pctv(bbase.get("ves")),
              _pctv(pa["track1"]["ours"]["ves"]),
              _pctv(pa["track1"]["baseline"]["ves"])]]))
        L.append("")
        L.append("\\* The prior-art healed-EX figure was produced with "
                 "**result-aware healing**: its pipeline received the gold row "
                 "count and gold column names and re-prompted against them. "
                 "PayLens's heal loop sees only the rejection reason from check "
                 "10 and the guard, never anything derived from the answer, so "
                 "the two numbers measure different protocols and a lower "
                 "PayLens figure is not a regression.")
    else:
        L.append("_Not run — BIRD Mini-Dev databases are not present. "
                 "Drop them in `evaluation/datasets/bird_dbs/` and run "
                 "`python -m evaluation.runners.run_bird_eval`._")
        L.append("")
        L.append("Prior art for reference:")
        L.append("")
        L.append(_md_table(["Metric", "Prior art: ours", "Prior art: baseline"],
                           [["EX", _pctv(pa["track1"]["ours"]["ex"]),
                             _pctv(pa["track1"]["baseline"]["ex"])],
                            ["First-shot EX", _pctv(pa["track1"]["ours"]["first_shot_ex"]),
                             _pctv(pa["track1"]["baseline"]["first_shot_ex"])],
                            ["Healed EX", _pctv(pa["track1"]["ours"]["healed_ex"]) + " \\*", "n/a"],
                            ["VES", _pctv(pa["track1"]["ours"]["ves"]),
                             _pctv(pa["track1"]["baseline"]["ves"])]]))
    L.append("")

    # ── Track 2A ────────────────────────────────────────────────────────────
    L += ["### Track 2A — Own queries, operational", ""]
    L.append(f"{v2.get('n_total', 0)} of 400 questions"
             f" ({v2.get('n_measured', 0)} authored SQL; the rest routed to "
             f"advise / explain / clarify / refuse, which never author a query).")
    L.append("")
    L.append(_md_table(
        ["Metric", "PayLens v2", "Naive baseline", "Prior art: ours",
         "Prior art: baseline"],
        [["ESR", _pctv(v2.get("esr")), _pctv(base.get("esr")),
          _pctv(pa["track2a"]["ours"]["esr"]), _pctv(pa["track2a"]["baseline"]["esr"])],
         ["Healing efficiency", _pctv(v2.get("healing_efficiency")),
          _pctv(base.get("healing_efficiency")),
          _pctv(pa["track2a"]["ours"]["healing_efficiency"]), "n/a"],
         ["P50 latency", _msv(v2.get("p50_ms")), _msv(base.get("p50_ms")),
          _msv(pa["track2a"]["ours"]["p50"]), _msv(pa["track2a"]["baseline"]["p50"])],
         ["P95 latency", _msv(v2.get("p95_ms")), _msv(base.get("p95_ms")),
          _msv(pa["track2a"]["ours"]["p95"]), _msv(pa["track2a"]["baseline"]["p95"])],
         ["VES-static (mean)", _pctv(v2.get("mean_ves_static")),
          _pctv(base.get("mean_ves_static")),
          _pctv(pa["track2a"]["ours"]["ves_static"]),
          _pctv(pa["track2a"]["baseline"]["ves_static"])]]))
    L.append("")

    # ── Track 2B ────────────────────────────────────────────────────────────
    L += ["### Track 2B — LLM judge (G-Eval), 0–100", ""]
    L.append(f"Judge `{env.get('judge_model')}`, run on runnable results only: "
             f"{v2.get('n_judged', 0)} judged, {v2.get('n_judge_errors', 0)} "
             f"judge errors. Weights and prompt are byte-identical to the "
             f"prior-art harness so the columns are comparable.")
    L.append("")

    def j(d, k):
        v = d.get(k)
        return "n/a" if v is None else f"{v * 100:.1f}"

    L.append(_md_table(
        ["Criterion", "Weight", "PayLens v2", "Naive baseline",
         "Prior art: ours", "Prior art: baseline"],
        [["Temporal precision", "0.30", j(v2, "mean_temporal_precision"),
          j(base, "mean_temporal_precision"),
          _num(pa["track2b"]["ours"]["temporal"]),
          _num(pa["track2b"]["baseline"]["temporal"])],
         ["Schema alignment", "0.25", j(v2, "mean_schema_alignment"),
          j(base, "mean_schema_alignment"),
          _num(pa["track2b"]["ours"]["schema"]),
          _num(pa["track2b"]["baseline"]["schema"])],
         ["Faithfulness", "0.30", j(v2, "mean_faithfulness"),
          j(base, "mean_faithfulness"),
          _num(pa["track2b"]["ours"]["faithfulness"]),
          _num(pa["track2b"]["baseline"]["faithfulness"])],
         ["Completeness", "0.15", j(v2, "mean_completeness"),
          j(base, "mean_completeness"),
          _num(pa["track2b"]["ours"]["completeness"]),
          _num(pa["track2b"]["baseline"]["completeness"])],
         ["**Composite**", "", f"**{j(v2, 'mean_llm_judge_score')}**",
          f"**{j(base, 'mean_llm_judge_score')}**",
          f"**{_num(pa['track2b']['ours']['composite'])}** \\*\\*",
          f"**{_num(pa['track2b']['baseline']['composite'])}**"]]))
    L.append("")
    L.append("\\*\\* The prior-art \"ours\" composite does not reconcile with "
             "its own criteria: `0.30(79.2) + 0.25(86.0) + 0.30(77.8) + "
             "0.15(77.4) = 80.2`, not 69.6. Its baseline row does reconcile "
             "(60.4), so this is a transcription error in that report. "
             "The PayLens composite above is the mean of per-query composites, "
             "which is the statistic the prior-art harness computed.")
    L.append("")

    # ── beyond the reference set ────────────────────────────────────────────
    L += ["### Beyond the reference set", "",
          "Metrics the earlier harness had no fields to compute. Its pipeline "
          "returned SQL and prose; v2 carries its own working on every answer.",
          ""]
    rows = [
        ["Action-routing accuracy", _pctv(v2.get("action_routing_accuracy")),
         _pctv(base.get("action_routing_accuracy")),
         "did the question reach the right one of six branches"],
        ["Metric-formula fidelity (final)", _pctv(v2.get("mff_final")),
         _pctv(base.get("mff_final")),
         "a rate aliased as a metric used the canonical expression"],
        ["Metric-formula fidelity (first shot)", _pctv(v2.get("mff_first_shot")),
         _pctv(base.get("mff_first_shot")), "...without being corrected"],
        ["Schema precision", _pctv(v2.get("schema_precision")),
         _pctv(base.get("schema_precision")),
         "query named no raw fact_/dim_ table"],
        ["RBAC enforcement", _pctv(v2.get("rbac_enforcement")),
         _pctv(base.get("rbac_enforcement")),
         "scoped principal's query carried its row filter"],
        ["RBAC leak resistance", _pctv(v2.get("rbac_leak_blocked")),
         _pctv(base.get("rbac_leak_blocked")),
         "filter deleted and neutralised; guard check 9 refused both"],
        ["Grounding rate", _pctv(v2.get("grounding_rate")),
         _pctv(base.get("grounding_rate")),
         "every number in the prose was measured"],
        ["Ungrounded-number rate", _pctv(v2.get("ungrounded_number_rate")),
         _pctv(base.get("ungrounded_number_rate")),
         "per-number, not per-answer — lower is better"],
        ["False-refusal rate", _pctv(v2.get("false_refusal_rate")),
         _pctv(base.get("false_refusal_rate")),
         "claimed the data lacks something it holds — lower is better"],
        ["Refusal recall", _pctv(v2.get("refusal_recall")),
         _pctv(base.get("refusal_recall")),
         "out-of-scope and adversarial prompts correctly declined"],
        ["Clarification F1", _pctv(v2.get("clarification_f1")),
         _pctv(base.get("clarification_f1")),
         "asked back when it should, and only then"],
        ["Silent-empty rate", _pctv(v2.get("silent_empty_rate")),
         _pctv(base.get("silent_empty_rate")),
         "zero rows narrated as a finding — lower is better"],
        ["Decomposition identity hold", _pctv(v2.get("identity_hold_rate")),
         "n/a", "delta = mix + rate closed to within 0.01pp"],
        ["DQ surfacing rate", _pctv(v2.get("dq_surfacing_rate")), "n/a",
         "a failing probe reached the reader, not just the trace"],
        ["Mechanical repair rate", _pctv(v2.get("mech_repair_rate")), "n/a",
         "SQL the fixer corrected before the checks saw it"],
        ["LLM calls per query", _num(v2.get("calls_per_query"), 2),
         _num(base.get("calls_per_query"), 2), "cost"],
        ["LLM share of latency", _pctv(v2.get("llm_share_of_latency")),
         _pctv(base.get("llm_share_of_latency")),
         "how much of the wall clock is provider time"],
    ]
    L.append(_md_table(["Metric", "PayLens v2", "Naive baseline", "What it catches"],
                       rows))
    L.append("")
    if cache:
        L += ["### The NLQ→SQL cache", "",
              _md_table(["Metric", "Value"],
                        [[k, _num(v, 2) if isinstance(v, float) else str(v)]
                         for k, v in cache.items()]), ""]
    Path(out_dir, "REPORT.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[report] markdown -> {Path(out_dir, 'REPORT.md')}")


def write_csv(out_dir, results: dict) -> None:
    """Wide per-query CSV — what you sort to find the queries that failed."""
    import csv
    from pathlib import Path
    cols = ["question_id", "pipeline_name", "bucket", "domain", "difficulty",
            "phrasing", "role", "expected_action", "action", "success",
            "failure_kind", "row_count", "latency_ms", "heal_attempts",
            "measured", "grounding_verdict", "n_checks_passed",
            "injected_predicates", "judge_composite", "question"]
    path = Path(out_dir, "per_query.csv")
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for rs in results.values():
            for r in rs:
                w.writerow([
                    r.question_id, r.pipeline_name, r.bucket, r.domain,
                    r.difficulty, r.phrasing, r.role, r.expected_action,
                    r.action, r.success, r.failure_kind, r.row_count,
                    round(r.latency_ms), r.heal_attempts, r.measured,
                    (r.grounding or {}).get("verdict", ""),
                    len(r.checks_passed or []),
                    "; ".join(r.injected_predicates or []),
                    (r.judge or {}).get("llm_judge_score", ""),
                    r.question])
    print(f"[report] per-query csv -> {path}")
