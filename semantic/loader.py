"""
Loads semantic_model.yaml into typed objects and renders the LLM's schema card.

This is the only module that reads the YAML. The guard's allowlist, the compiler's
joins, RBAC's row scopes and the DQ checks all come from here, so none of them is
maintained twice.
"""
from __future__ import annotations

import functools
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend import config


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    type: str                      # ratio | sum | count | avg
    base: str
    format: str
    decimals: int
    decomposable: bool
    higher_is_better: bool | None
    synonyms: tuple[str, ...]
    expr: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    denominator_note: str | None = None
    # Tables this metric's SQL references besides its own base. A metric that
    # classifies failures has to reach dim_failure_code even when nobody grouped
    # by it, and the compiler only joins tables it can see a reason for — so the
    # reason is declared here rather than discovered by string-matching the expr.
    requires_join: tuple[str, ...] = ()

    @property
    def is_ratio(self) -> bool:
        return self.type == "ratio"


@dataclass(frozen=True)
class Dimension:
    id: str
    label: str
    table: str
    column: str
    synonyms: tuple[str, ...]
    decomposable: bool
    join_via: str | None = None
    rbac_scope: bool = False
    nullable: bool = False
    null_label: str | None = None
    expected_cardinality: int | None = None
    implies_filter: str | None = None
    implies_metric: str | None = None

    @property
    def sql(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class Join:
    left: str
    left_on: str
    right: str
    right_on: str
    type: str = "left"
    alias: str | None = None


@dataclass(frozen=True)
class Dataset:
    id: str
    label: str
    grain: str
    primary_key: str
    time_column: str | None = None
    dedupe_rule: str | None = None
    freshness_lag_days: int = 0
    description: str = ""


@dataclass(frozen=True)
class RoleDef:
    key: str
    label: str
    row_scope: dict | None
    masked_columns: tuple[str, ...]
    denied_metrics: tuple[str, ...]
    persona: str


@dataclass
class SemanticModel:
    version: str
    datasets: dict[str, Dataset]
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    joins: tuple[Join, ...]
    allowed_tables: frozenset[str]
    denied_functions: frozenset[str]
    roles: dict[str, RoleDef]
    data_quality: dict[str, Any]
    intents: dict[str, dict]
    time_grains: dict[str, dict]
    raw: dict = field(repr=False, default_factory=dict)

    # ── lookup ────────────────────────────────────────────────────────────────
    def metric(self, mid: str) -> Metric:
        if mid not in self.metrics:
            raise KeyError(f"unknown metric '{mid}'. Known: {sorted(self.metrics)}")
        return self.metrics[mid]

    def dimension(self, did: str) -> Dimension:
        if did not in self.dimensions:
            raise KeyError(f"unknown dimension '{did}'. Known: {sorted(self.dimensions)}")
        return self.dimensions[did]

    def role(self, key: str) -> RoleDef:
        if key not in self.roles:
            raise KeyError(f"unknown role '{key}'. Known: {sorted(self.roles)}")
        return self.roles[key]

    def is_aggregate_intent(self, intent: str) -> bool:
        return bool(self.intents.get(intent, {}).get("aggregate", True))

    # ── resolution by synonym (deterministic; runs before any LLM call) ────────
    @staticmethod
    def _match(text: str, candidates: dict[str, tuple[str, ...]]) -> list[str]:
        """
        Longest-match-wins over character spans.

        Naive per-term matching double-counts overlapping phrases: "merchant tier"
        also contains "merchant", and "failure category" contains "category" (a
        synonym of merchant_category). That silently adds a second GROUP BY column
        and produces a different — wrong — answer. So every term is matched WITH ITS
        SPAN, longest first, and a term is only accepted if its span does not overlap
        one already taken.
        """
        t = f" {text.lower()} "
        found: list[tuple[int, int, int, str]] = []   # (-len, start, end, id)
        for key, terms in candidates.items():
            for term in terms:
                term = (term or "").strip().lower()
                if len(term) < 2:
                    continue
                for m in re.finditer(
                        rf"(?<![a-z0-9]){re.escape(term)}e?s?(?![a-z0-9])", t):
                    found.append((-len(term), m.start(), m.end(), key))

        found.sort()
        taken: list[tuple[int, int]] = []
        out: list[str] = []
        for _neg, a, b, key in found:
            if any(a < tb and ta < b for ta, tb in taken):   # span overlap
                continue
            taken.append((a, b))
            if key not in out:
                out.append(key)
        return out

    def match_all(self, text: str) -> tuple[list[str], list[str]]:
        """
        Resolve metrics AND dimensions in ONE span-competitive pass.

        Running them as two independent passes lets both claim the same words:
        "failure rate by payment method" gave the `failure_rate` metric (matching
        "failure rate") *and* the `failure_category` dimension (matching
        "failure"), which silently added a GROUP BY and a join that the question
        never asked for.

        A phrase refers to one thing. Longest match wins, whether the winner is a
        metric or a dimension.
        """
        cands: dict[str, tuple[str, ...]] = {}
        for m in self.metrics.values():
            cands[f"m:{m.id}"] = (m.id.replace("_", " "), m.label.lower(), *m.synonyms)
        for d in self.dimensions.values():
            cands[f"d:{d.id}"] = (d.id.replace("_", " "), d.label.lower(), *d.synonyms)
        won = self._match(text, cands)
        return ([k[2:] for k in won if k.startswith("m:")],
                [k[2:] for k in won if k.startswith("d:")])

    def match_metrics(self, text: str) -> list[str]:
        return self.match_all(text)[0]

    def match_dimensions(self, text: str) -> list[str]:
        return self.match_all(text)[1]

    def tables_for(self, metric_ids: list[str], dimension_ids: list[str]) -> set[str]:
        tabs = {self.metric(m).base for m in metric_ids}
        for d in dimension_ids:
            tabs.add(self.dimension(d).table)
        return tabs

    def join_path(self, base: str, target: str) -> Join | None:
        for j in self.joins:
            if j.left == base and j.right == target:
                return j
        return None

    # ── the card the LLM actually sees (never the raw database) ───────────────
    @functools.cached_property
    def schema_card(self) -> str:
        out = [f"# PayLens semantic model (version {self.version})",
               "You may ONLY reference the metric ids and dimension ids listed here.",
               "", "## Metrics"]
        for m in self.metrics.values():
            if m.is_ratio:
                out.append(f"- `{m.id}` — {m.label} [ratio, {m.format}]")
                out.append(f"    numerator:   {m.numerator}")
                out.append(f"    denominator: {m.denominator}")
                if m.denominator_note:
                    out.append(f"    note: {' '.join(m.denominator_note.split())}")
            else:
                out.append(f"- `{m.id}` — {m.label} [{m.type}, {m.format}] expr: {m.expr}")
            if m.synonyms:
                out.append(f"    also called: {', '.join(m.synonyms)}")
            if m.decomposable:
                out.append("    decomposable: yes (eligible for mix-vs-rate analysis)")
        out += ["", "## Dimensions"]
        for d in self.dimensions.values():
            extra = []
            if d.nullable:
                extra.append("NULLABLE — segment rows will not sum to the total")
            if d.rbac_scope:
                extra.append("access-scoped")
            out.append(f"- `{d.id}` — {d.label} ({d.sql})"
                       + (f" [{'; '.join(extra)}]" if extra else ""))
            if d.synonyms:
                out.append(f"    also called: {', '.join(d.synonyms)}")
        out += ["", "## Time grains", "  " + ", ".join(self.time_grains)]
        out += ["", "## Intents"]
        for k, v in self.intents.items():
            out.append(f"- `{k}` — {v.get('label', '')}"
                       + ("  [needs decomposition]" if v.get("needs_decomposition") else ""))
        out += ["", "## Facts and grain"]
        for ds in self.datasets.values():
            if ds.id.startswith("fact"):
                out.append(f"- `{ds.id}` — grain: one {ds.grain}; time column {ds.time_column}"
                           + (f"; freshness lag {ds.freshness_lag_days}d" if ds.freshness_lag_days else ""))
        return "\n".join(out)

    @functools.cached_property
    def fingerprint(self) -> str:
        return hashlib.sha1(
            (self.version + "|" + "|".join(sorted(self.metrics))
             + "|" + "|".join(sorted(self.dimensions))).encode()).hexdigest()[:12]


def _tup(v: Any) -> tuple:
    if v is None:
        return ()
    return tuple(v) if isinstance(v, (list, tuple)) else (v,)


def load(path: Path | str | None = None) -> SemanticModel:
    p = Path(path or config.SEMANTIC_MODEL_PATH)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))

    datasets = {k: Dataset(
        id=k, label=v.get("label", k), grain=v.get("grain", ""),
        primary_key=v.get("primary_key", ""), time_column=v.get("time_column"),
        dedupe_rule=v.get("dedupe_rule"), freshness_lag_days=v.get("freshness_lag_days", 0),
        description=v.get("description", "")) for k, v in raw["datasets"].items()}

    metrics = {k: Metric(
        id=k, label=v.get("label", k), type=v["type"], base=v["base"],
        format=v.get("format", "number"), decimals=v.get("decimals", 2),
        decomposable=bool(v.get("decomposable", False)),
        higher_is_better=v.get("higher_is_better"), synonyms=_tup(v.get("synonyms")),
        expr=v.get("expr"), numerator=v.get("numerator"), denominator=v.get("denominator"),
        denominator_note=v.get("denominator_note"),
        requires_join=_tup(v.get("requires_join"))) for k, v in raw["metrics"].items()}

    dimensions = {k: Dimension(
        id=k, label=v.get("label", k), table=v["table"], column=v["column"],
        synonyms=_tup(v.get("synonyms")), decomposable=bool(v.get("decomposable", False)),
        join_via=v.get("join_via"), rbac_scope=bool(v.get("rbac_scope", False)),
        nullable=bool(v.get("nullable", False)), null_label=v.get("null_label"),
        expected_cardinality=v.get("expected_cardinality"),
        implies_filter=v.get("implies_filter"),
        implies_metric=v.get("implies_metric")) for k, v in raw["dimensions"].items()}

    joins = tuple(Join(**j) for j in raw["joins"])
    roles = {k: RoleDef(
        key=k, label=v.get("label", k), row_scope=v.get("row_scope"),
        masked_columns=_tup(v.get("masked_columns")),
        denied_metrics=_tup(v.get("denied_metrics")),
        persona=" ".join((v.get("persona") or "").split()))
        for k, v in raw["rbac"]["roles"].items()}

    model = SemanticModel(
        version=raw["version"], datasets=datasets, metrics=metrics, dimensions=dimensions,
        joins=joins,
        allowed_tables=frozenset(raw["allowlist"]["tables"]),
        denied_functions=frozenset(f.lower() for f in raw["allowlist"]["functions_denied"]),
        roles=roles, data_quality=raw["data_quality"], intents=raw["intents"],
        time_grains=raw["time_grains"], raw=raw)
    _validate(model)
    return model


