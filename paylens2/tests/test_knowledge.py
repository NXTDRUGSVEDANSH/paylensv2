"""
The knowledge corpus and the explain path.

WHY THIS EXISTS
---------------
Call 1 could return only sql, advise, clarify or refuse. "What is the formula of
success rate" had nowhere to go, so it came back as a measurement of last month
— byte-identical to the answer for "what is success rate for last month". A
definitional question answered with a number is wrong even when the number is
right.

The corpus has two kinds of content and the distinction is load-bearing. The
metric tier is GENERATED from views.yaml, so it is what check 10d enforces. The
industry tier is sourced context, and the public definitions of authorisation
rate disagree with each other about the denominator — none of them address
unresolved attempts at all. If an industry chunk were ever treated as the
definition, the knowledge base would contradict the guard.
"""
from __future__ import annotations

import pathlib

import pytest

from paylens2.core import knowledge
from paylens2.core.author import _coerce, validate
from paylens2.knowledge import build

ROOT = pathlib.Path(__file__).parent.parent


# ── the corpus ──────────────────────────────────────────────────────────────
def test_every_metric_in_the_model_gets_a_chunk():
    from paylens2.core.model import get_model
    refs = {c["meta"]["ref"] for c in build.metric_chunks()}
    assert refs == set(get_model().metrics), \
        "a metric with no chunk cannot be explained"


def test_a_metric_chunk_carries_the_expression_the_guard_enforces():
    for c in build.metric_chunks():
        mid = c["meta"]["ref"]
        assert c["meta"]["formula"] == knowledge.canonical_formula(mid), \
            f"{mid}: the chunk's formula is not the semantic model's"


def test_counts_and_sums_get_a_formula_too():
    # Ratios carry numerator/denominator; counts and sums carry `expr`. Reading
    # only the first pair left txn_count and gmv with no formula at all.
    by_ref = {c["meta"]["ref"]: c for c in build.metric_chunks()}
    for mid in ("txn_count", "gmv"):
        assert by_ref[mid]["meta"]["formula"], f"{mid} has no formula"
        assert "expression PayLens runs" in by_ref[mid]["text"]


def test_industry_chunks_are_never_authoritative():
    for c in build.industry_chunks():
        assert c["meta"]["authoritative"] is False, \
            "industry context must never outrank the semantic model"


def test_industry_entries_carry_a_source_or_say_why_not():
    import yaml
    data = yaml.safe_load(build.INDUSTRY_YAML.read_text(encoding="utf-8"))
    for e in data["entries"]:
        assert "contrast" in e, f"{e['id']}: no statement of how PayLens differs"
        assert "sources" in e, f"{e['id']}: sources key missing"


def test_the_knowledge_corpus_is_a_separate_collection():
    from backend.kb import store
    assert build.COLLECTION != store.COLLECTION, \
        "definitions in the few-shot corpus would teach the model to answer a " \
        "measurement with prose"


# ── the formula backstop ────────────────────────────────────────────────────
def test_the_real_formula_passes():
    ok, _ = knowledge.verify_formula(
        "success rate is (count(*) filter (where is_success)) / "
        "nullif(count(*) filter (where is_resolved), 0)", ["success_rate"])
    assert ok


def test_the_industry_formula_is_rejected():
    # This is the whole point. "approvals / attempts" is what the industry says
    # and what check 10d forbids, because it silently includes PENDING.
    ok, note = knowledge.verify_formula(
        "success rate is count(*) filter (where is_success) divided by count(*)",
        ["success_rate"])
    assert not ok and "does not match" in note


def test_a_swapped_numerator_is_rejected():
    ok, _ = knowledge.verify_formula(
        "success rate is (count(*) filter (where is_failed)) / "
        "nullif(count(*) filter (where is_resolved), 0)", ["success_rate"])
    assert not ok, "every ratio shares a denominator; the numerator decides"


def test_prose_without_arithmetic_is_not_checked():
    ok, _ = knowledge.verify_formula(
        "Success rate tells you how many payments go through.", ["success_rate"])
    assert ok, "not every explanation needs to be arithmetic"


