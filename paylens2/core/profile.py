"""
Deterministic profiling of a result set, and the numbered fact pack.

WHY THIS IS NOT A TOOL CALL
---------------------------
The obvious design is to hand the model a calculator and let it work out the
deltas and shares it wants to mention. The problem is provenance, not accuracy: a
number produced by a tool call cannot be shown in an Evidence tab and cannot be
re-derived by a reviewer. Every number this pipeline prints has to trace to the
query that produced it.

So Python computes, here, and the model receives a numbered pack. Whatever is in
the pack it may state; anything else it says is rejected by synth.verify().

The profile also exists because 5,000 rows do not fit in a prompt and would not
help if they did.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from paylens2 import config


@dataclass
class Fact:
    id: str
    label: str
    value: str
    detail: str = ""
    raw: float | None = None


@dataclass
class Profile:
    row_count: int
    truncated: bool
    columns: list[dict] = field(default_factory=list)
    sample: list[dict] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    text: str = ""


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and not (isinstance(v, float) and math.isnan(v))


def _fmt(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if v != v:
            return "null"
        if abs(v) < 1 and v != 0:
            return f"{v:.4f}"
        return f"{v:,.2f}"
    return str(v)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _formatter(formats: dict[str, str] | None):
    """
    Per-column rendering, driven by the semantic model's declared format.

    A ratio metric is stored 0–1 and was being handed to the composer as
    `0.8660`, which then appeared in a business briefing verbatim. The number
    was right and unreadable. Percent and currency formats are declared in
    schema/views.yaml, so the pack can speak the reader's units without the
    model doing any arithmetic to get there.
    """
    formats = formats or {}

    def fmt(col: str, v):
        kind = formats.get(col, "")
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            return _fmt(v)
        if v != v:
            return "null"
        if kind == "percent":
            return f"{v * 100:.2f}%"
        if kind == "currency":
            return f"INR {v:,.2f}" if abs(v) % 1 else f"INR {v:,.0f}"
        return _fmt(v)

    return fmt


def profile(rows: list[dict], *, truncated: bool = False,
            metric_columns: list[str] | None = None,
            formats: dict[str, str] | None = None) -> Profile:
    """
    Summarise a result set and derive the facts worth stating.

    `metric_columns` names the columns that are the ANSWER rather than context,
    which is what decides whether a fact says "success_rate is 86.60%" or merely
    reports a column's range.
    """
    p = Profile(row_count=len(rows), truncated=truncated)
    if not rows:
        p.text = "The query returned no rows."
        p.facts = [Fact(id="E1", label="Rows returned", value="0",
                        detail="an empty result means no attempts matched, "
                               "not that the question was wrong", raw=0)]
        return p

    cols = list(rows[0].keys())
    metric_cols = [c for c in (metric_columns or []) if c in cols]
    numeric: dict[str, list[float]] = {}
    categorical: dict[str, dict[str, int]] = {}

    for c in cols:
        vals = [r.get(c) for r in rows]
        nulls = sum(1 for v in vals if v is None)
        nums = [float(v) for v in vals if _is_num(v)]
        if nums and len(nums) >= max(1, (len(vals) - nulls) // 2):
            numeric[c] = sorted(nums)
            info = {"name": c, "kind": "numeric", "nulls": nulls,
                    "min": min(nums), "max": max(nums),
                    "mean": sum(nums) / len(nums),
                    "median": _percentile(sorted(nums), 0.5),
                    "p95": _percentile(sorted(nums), 0.95)}
        else:
            counts: dict[str, int] = {}
            for v in vals:
                k = "(null)" if v is None else str(v)
                counts[k] = counts.get(k, 0) + 1
            categorical[c] = counts
            info = {"name": c, "kind": "categorical", "nulls": nulls,
                    "distinct": len(counts),
                    "top": sorted(counts.items(), key=lambda kv: -kv[1])[:config.PROFILE_TOP_K]}
        p.columns.append(info)

    if len(rows) <= config.PROFILE_FULL_ROWS:
        p.sample = rows
    else:
        p.sample = rows[:config.PROFILE_FULL_ROWS]

    fmt = _formatter(formats)
    p.facts = _derive_facts(rows, p, metric_cols, numeric, categorical, fmt,
                            formats)
    p.text = _render(p, truncated, fmt)
    return p


def combine(parts: list[tuple[str, Profile]]) -> Profile:
    """
    Merge several evidence queries into one pack, renumbered E1…En.

    An advisory answer rests on two to four small queries rather than one big
    one — where the losses sit, which segment underperforms, how large the
    recoverable pool is. They have to arrive at the composer as a single
    numbered pack, or a citation cannot be resolved unambiguously.

    Each fact keeps the purpose of the query it came from, so the reader can
    see why a number was measured and not merely what it is.
    """
    out = Profile(row_count=0, truncated=False)
    texts: list[str] = []
    for purpose, prof in parts:
        out.row_count += prof.row_count
        out.truncated = out.truncated or prof.truncated
        out.columns.extend(prof.columns)
        for f in prof.facts:
            # "Rows returned" per query is bookkeeping, not evidence; it would
            # fill the pack with numbers nobody wants to cite.
            if f.label == "Rows returned" and len(parts) > 1:
                continue
            out.facts.append(Fact(
                id=f"E{len(out.facts) + 1}", label=f.label, value=f.value,
                detail=(f"{f.detail}; {purpose}" if f.detail and purpose
                        else f.detail or purpose),
                raw=f.raw))
        texts.append((f"── {purpose} ──\n" if purpose else "") + prof.text)
    out.text = "\n\n".join(texts)
    return out


def _derive_facts(rows, p: Profile, metric_cols, numeric, categorical,
                  fmt=None, formats: dict[str, str] | None = None) -> list[Fact]:
    fmt = fmt or (lambda c, v: _fmt(v))
    """
    The pack. Deliberately conservative: every entry is a value that was computed
    here, labelled with what it is. No entry is an interpretation.
    """
    facts: list[Fact] = []

    def add(label: str, value: str, detail: str = "", raw: float | None = None):
        facts.append(Fact(id=f"E{len(facts) + 1}", label=label, value=value,
                          detail=detail, raw=raw))

    add("Rows returned", f"{len(rows):,}",
        "truncated at the row cap" if p.truncated else "", float(len(rows)))

    label_col = next((c["name"] for c in p.columns if c["kind"] == "categorical"), None)

    # A single row is a headline answer: state every numeric column on it.
    if len(rows) == 1:
        for c, vals in numeric.items():
            add(c.replace("_", " "), fmt(c, rows[0].get(c)), "", vals[0] if vals else None)
        return facts

    # Exactly two rows is almost always a comparison — this period against the
    # one before, this rail against that one. The reader wants the CHANGE, and
    # a composer that is not given it either omits the point of the question or
    # computes the subtraction itself and gets rejected for an ungrounded
    # number. So Python does the subtraction, as it does every other one.
    if len(rows) == 2 and (metric_cols or numeric):
        a_row, b_row = rows[0], rows[1]
        a_lab = str(a_row.get(label_col)) if label_col else "first"
        b_lab = str(b_row.get(label_col)) if label_col else "second"
        for c in (metric_cols or list(numeric)):
            av, bv = a_row.get(c), b_row.get(c)
            if not (_is_num(av) and _is_num(bv)):
                continue
            name = c.replace("_", " ")
            add(f"{name} — {a_lab}", fmt(c, av), "", float(av))
            add(f"{name} — {b_lab}", fmt(c, bv), "", float(bv))
            delta = float(bv) - float(av)
            # A ratio's change is quoted in percentage points, because a
            # "1.3% fall" in a rate is ambiguous and a reader will take it
            # as relative when it is absolute.
            if abs(av) <= 1 and abs(bv) <= 1 and c in (metric_cols or []):
                add(f"{name} — change, {a_lab} to {b_lab}",
                    f"{delta * 100:+.2f}pp", "percentage points, not percent",
                    delta * 100)
            else:
                add(f"{name} — change, {a_lab} to {b_lab}", f"{delta:+,.2f}",
                    "", delta)
            if av:
                rel = (float(bv) - float(av)) / abs(float(av)) * 100
                add(f"{name} — relative change", f"{rel:+.2f}%",
                    f"of the {a_lab} value", rel)
        return facts

    # Many rows: for each metric column, the extremes and the spread, named by
    # their label column. "76.06%" is useless without "Fino Payments Bank".
    targets = metric_cols or list(numeric)
    for c in targets:
        if c not in numeric or not numeric[c]:
            continue
        vals = numeric[c]
        hi_row = max(rows, key=lambda r: (r.get(c) is not None, r.get(c)))
        lo_row = min(rows, key=lambda r: (r.get(c) is None, r.get(c)))
        hi_lab = str(hi_row.get(label_col)) if label_col else "highest"
        lo_lab = str(lo_row.get(label_col)) if label_col else "lowest"
        add(f"{c.replace('_', ' ')} — highest ({hi_lab})", fmt(c, hi_row.get(c)),
            "", hi_row.get(c) if _is_num(hi_row.get(c)) else None)
        add(f"{c.replace('_', ' ')} — lowest ({lo_lab})", fmt(c, lo_row.get(c)),
            "", lo_row.get(c) if _is_num(lo_row.get(c)) else None)
        total = sum(vals)
        if c.endswith(("_count", "count", "attempts", "amount", "gmv", "volume")):
            add(f"{c.replace('_', ' ')} — total across all rows", fmt(c, total),
                f"over {len(rows):,} rows", total)
            if len(vals) > 1 and total:
                share = (max(vals) / total) * 100
                add(f"{c.replace('_', ' ')} — largest row's share of the total",
                    f"{share:.1f}%", f"{hi_lab}", share)
        elif (formats or {}).get(c) == "percent":
            # NO unweighted mean for a ratio. The average of five rails'
            # success rates is not the success rate: it weights a rail with
            # 1,468 attempts the same as one with 26,938. Asked for "last
            # week" that mean is 85.91% while the real figure is 86.60%.
            #
            # Labelling it "unweighted" was not enough — a composition read
            # the label and still called it "the overall average", which is
            # the exact class of denominator error this system exists to
            # prevent. So the fact is not produced at all, and the grounding
            # check does the rest: a number that is not in the pack cannot be
            # stated. An honest gap beats a plausible wrong number.
            pass
        else:
            add(f"{c.replace('_', ' ')} — average across rows", fmt(c, total / len(vals)),
                "unweighted; segment sizes differ", total / len(vals))

    if label_col and label_col in categorical:
        add(f"Distinct {label_col.replace('_', ' ')} values",
            f"{len(categorical[label_col]):,}", "", float(len(categorical[label_col])))
    return facts


def _render(p: Profile, truncated: bool, fmt=None) -> str:
    fmt = fmt or (lambda c, v: _fmt(v))
    out = [f"rows: {p.row_count:,}" + ("  (TRUNCATED at the row cap)" if truncated else "")]
    out.append("")
    out.append("columns:")
    for c in p.columns:
        if c["kind"] == "numeric":
            out.append(f"  {c['name']:28} numeric   min {fmt(c['name'], c['min'])}  "
                       f"median {fmt(c['name'], c['median'])}  "
                       f"max {fmt(c['name'], c['max'])}  nulls {c['nulls']}")
        else:
            top = ", ".join(f"{k}={v:,}" for k, v in c["top"][:5])
            out.append(f"  {c['name']:28} category  {c['distinct']} distinct  "
                       f"nulls {c['nulls']}")
            if top:
                out.append(f"  {'':28} top: {top}")
    if p.sample:
        out.append("")
        shown = len(p.sample)
        out.append(f"rows ({shown} of {p.row_count:,}):" if shown < p.row_count
                   else f"all {shown} rows:")
        keys = list(p.sample[0].keys())
        out.append("  " + " | ".join(keys))
        for r in p.sample:
            out.append("  " + " | ".join(fmt(k, r.get(k)) for k in keys))
    return "\n".join(out)
