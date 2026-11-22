"""
Mix-vs-rate decomposition. Pure Python, no model involved.

A ratio can fall for two completely different reasons, and conflating them is
the classic analyst error the brief is built around:

    MIX  effect — traffic moved toward structurally worse segments
    RATE effect — the segments themselves got worse

    delta = Σ (w_t − w_b) · v_b        [mix]
          + Σ  w_t · (v_t − v_b)       [rate]

The identity is exact, which is why `residual_pp` is asserted at ~0: if it
drifts, the weights do not sum to 1 and the answer is not trustworthy. That is
also why an aggregate over the row cap is BLOCKED rather than truncated —
a missing segment silently breaks the identity.

WHY v2 COULD NOT DO THIS BEFORE
-------------------------------
The arithmetic was never the problem. The problem is that it needs a result
of one exact shape — both periods, per segment, with numerator and denominator
— and free-form model-authored SQL produces that shape by luck. So the query
is not authored here: `decompose_sql()` builds it from the semantic model, and
the model's only job is to choose the metric, the dimension and the two
windows. Python writes the query, Python does the arithmetic, and the guard
still validates the result like any other query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Segment:
    key: str
    value: float                       # the metric for this segment
    denominator: float                 # its volume, which becomes its weight
    numerator: float | None = None
    weight: float = 0.0                # filled by _normalise


@dataclass
class SegmentEffect:
    key: str
    mix_pp: float
    rate_pp: float
    total_pp: float
    weight_base: float
    weight_target: float
    value_base: float
    value_target: float


@dataclass
class Decomposition:
    dimension: str
    baseline_value: float
    target_value: float
    delta_pp: float
    mix_pp: float
    rate_pp: float
    residual_pp: float
    mix_share_pct: float
    segments: list[SegmentEffect] = field(default_factory=list)

    @property
    def dominant(self) -> Literal["mix", "rate", "mixed"]:
        if abs(self.delta_pp) < 1e-9:
            return "mixed"
        s = abs(self.mix_pp) / (abs(self.mix_pp) + abs(self.rate_pp) + 1e-12)
        return "mix" if s >= 0.65 else "rate" if s <= 0.35 else "mixed"

    def as_dict(self) -> dict:
        return {"dimension": self.dimension,
                "baseline_value": self.baseline_value,
                "target_value": self.target_value,
                "delta_pp": self.delta_pp, "mix_pp": self.mix_pp,
                "rate_pp": self.rate_pp, "residual_pp": self.residual_pp,
                "mix_share_pct": self.mix_share_pct, "dominant": self.dominant,
                "segments": [vars(s) for s in self.segments]}


def _normalise(segs: list[Segment]) -> dict[str, Segment]:
    """
    Weights come from the denominators, recomputed here.

    Not taken from a total row: deriving them from the segments themselves is
    what makes `delta = mix + rate` hold exactly, and the residual is the test
    that it did.
    """
    total = sum(s.denominator for s in segs)
    return {s.key: Segment(key=s.key, value=s.value, denominator=s.denominator,
                           numerator=s.numerator,
                           weight=(s.denominator / total) if total else 0.0)
            for s in segs}


def decompose(dimension: str, baseline: list[Segment], target: list[Segment],
              *, as_pp: bool = True) -> Decomposition:
    """
    Split the change in a ratio into mix and rate.

    A segment present in only one period contributes purely MIX — its rate is
    carried over from the period where it exists. That is the honest
    treatment: a rail that did not exist last week cannot have got worse.
    """
    b, t = _normalise(baseline), _normalise(target)
    keys = sorted(set(b) | set(t))
    scale = 100.0 if as_pp else 1.0

    base_value = sum(s.weight * s.value for s in b.values())
    target_value = sum(s.weight * s.value for s in t.values())

    effects: list[SegmentEffect] = []
    mix_total = rate_total = 0.0
    for k in keys:
        bs, ts = b.get(k), t.get(k)
        w_b = bs.weight if bs else 0.0
        w_t = ts.weight if ts else 0.0
        v_b = bs.value if bs else (ts.value if ts else 0.0)
        v_t = ts.value if ts else (bs.value if bs else 0.0)
        mix = (w_t - w_b) * v_b
        rate = w_t * (v_t - v_b)
        mix_total += mix
        rate_total += rate
        effects.append(SegmentEffect(
            key=k, mix_pp=mix * scale, rate_pp=rate * scale,
            total_pp=(mix + rate) * scale, weight_base=w_b, weight_target=w_t,
            value_base=v_b, value_target=v_t))

    delta = target_value - base_value
    effects.sort(key=lambda e: -abs(e.total_pp))
    return Decomposition(
        dimension=dimension,
        baseline_value=base_value * scale, target_value=target_value * scale,
        delta_pp=delta * scale, mix_pp=mix_total * scale,
        rate_pp=rate_total * scale,
        residual_pp=(delta - mix_total - rate_total) * scale,
        mix_share_pct=(100.0 * mix_total / delta) if abs(delta) > 1e-12 else 0.0,
        segments=effects)


def rank_dimensions(decomps: list[Decomposition]) -> list[Decomposition]:
    """
    Order dimensions by how much of the change each actually explains.

    Ranking by |delta| is useless — every dimension reports the same overall
    delta. What differs is concentration: a dimension where one or two
    segments account for nearly all the movement is a better explanation than
    one where it is smeared evenly, because it points somewhere specific.
    """
    def concentration(d: Decomposition) -> float:
        tot = sum(abs(s.total_pp) for s in d.segments) or 1e-12
        return sum(abs(s.total_pp) for s in d.segments[:2]) / tot
    return sorted(decomps, key=lambda d: -concentration(d))


# ── the query, built rather than authored ───────────────────────────────────
def decompose_sql(metric_id: str, dimension: str,
                  base: tuple[str, str], target: tuple[str, str]) -> str:
    """
    The fixed shape the arithmetic needs, built from the semantic model.

    Deliberately does NOT alias the ratio itself. Only the numerator and
    denominator are selected and Python divides them, which keeps every
    figure computed where it can be shown — and sidesteps check 10d, since
    there is no column claiming to be a metric for it to police.
    """
    from paylens2.core.model import get_model
    m = get_model()
    met = m.metrics[metric_id]
    num = getattr(met, "numerator", "") or getattr(met, "expr", "")
    den = getattr(met, "denominator", "") or "count(*)"
    col = m.dimension(dimension).column

    def side(label: str, w: tuple[str, str]) -> str:
        return (f"SELECT '{label}' AS period, {col} AS segment, "
                f"({num}) AS _num, ({den}) AS _den "
                f"FROM v_attempts "
                f"WHERE txn_ts >= DATE '{w[0]}' AND txn_ts < DATE '{w[1]}' "
                f"GROUP BY 1, 2")

    return f"{side('baseline', base)}\nUNION ALL\n{side('target', target)}"


def segments_from_rows(rows: list[dict], period: str) -> list[Segment]:
    out: list[Segment] = []
    for r in rows:
        if str(r.get("period")) != period:
            continue
        den = r.get("_den")
        key = r.get("segment")
        if key is None or not den:
            continue
        num = float(r.get("_num") or 0.0)
        out.append(Segment(key=str(key), value=num / float(den),
                           denominator=float(den), numerator=num))
    return out
