"""
Role-based access control, enforced in the SQL AST.

The naive approach is to tell the model "only query merchant M-1042's data". That
is not security, it is a suggestion — defeated by prompt injection, or by the model
simply forgetting on turn seven. Here the model generates whatever it wants and the
guard REWRITES the query before execution.

Two separate mechanisms, both needed:

  row_predicates / inject   restrict which ROWS come back
  scope_violations          refuse a question that NAMES an out-of-scope entity

The second exists because filter injection alone turns "compare me to M-2201" into
a silently empty or self-comparing result. A wrong answer is worse than a refusal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlglot import exp

from semantic.loader import SemanticModel


@dataclass(frozen=True)
class Principal:
    role: str
    claims: dict[str, str] = field(default_factory=dict)

    def claim(self, key: str) -> str | None:
        return self.claims.get(key)


@dataclass(frozen=True)
class ScopePredicate:
    """
    A row filter, anchored on a table the query is guaranteed to contain.

    The subtlety that made the first implementation unsafe: a scope defined on a
    DIMENSION table (region lives on dim_merchant) can only be injected if that
    table is in the query. "success rate by payment method" joins
    dim_payment_method and never dim_merchant — so no predicate was injected and
    a regional manager saw every region.

    Fixing it by injecting a JOIN would mean rewriting arbitrary FROM clauses. A
    semi-join on the FACT table is equivalent, always applicable, and much harder
    to get wrong:

        fact_transactions.merchant_id IN
            (SELECT merchant_id FROM dim_merchant WHERE region = 'South')
    """
    table: str                      # the ANCHOR table — always present in the query
    column: str                     # the anchor's key column
    value: str
    # Set when the scope lives on a dimension table reached via a key.
    lookup_table: str | None = None
    lookup_key: str | None = None
    lookup_column: str | None = None

    @property
    def is_semijoin(self) -> bool:
        return self.lookup_table is not None

    def sql(self, alias: str | None = None) -> str:
        ref = f"{alias or self.table}.{self.column}"
        if self.is_semijoin:
            return (f"{ref} IN (SELECT {self.lookup_key} FROM {self.lookup_table} "
                    f"WHERE {self.lookup_column} = '{self.value}')")
        return f"{ref} = '{self.value}'"

    def describe(self) -> str:
        if self.is_semijoin:
            return f"{self.lookup_table}.{self.lookup_column} = '{self.value}'"
        return f"{self.table}.{self.column} = '{self.value}'"


def principal_for(role: str, claims: dict[str, str] | None = None) -> Principal:
    return Principal(role=role, claims=dict(claims or {}))


def row_predicates(p: Principal, model: SemanticModel) -> list[ScopePredicate]:
    """The row filters this principal must always have applied."""
    rd = model.role(p.role)
    if not rd.row_scope:
        return []
    scope = rd.row_scope
    claim_key = scope.get("claim")
    value = p.claim(claim_key) if claim_key else None
    if value is None:
        raise PermissionError(
            f"role '{p.role}' requires claim '{claim_key}' but it was not supplied")

    anchor = scope.get("anchor_table", "fact_transactions")
    dim_id = scope["dimension"]

    if dim_id in model.dimensions:
        d = model.dimension(dim_id)
        if d.table == anchor:
            # The scope column is on the fact table itself: a direct equality.
            return [ScopePredicate(table=anchor, column=d.column, value=value)]
        # The scope lives on a dimension table. Anchor a semi-join on the fact
        # table so the filter applies whether or not that dimension is joined.
        join = model.join_path(anchor, d.table)
        if join is None:
            raise PermissionError(
                f"role '{p.role}' is scoped by '{dim_id}' on {d.table}, but the "
                f"semantic model declares no join from {anchor} — the scope cannot "
                f"be enforced, so the request is refused rather than run unscoped")
        return [ScopePredicate(table=anchor, column=join.left_on, value=value,
                               lookup_table=d.table, lookup_key=join.right_on,
                               lookup_column=d.column)]

    # Not a modelled dimension — a raw column on the anchor table.
    return [ScopePredicate(table=scope.get("table", anchor), column=dim_id, value=value)]


def masked_columns(p: Principal, model: SemanticModel) -> set[str]:
    return {c.lower() for c in model.role(p.role).masked_columns}


def denied_metrics(p: Principal, model: SemanticModel) -> set[str]:
    return set(model.role(p.role).denied_metrics)


# ─────────────────────────────────────────────────────────────────────────────
# Scope violations — the explicit-refusal path
# ─────────────────────────────────────────────────────────────────────────────
_MERCHANT_ID = re.compile(r"\bM-\d{3,5}\b")


@dataclass
class Entity:
    kind: str           # merchant_id | merchant_name | region | state
    raw: str
    resolved: str | None = None


def named_entities(question: str, model: SemanticModel, known: dict[str, set[str]]) -> list[Entity]:
    """
    Pull entities the user named out of the question. `known` supplies the valid
    values per kind, loaded from the dimension tables.
    """
    found: list[Entity] = []
    for m in _MERCHANT_ID.findall(question.upper()):
        found.append(Entity(kind="merchant_id", raw=m, resolved=m))
    low = question.lower()
    for kind in ("region", "state"):
        for v in known.get(kind, set()):
            if re.search(rf"(?<![a-z]){re.escape(v.lower())}(?![a-z])", low):
                found.append(Entity(kind=kind, raw=v, resolved=v))
    return found


def scope_violations(p: Principal, entities: list[Entity], model: SemanticModel
                     ) -> list[dict]:
    """
    Entities the principal may not see. Returned as dicts so the API layer can turn
    them straight into ScopeViolation models.
    """
    rd = model.role(p.role)
    if not rd.row_scope:
        return []
    scope = rd.row_scope
    dim = scope["dimension"]
    own = p.claim(scope.get("claim", ""))
    out: list[dict] = []

    kind_for_dim = {"merchant_id": "merchant_id", "region": "region", "state": "state"}
    guarded_kind = kind_for_dim.get(dim)
    for e in entities:
        if guarded_kind and e.kind == guarded_kind and e.resolved != own:
            out.append({
                "entity": e.raw,
                "entity_type": e.kind,
                "reason": (f"Your access is limited to {dim} = '{own}'. "
                           f"'{e.raw}' is outside that scope."),
                "allowed_alternative": (
                    "You can compare against your own category or tier benchmark, "
                    "which is aggregated and does not identify other merchants."
                    if e.kind == "merchant_id" else
                    f"You can ask about {dim} = '{own}'."),
            })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# AST injection
# ─────────────────────────────────────────────────────────────────────────────
def _selects_touching(root: exp.Expression, table: str) -> list[tuple[exp.Select, str]]:
    """Every SELECT whose own FROM/JOIN references `table`, with the alias in use."""
    out: list[tuple[exp.Select, str]] = []
    for select in root.find_all(exp.Select):
        sources: list[exp.Table] = []
        # sqlglot >= 26 keys this as "from_"; older releases used "from".
        frm = select.args.get("from_") or select.args.get("from")
        if frm:
            sources += list(frm.find_all(exp.Table))
        for j in select.args.get("joins") or []:
            sources += list(j.find_all(exp.Table))
        for t in sources:
            if (t.name or "").lower() == table.lower():
                out.append((select, t.alias_or_name))
                break
    return out


def inject(root: exp.Expression, preds: list[ScopePredicate]) -> tuple[exp.Expression, list[str]]:
    """
    Add each scope predicate into the AST as a top-level conjunct of every SELECT
    that reads the scoped table. String concatenation would break on queries that
    already have a WHERE, a GROUP BY or a CTE; this does not.
    """
    applied: list[str] = []
    for pred in preds:
        for select, alias in _selects_touching(root, pred.table):
            cond = exp.condition(pred.sql(alias))
            select.where(cond, append=True, copy=False)
            applied.append(pred.sql(alias))
    return root, applied


def _conjuncts(e: exp.Expression | None) -> list[exp.Expression]:
    """Top-level AND conjuncts. Anything under an OR is deliberately NOT included."""
    if e is None:
        return []
    if isinstance(e, exp.And):
        return _conjuncts(e.this) + _conjuncts(e.expression)
    if isinstance(e, exp.Paren):
        return _conjuncts(e.this)
    return [e]


def assert_injected(root: exp.Expression, preds: list[ScopePredicate]) -> tuple[bool, str]:
    """
    Guard check 9. Belt to injection's braces.

    Re-walks the AST and asserts every scoped table carries its predicate as a
    top-level conjunct. A predicate neutralised by `OR 1=1`, or nested inside a
    negation, does not count — which is exactly the escape a "helpful" self-heal
    could otherwise introduce.
    """
    for pred in preds:
        targets = _selects_touching(root, pred.table)
        if not targets:
            continue
        for select, alias in targets:
            where = select.args.get("where")
            cs = _conjuncts(where.this if where else None)
            want = f"{(alias or pred.table).lower()}.{pred.column.lower()}"
            ok = False
            for c in cs:
                col = c.this if isinstance(c, (exp.EQ, exp.In)) else None
                if not isinstance(col, exp.Column):
                    continue
                got = f"{(col.table or pred.table).lower()}.{col.name.lower()}"
                if got != want:
                    continue
                if isinstance(c, exp.EQ):
                    lit = c.expression
                    if isinstance(lit, exp.Literal) and lit.this == pred.value:
                        ok = True
                        break
                elif isinstance(c, exp.In) and pred.is_semijoin:
                    # The value lives inside the sub-select; confirm both the
                    # lookup table and the literal are present in that subtree.
                    sub = c.sql().lower()
                    if (pred.lookup_table.lower() in sub
                            and f"'{pred.value}'".lower() in sub):
                        ok = True
                        break
            if not ok:
                return False, (f"RBAC predicate {pred.column}='{pred.value}' is missing or "
                               f"not a top-level conjunct on {pred.table} (alias {alias})")
    return True, ""


def persona(p: Principal, model: SemanticModel) -> str:
    return model.role(p.role).persona
