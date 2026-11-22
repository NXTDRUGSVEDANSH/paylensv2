"""
Call 1 — authoring. The model writes SQL, asks a question back, or refuses.

The contract is a discriminated union, not "SQL or nothing", because the hardcoded
five-option metric picker it replaces answered a question nobody asked. A model
that can say "do you mean the value you lost, or the value you refunded?" is
strictly better than a menu of five fixed metrics, and a model that can say "this
dataset holds no chargeback data" is better than both.

Every branch is checked:
  sql      -> check 10, then the existing nine-check guard
  clarify  -> each option must be a complete question that re-enters the pipeline
  refuse   -> the missing concept is verified against the schema; a refusal that
              names something which DOES exist is an error worth retrying
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date

from backend.llm import provider
from backend.llm.provider import CallBudget
from paylens2 import config
from paylens2.core import (catalogue, llmjson, model as v2model, periods,
                           prompts, retrieval)

log = logging.getLogger("paylens2.author")

MAX_OPTIONS = 4
MAX_EVIDENCE_QUERIES = 4


@dataclass
class Authored:
    action: str = "sql"            # sql | advise | clarify | refuse | reauthor
    sql: str = ""
    intent: str = "single_value"
    chart: dict = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    playbook_ids: list[str] = field(default_factory=list)
    # advise
    evidence_queries: list[dict] = field(default_factory=list)
    # clarify
    clarify_question: str = ""
    options: list[dict] = field(default_factory=list)
    # refuse
    missing_concept: str = ""
    nearest: list[str] = field(default_factory=list)
    # explain
    terms: list[str] = field(default_factory=list)
    also_measure: str = ""
    # decompose
    dimension: str = ""
    window: str = ""
    baseline: str = ""
    # bookkeeping
    invented_playbooks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Set when the response was internally malformed. The caller asks the model
    # again with this as the complaint, rather than inventing a refusal.
    reauthor_reason: str = ""


def _schema_terms() -> set[str]:
    """Every word the schema actually contains — used to check a refusal."""
    m = v2model.get_model()
    terms: set[str] = set()
    for d in m.dimensions.values():
        terms.add(d.id.lower())
        terms.add(d.column.lower())
        terms.update(s.lower() for s in d.synonyms)
    for met in m.metrics.values():
        terms.add(met.id.lower())
        terms.update(s.lower() for s in met.synonyms)
        terms.add(met.label.lower())
    return terms


def _coerce(raw: dict) -> Authored:
    # Defaulting to "sql" here hid a missing action: it was already a valid
    # value, so the inference below never ran and an advisory response with
    # evidence_queries and no `action` was read as a (SQL-less) query.
    action = str(raw.get("action") or "").strip().lower()
    if action not in ("sql", "advise", "clarify", "refuse", "explain",
                      "decompose"):
        action = ("advise" if raw.get("evidence_queries")
                  else "sql" if raw.get("sql")
                  else "decompose" if raw.get("dimension") and raw.get("baseline")
                  else "explain" if raw.get("terms") else "refuse")

    a = Authored(action=action)
    if action == "sql":
        a.sql = str(raw.get("sql") or "").strip().rstrip(";")
        a.intent = str(raw.get("intent") or "single_value")
        ch = raw.get("chart") or {}
        a.chart = ch if isinstance(ch, dict) else {}
        a.assumptions = [str(x) for x in (raw.get("assumptions") or []) if str(x).strip()]
    elif action == "advise":
        # Cap the fan-out. Each query is a guard pass and a DuckDB scan, and an
        # advisory answer built on seven queries is not seven times better.
        for q in (raw.get("evidence_queries") or [])[:MAX_EVIDENCE_QUERIES]:
            if not isinstance(q, dict):
                continue
            sql = str(q.get("sql") or "").strip().rstrip(";")
            if not sql:
                continue
            a.evidence_queries.append({
                "id": str(q.get("id") or f"q{len(a.evidence_queries) + 1}"),
                "purpose": str(q.get("purpose") or "").strip(),
                "sql": sql})
        a.assumptions = [str(x) for x in (raw.get("assumptions") or []) if str(x).strip()]
    elif action == "decompose":
        a.intent = "explain_change"
        a.sql = ""                       # built from the model, never authored
        a.terms = [str(raw.get("metric") or "").strip()]
        a.dimension = str(raw.get("dimension") or "").strip()
        a.window = str(raw.get("window") or "").strip()
        a.baseline = str(raw.get("baseline") or "").strip()
        a.assumptions = [str(x) for x in (raw.get("assumptions") or []) if str(x).strip()]
    elif action == "explain":
        a.terms = [str(x).strip() for x in (raw.get("terms") or []) if str(x).strip()]
        a.also_measure = str(raw.get("also_measure") or "").strip()
    elif action == "clarify":
        a.clarify_question = str(raw.get("question") or "").strip()
        opts = []
        for o in (raw.get("options") or [])[:MAX_OPTIONS]:
            if not isinstance(o, dict):
                continue
            q = str(o.get("question") or "").strip()
            lab = str(o.get("label") or q)[:80]
            # An option that is not a complete question cannot be re-submitted,
            # and an option the user clicks that then fails is worse than no
            # option at all.
            if len(q.split()) >= 3:
                opts.append({"label": lab, "question": q})
        a.options = opts
    else:
        a.missing_concept = str(raw.get("missing_concept") or "").strip()
        a.nearest = [str(x) for x in (raw.get("nearest") or []) if str(x).strip()]

    ids = [str(x) for x in (raw.get("playbook_ids") or [])]
    a.playbook_ids, a.invented_playbooks = catalogue.verify_ids(ids)
    return a


def validate(a: Authored) -> Authored:
    """
    Post-conditions each branch must satisfy.

    A response that fails one of these is NOT turned into a refusal. It used to
    be, and the result reached a user as:

        Not in this dataset
        This dataset does not hold a clear enough question.

    Which is a category error wearing an answer's clothes: the model had failed
    to produce a usable clarification, and the system reported that as a gap in
    the user's data. Nothing about the data was involved.

    So these set `action = "reauthor"` and say what went wrong, and the caller
    asks the model again with that as the complaint. Only the model's own
    `refuse` — naming a concept this schema genuinely lacks — may ever tell a
    user something is missing from their data.
    """
    if a.action == "advise" and not a.playbook_ids:
        # Advice with no catalogue entry behind it would be the model's own
        # opinion, which is exactly what the catalogue exists to prevent. Fall
        # back to answering it as a measurement rather than inventing counsel.
        if a.evidence_queries:
            a.sql = a.evidence_queries[0]["sql"]
            a.action = "sql"
            a.notes.append("advice requested but no catalogue entry matched; "
                           "answered as a measurement")
        else:
            a.action = "reauthor"
            a.reauthor_reason = (
                "you chose `advise` but named no playbook_ids from the catalogue, "
                "and wrote no evidence_queries. Either name the catalogue entries "
                "your advice rests on and the queries that measure them, or answer "
                "the question as a measurement with `action: sql`.")
    if a.action == "advise" and not a.evidence_queries:
        a.action = "reauthor"
        a.reauthor_reason = (
            "you chose `advise` but wrote no evidence_queries. Advice here must "
            "rest on measured facts; write 2 to 4 small queries that measure what "
            "the advice depends on.")
    if a.action == "decompose":
        # Everything here is checked against the semantic model before any SQL
        # is built. A bad choice goes back to the model as a complaint rather
        # than becoming a refusal: nothing about the user's DATA is wrong.
        from paylens2.core.model import get_model
        m = get_model()
        mid = (a.terms or [""])[0]
        met = m.metrics.get(mid)
        why = ""
        if met is None:
            why = (f"you chose `decompose` with metric '{mid}', which is not in "
                   f"the schema. Use one of: {', '.join(sorted(m.metrics))}.")
        elif not (getattr(met, "denominator", "") or ""):
            why = (f"'{mid}' is a {getattr(met, 'type', 'count')}, not a ratio, so "
                   f"it has no mix-versus-rate split. Answer it with `action: sql`.")
        elif a.dimension not in m.dimensions:
            why = (f"you chose `decompose` by '{a.dimension}', which is not a "
                   f"dimension in the schema.")
        elif not (a.window and a.baseline):
            why = ("you chose `decompose` but did not give both `window` and "
                   "`baseline`. A decomposition compares two periods.")
        if why:
            a.action = "reauthor"
            a.reauthor_reason = why
    if a.action == "explain" and not a.terms:
        # Recoverable rather than fatal: the question itself names the thing
        # being asked about, and retrieval is a similarity search over exactly
        # that. An empty `terms` costs context, not correctness.
        a.notes.append("explain returned no terms; falling back to the question")
    if a.action == "sql" and not a.sql:
        a.action = "reauthor"
        a.reauthor_reason = ("you chose `action: sql` but the `sql` field was "
                             "empty. Return the query.")
    if a.action == "clarify" and not a.options:
        a.action = "reauthor"
        a.reauthor_reason = (
            "you chose `clarify` but gave no usable options. Every option's "
            "`question` must be a COMPLETE question of at least three words that "
            "can be run on its own. If you cannot offer such options, do not "
            "clarify — pick a sensible default, state it in `assumptions`, and "
            "answer the question.")
    if a.action == "refuse":
        concept = a.missing_concept.lower().strip()
        if concept and concept in _schema_terms():
            a.action = "reauthor"
            a.reauthor_reason = (
                f"you refused, saying this dataset does not hold "
                f"'{a.missing_concept}' — but it does; it is in the schema you "
                f"were given. Write the query.")
    if a.invented_playbooks:
        a.notes.append("dropped unknown playbook ids: "
                       + ", ".join(a.invented_playbooks))
    return a


def needs_reauthor(a: Authored) -> bool:
    return a.action == "reauthor"


def _or_salvage(parsed: dict, text: str) -> dict:
    """
    A response whose JSON could not be recovered may still contain the query.

    Throwing away correct SQL because its wrapper was untidy costs a round trip
    to fix a missing quotation mark. Nothing unsafe follows from trying: the
    salvaged query faces check 10 and the full guard like any other.
    """
    if parsed:
        return parsed
    from paylens2.core import jsonx
    sql = jsonx.salvage_sql(text)
    if sql:
        log.warning("JSON unrecoverable; salvaged the SQL from the raw response")
        return {"action": "sql", "sql": sql, "intent": "single_value",
                "assumptions": ["the model's response was malformed and the query "
                                "was recovered from it"]}
    raise ValueError("the model returned neither usable JSON nor a query")


def author(question: str, *, persona: str, today: str,
           conversation: list[dict] | None = None,
           budget: CallBudget | None = None,
           complaint: str = "", purpose: str = "author") -> Authored:
    """
    One call. Raises LLMUnavailable if no model is configured — v2 needs one.

    `complaint` re-runs the same prompt with a note about what was wrong with
    the previous response. The full schema, catalogue and period table go with
    it, which a narrow repair prompt would not carry.
    """
    payload = prompts.author_payload(
        question,
        v2model.schema_card(),
        role_persona=persona,
        fewshots=retrieval.fewshots(question),
        catalogue=catalogue.summaries(),
        today=today,
        conversation=conversation,
        periods_card=periods.card(_date.fromisoformat(today)))
    if complaint:
        payload += ("\n\n# YOUR PREVIOUS RESPONSE WAS NOT USABLE\n" + complaint
                    + "\n\nAnswer the question again, properly.")
    raw, text = llmjson.call(payload, system=prompts.AUTHOR_SYSTEM,
                             model=config.AUTHOR_MODEL, purpose=purpose,
                             budget=budget)
    return validate(_coerce(_or_salvage(raw, text)))


def heal(question: str, sql: str, reason: str, hint: str, *,
         budget: CallBudget | None = None) -> Authored:
    """
    Re-author after a rejection, with the reason attached.

    Note what is NOT done here: the check that rejected the query is never
    relaxed, and the repaired SQL re-enters check 10 and the full guard from the
    top. A heal that could widen its own acceptance criteria is how a self-healing
    pipeline deletes its own access predicate.
    """
    payload = prompts.heal_payload(question, sql, reason, hint,
                                   v2model.schema_card())
    raw, text = llmjson.call(payload, system=prompts.HEAL_SYSTEM,
                             model=config.AUTHOR_MODEL, purpose="heal",
                             budget=budget)
    return validate(_coerce(_or_salvage(raw, text)))