# ── the action ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ({"action": "explain", "terms": ["success_rate"]}, "explain"),
    ({"terms": ["technical_decline"]}, "explain"),          # inferred
    ({"action": "sql", "sql": "SELECT 1"}, "sql"),
    ({"action": "advise", "playbook_ids": ["retry_strategy"],
      "evidence_queries": [{"id": "q1", "sql": "SELECT 1"}]}, "advise"),
])
def test_the_author_recognises_explain(raw, expected):
    assert validate(_coerce(raw)).action == expected


def test_explain_without_terms_is_not_fatal():
    # The question itself names the thing; retrieval is a similarity search over
    # exactly that. Missing terms costs context, not correctness.
    a = validate(_coerce({"action": "explain", "terms": []}))
    assert a.action == "explain"


def test_the_prompt_tells_the_model_not_to_answer_with_a_number():
    from paylens2.core import prompts
    sys = prompts.AUTHOR_SYSTEM
    assert '"action": "explain"' in sys
    # The prose count and the numbered branches have to agree. Pinning the
    # literal word went stale the moment a sixth branch was added, so this
    # counts the branches instead.
    import re
    words = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
    claimed = next((n for w, n in words.items() if f"ONE of {w} things" in sys), None)
    actual = len(re.findall(r"^\d+\. [A-Z]", sys, re.M))
    assert claimed == actual, (f"the system prompt promises {claimed} branches "
                               f"but lists {actual}")
    # Action 2 used to claim it covered "what something MEANS", which is the
    # overlap that sent definitional questions down the measurement path.
    assert "what something MEANS" not in sys


def test_the_pipeline_has_an_explain_branch():
    from paylens2.core.pipeline import V2Answer
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert "explanation" in V2Answer.__dataclass_fields__
    assert 'a.action == "explain"' in src
    assert "def _explain(" in src


def test_the_ui_renders_a_definition_rather_than_a_table():
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "function explain(" in js
    assert "explanation: d.explanation" in js, "a reload would lose it"
    assert "The expression PayLens runs" in js


# ── the typographic defect that shipped alongside it ────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("was 87.91%, with 209,701attempts.", "was 87.91%, with 209,701 attempts."),
    ("dropped to86.60% last week", "dropped to 86.60% last week"),
    # Units are written tight against their number in the fact pack itself;
    # respacing them would make the prose disagree with the evidence it cites.
    ("moved -1.33pp this week", "moved -1.33pp this week"),
    ("took 341ms to run", "took 341ms to run"),
    ("between 2026-10-01 and 2026-11-01", "between 2026-10-01 and 2026-11-01"),
])
def test_numbers_and_words_are_not_run_together(text, expected):
    from paylens2.core.synth import tidy
    assert tidy(text) == expected


# ── the corpus is read FROM, never trusted ──────────────────────────────────
# Found by testing rather than by reading: the offline path copies a chunk
# verbatim and never reaches verify_formula, so a chunk embedded before
# views.yaml last changed would have been printed as fact.
def _poisoned_chunk():
    return [{"id": "fake", "tier": "metric", "ref": "success_rate",
             "term": "Success Rate", "authoritative": True, "sources": [],
             "similarity": 0.99,
             "text": "The exact expression PayLens runs is "
                     "count(*) filter (where is_success) / count(*).",
             "formula": "count(*) filter (where is_success) / count(*)"}]


def test_a_stale_chunk_cannot_put_a_wrong_formula_on_screen():
    from paylens2.core.synth import _explain_template
    e = _explain_template(_poisoned_chunk())
    assert e.formula == knowledge.canonical_formula("success_rate"), \
        "the offline path printed the chunk instead of the semantic model"


def test_the_checker_never_consults_the_corpus():
    # The signature is the guarantee: no chunk can influence the verdict, so a
    # poisoned or stale corpus cannot talk the checker into agreeing with it.
    import inspect
    params = inspect.signature(knowledge.verify_formula).parameters
    assert list(params) == ["text", "metric_ids"]


def test_repeating_a_poisoned_chunk_is_still_rejected():
    ok, _ = knowledge.verify_formula(
        _poisoned_chunk()[0]["text"], ["success_rate"])
    assert not ok