def _validate(m: SemanticModel) -> None:
    """Fail at load, not at query time, on an internally inconsistent model."""
    errs: list[str] = []
    for metric in m.metrics.values():
        if metric.base not in m.datasets:
            errs.append(f"metric {metric.id}: base '{metric.base}' is not a dataset")
        if metric.base not in m.allowed_tables:
            errs.append(f"metric {metric.id}: base '{metric.base}' not in allowlist")
        if metric.is_ratio and not (metric.numerator and metric.denominator):
            errs.append(f"metric {metric.id}: ratio needs numerator and denominator")
        if not metric.is_ratio and not metric.expr:
            errs.append(f"metric {metric.id}: non-ratio needs expr")
    for dim in m.dimensions.values():
        if dim.table not in m.allowed_tables:
            errs.append(f"dimension {dim.id}: table '{dim.table}' not in allowlist")
    for j in m.joins:
        for side in (j.left, j.right):
            if side not in m.allowed_tables:
                errs.append(f"join references non-allowlisted table '{side}'")
    for role in m.roles.values():
        scope = role.row_scope
        if scope and "dimension" in scope:
            d = scope["dimension"]
            if d in m.dimensions and not m.dimensions[d].rbac_scope:
                errs.append(f"role {role.key}: scope dimension '{d}' lacks rbac_scope: true")
        for dm in role.denied_metrics:
            if dm not in m.metrics:
                errs.append(f"role {role.key}: denied metric '{dm}' does not exist")
    if errs:
        raise ValueError("semantic model is inconsistent:\n  - " + "\n  - ".join(errs))


@functools.lru_cache(maxsize=1)
def get_model() -> SemanticModel:
    return load()
