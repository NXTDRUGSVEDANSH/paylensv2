"""
Call 2 — synthesis, and the check that makes it safe.

The model receives the profile and the numbered fact pack, and writes the answer.
It computes nothing. verify() then asserts that every number in the prose was one
we measured; a single ungrounded figure discards the whole composition, because a
reader cannot tell which sentence was the invented one, and an answer you have to
audit by hand is worth less than one you can trust by construction.

On rejection there is ONE repair attempt, handed the specific offending literals.
That is the difference from the classic pipeline, which throws the whole
composition away on the first bad number. The repair is allowed to delete or
re-cite; it is not allowed to widen the check.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from backend.llm import provider
from backend.llm.provider import CallBudget
from paylens2 import config
from paylens2.core import llmjson, prompts
from paylens2.core.profile import Fact, Profile, profile

log = logging.getLogger("paylens2.synth")

_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|pp|ms)?")
_CITE = re.compile(r"\[(E\d+)\]")

# Numbers that are not claims: "three segments", "the ten-step journey". Refusing
# these would make the grounding check fire on ordinary English.
_FREE = set(range(0, 13)) | {24, 100}


@dataclass
class Grounding:
    verdict: str = "not_applicable"      # grounded | rejected | not_applicable
    numbers_checked: int = 0
    ungrounded: list[str] = field(default_factory=list)
    citations_checked: int = 0
    bad_citations: list[str] = field(default_factory=list)
    levers_checked: int = 0
    invented_levers: list[str] = field(default_factory=list)
    repaired: bool = False
    # Sentences naming the wrong segment as best or worst. Separate from
    # `ungrounded`, because the numbers in them are real — it is the claim
    # around them that is false, and the repair prompt has to say which.
    wrong_superlatives: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Advice:
    situation: str = ""
    whats_happening: list[str] = field(default_factory=list)
    what_to_do: list[dict] = field(default_factory=list)
    cannot_tell: list[str] = field(default_factory=list)
    composed_by: str = "template"
    grounding: Grounding = field(default_factory=Grounding)

    def all_text(self) -> str:
        bits = [self.situation, *self.whats_happening, *self.cannot_tell]
        bits += [f"{d.get('lever','')} {d.get('why_here','')}"
                 for d in self.what_to_do]
        return "  ".join(b for b in bits if b)


@dataclass
class Synthesis:
    headline: str = ""
    observed: list[str] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    composed_by: str = "template"        # llm | template
    grounding: Grounding = field(default_factory=Grounding)
    facts: list = field(default_factory=list)

    def all_text(self) -> str:
        bits = [self.headline, *self.observed, *self.caveats]
        bits += [h.get("text", "") for h in self.hypotheses]
        return "  ".join(b for b in bits if b)


def _numbers(text: str) -> list[tuple[float, str]]:
    out = []
    for m in _NUM.finditer(text or ""):
        try:
            out.append((float(m.group(1).replace(",", "")), m.group(0).strip()))
        except ValueError:
            continue
    return out


def _allowed(facts: list[Fact], profile: Profile, question: str,
             sql: str = "") -> set[float]:
    """
    Every number the prose may legally contain.

    The profile's own statistics count, not just the fact pack: the model is shown
    column minima, medians and top-k counts, and forbidding it to mention what it
    was shown would make the check fire on correct answers.
    """
    allowed: set[float] = set()
    for f in facts:
        for v, _ in _numbers(f"{f.value} {f.detail} {f.label}"):
            allowed.add(v)
        if f.raw is not None:
            allowed.add(float(f.raw))
            allowed.add(round(float(f.raw) * 100, 6))   # a ratio stated as a percent
    for v, _ in _numbers(profile.text):
        allowed.add(v)
    for v, _ in _numbers(question):
        allowed.add(v)
    # Literals in the executed query are INPUTS, not claims: the window dates,
    # a threshold, a top-N. Flagging them produced a false rejection on the very
    # first live question, where "2026-11-09" was read as the ungrounded numbers
    # 2026, 11 and 9. Anyone can check them — the SQL is shown beside the answer.
    for v, _ in _numbers(sql):
        allowed.add(v)
    allowed.add(float(profile.row_count))
    return allowed



# ── superlative claims ──────────────────────────────────────────────────────
# "every number was measured" and "every claim about those numbers is true"
# are different guarantees, and only the first was enforced. A composition
# read a pack whose E2 said `success rate — highest (WALLET)` and opened with
# "UPI was the highest at 88.79%". Both figures were real, both came from the
# pack, and the sentence was false. Another said "UPI (INR 1,140.63) highest,
# followed by EMI (INR 1,143.85)" — contradicting itself inside one clause.
#
# A reader checks the headline and trusts the rest, so this is the most
# expensive sentence in the answer to get wrong.
_SUPERLATIVE = {
    "highest": "hi", "greatest": "hi", "largest": "hi", "biggest": "hi",
    "best": "hi", "top": "hi", "strongest": "hi", "maximum": "hi", "most": "hi",
    "lowest": "lo", "smallest": "lo", "worst": "lo", "least": "lo",
    "weakest": "lo", "minimum": "lo", "bottom": "lo",
}
_EXTREME = re.compile(r"—\s*(highest|lowest)\s*\((.+?)\)\s*$")
# Split on terminal punctuation followed by whitespace, NOT on every full
# stop. Splitting on "." alone cuts 88.79% into "88" and "79%", which breaks a
# sentence in half and hides the claim: "UPI, at 88.79%, was the highest"
# becomes two fragments, neither of which contains both the segment and the
# superlative, and the check sees nothing wrong.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def _segment_labels(profile: Profile) -> set[str]:
    """Every segment name the result actually contained."""
    out: set[str] = set()
    for col in profile.columns:
        if col.get("kind") != "categorical":
            continue
        for value, _count in col.get("top", []):
            v = str(value).strip()
            # Two characters or fewer matches far too much ordinary prose.
            if len(v) > 2 and v != "(null)":
                out.add(v)
    return out


def verify_superlatives(text: str, facts: list[Fact],
                        profile: Profile) -> list[str]:
    """
    Sentences that name the wrong segment as the best or the worst.

    Deliberately conservative: a sentence is flagged only when it uses a
    superlative, names a segment that is NOT the extreme, and does not also
    name the one that is. "WALLET leads, ahead of UPI" is left alone; "UPI was
    the highest" when WALLET is, is not. A false rejection throws away a good
    answer, so the check would rather miss than guess.
    """
    extremes: dict[str, str] = {}          # "hi"/"lo" -> the true segment
    for f in facts:
        m = _EXTREME.search(f.label or "")
        if m:
            extremes.setdefault("hi" if m.group(1) == "highest" else "lo",
                                m.group(2).strip())
    if not extremes:
        return []

    labels = _segment_labels(profile)
    if not labels:
        return []

    bad: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        low = sentence.lower()
        directions = {d for word, d in _SUPERLATIVE.items()
                      if re.search(rf"\b{word}\b", low)}
        # A sentence claiming both ends at once ("highest ... lowest ...")
        # cannot be attributed to one segment safely, so it is left alone.
        if len(directions) != 1:
            continue
        direction = directions.pop()
        truth = extremes.get(direction)
        if not truth:
            continue
        named = {l for l in labels if re.search(rf"\b{re.escape(l.lower())}\b", low)}
        if named and truth.lower() not in {n.lower() for n in named}:
            bad.append(f"{sentence.strip()[:90]} — "
                       f"the {'highest' if direction == 'hi' else 'lowest'} "
                       f"is {truth}")
    return bad


def verify(text: str, facts: list[Fact], profile: Profile, question: str,
           sql: str = "") -> Grounding:
    allowed = _allowed(facts, profile, question, sql)
    ids = {f.id for f in facts}
    ungrounded: list[str] = []
    checked = 0

    for value, literal in _numbers(text):
        if value in _FREE and float(value).is_integer():
            continue
        checked += 1
        # Tolerance is deliberate but tight: 86.6% for 86.60% is the same claim;
        # 2.5% invented from nowhere is not.
        if any(abs(value - a) <= max(0.05, abs(a) * 0.01) for a in allowed):
            continue
        ungrounded.append(literal)

    cites = _CITE.findall(text or "")
    bad = sorted({c for c in cites if c not in ids})
    wrong_superlatives = verify_superlatives(text, facts, profile)
    ok = not ungrounded and not bad and not wrong_superlatives

    note = (f"Every number was matched against the measured facts "
            f"({checked} checked, {len(cites)} citations)." if ok else
            "The composed answer contained "
            + (f"{len(ungrounded)} number(s) not present in the measured evidence "
               f"({', '.join(ungrounded[:3])})" if ungrounded else "")
            + (" and " if ungrounded and bad else "")
            + (f"citation(s) to evidence that does not exist ({', '.join(bad[:3])})"
               if bad else "")
            + ((" and " if (ungrounded or bad) else "")
               + f"{len(wrong_superlatives)} false superlative claim(s)"
               if wrong_superlatives else "") + ".")
    g = Grounding(verdict="grounded" if ok else "rejected",
                  numbers_checked=checked, ungrounded=ungrounded[:6],
                  citations_checked=len(cites), bad_citations=bad[:6], note=note)
    g.wrong_superlatives = wrong_superlatives[:4]
    return g


# A code model writing prose runs words into numbers: "dropped to86.60% last
# week", "is87.38% [E1]". Telling it to leave a space did not work, and it is
# not worth a repair call — it is a typographic flaw, not a factual one. Fixed
# here, deterministically, after the grounding check has already passed on the
# unmodified text.
#
# It runs them together in BOTH directions — "with 209,701attempts" is the same
# defect arriving from the other side, and the first version of this only caught
# one of them. The trailing form needs three letters or more so that a unit
# written tight against its number ("341ms", "-1.33pp") is left alone: those are
# how the fact pack itself spells them, and respacing them would make the prose
# disagree with the evidence it cites.
# The leading [-+] arrived with the decomposition, which is the first path
# whose figures are routinely signed: "a total change of-1.33pp". Without it
# the lookahead sees the sign, not a digit, and the run-on survives.
# Two digits are required when there is no sign, because a single digit after
# a word is usually deliberate ("q3", "tier1"). A SIGN is signal enough on its
# own: nothing writes "of-1" on purpose. "-1.33" has one digit before the dot,
# which is why \d{2} alone never caught it.
_RUNON = re.compile(r"(?<=[a-z]{2})(?=[-+]\d|\d{2})")
_RUNON_TRAILING = re.compile(r"(?<=\d)(?=[a-z]{3,})")


def tidy(text: str) -> str:
    return _RUNON_TRAILING.sub(" ", _RUNON.sub(" ", text or ""))


def _norm_lever(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())


def verify_levers(proposed: list[dict], allowed: list[dict]) -> tuple[list[dict], list[str]]:
    """
    (kept, invented). A recommendation not in the catalogue is dropped.

    This is the check the number-grounding one cannot do. "You should enable
    network tokenisation" contains no figure, so verify() waves it through —
    and it is advice this system has no basis for, aimed at a team nobody
    named. Matching is on a normalised prefix rather than equality, because the
    model is allowed to shorten a lever for readability; it is not allowed to
    invent one.
    """
    kept: list[dict] = []
    invented: list[str] = []
    index = {_norm_lever(a["lever"]): a for a in allowed}
    for d in proposed:
        text = _norm_lever(d.get("lever", ""))
        if not text:
            continue
        hit = index.get(text)
        if hit is None:
            hit = next((a for k, a in index.items()
                        if k.startswith(text[:40]) or text.startswith(k[:40])), None)
        if hit is None:
            invented.append(str(d.get("lever", ""))[:90])
            continue
        kept.append({"lever": hit["lever"], "owner": hit["owner"],
                     "why_here": str(d.get("why_here", "")).strip() or hit["why"],
                     "check_with": hit["measure"] or str(d.get("check_with", ""))})
    return kept, invented


def _advice_template(advice_levers: list[dict], facts: list[Fact],
                     profile: Profile, framing: str) -> Advice:
    """
    The advisory answer when no model writes it — a real briefing, not a stub.

    Served when the key is absent, the call fails, or the composition is
    rejected twice. Every number in it came from the pack by construction, and
    every lever from the catalogue.
    """
    a = Advice(composed_by="template")
    a.situation = framing.split(". ")[0].strip() + "." if framing else \
        "Here is what the data shows and what can be done about it."
    a.whats_happening = [f"{f.label} is {f.value}." + (f" ({f.detail})" if f.detail else "")
                         for f in facts[1:5]]
    a.what_to_do = [{"lever": l["lever"], "owner": l["owner"],
                     "why_here": l["why"], "check_with": l["measure"]}
                    for l in advice_levers[:5]]
    a.cannot_tell = ["This is measured from your own estate only; no industry "
                     "benchmark is held here."]
    a.grounding = Grounding(verdict="not_applicable",
                            note="Composed from the catalogue and the measured "
                                 "facts directly; no model wrote this text.")
    return a


def _tidy_advice(a: Advice) -> Advice:
    a.situation = tidy(a.situation)
    a.whats_happening = [tidy(x) for x in a.whats_happening]
    a.cannot_tell = [tidy(x) for x in a.cannot_tell]
    a.what_to_do = [{**d, "why_here": tidy(d.get("why_here", ""))}
                    for d in a.what_to_do]
    return a


def advise(question: str, queries: list[dict], profile: Profile, *, persona: str,
           levers: list[dict], framing: str, limits: list[str],
           assumptions: list[str], budget: CallBudget | None = None) -> Advice:
    """Call 2 on the advisory path: business prose, then both checks."""
    facts = profile.facts
    if not provider.available() or not levers:
        return _advice_template(levers, facts, profile, framing)

    payload = prompts.advise_payload(
        question, queries, profile.text,
        [{"id": f.id, "label": f.label, "value": f.value, "detail": f.detail}
         for f in facts],
        role_persona=persona, levers=levers, framing=framing, limits=limits,
        assumptions=assumptions)

    def compose(prompt: str, purpose: str) -> Advice | None:
        try:
            raw, _ = llmjson.call(prompt, system=prompts.ADVISE_SYSTEM,
                                  model=config.SYNTH_MODEL, purpose=purpose,
                                  budget=budget)
        except Exception as exc:  # noqa: BLE001
            log.warning("advisory composition failed: %s", exc)
            return None
        a = Advice(composed_by="llm")
        a.situation = str(raw.get("situation") or "").strip()
        a.whats_happening = [str(x).strip() for x in (raw.get("whats_happening") or [])
                             if str(x).strip()]
        a.cannot_tell = [str(x).strip() for x in (raw.get("cannot_tell") or [])
                         if str(x).strip()]
        a.what_to_do = [d for d in (raw.get("what_to_do") or []) if isinstance(d, dict)]
        return a if a.situation else None

    out = compose(payload, "synth")
    if out is None:
        return _advice_template(levers, facts, profile, framing)

    kept, invented = verify_levers(out.what_to_do, levers)
    g = verify(out.all_text(), facts, profile, question)
    g.levers_checked = len(out.what_to_do)
    g.invented_levers = invented[:4]

    if g.verdict == "grounded" and not invented:
        out.what_to_do = kept
        out.grounding = g
        return _tidy_advice(out)

    # One repair, pointed at exactly what failed.
    problems = []
    if g.ungrounded:
        problems.append(f"numbers not in FACTS: {', '.join(g.ungrounded[:4])}")
    if g.bad_citations:
        problems.append(f"citations to nothing: {', '.join(g.bad_citations[:4])}")
    if invented:
        problems.append(f"recommendations not in LEVERS: {'; '.join(invented[:3])}")
    log.warning("advisory rejected: %s", " | ".join(problems))

    out2 = compose(
        payload + "\n\n# YOUR PREVIOUS ANSWER WAS REJECTED\n" + "\n".join(problems)
        + "\n\nRewrite it. Remove every number that is not in FACTS and every "
          "recommendation that is not in LEVERS, copying lever text exactly. Do "
          "not add anything new. Keep the meaning.", "repair")
    if out2 is not None:
        kept2, invented2 = verify_levers(out2.what_to_do, levers)
        g2 = verify(out2.all_text(), facts, profile, question)
        g2.levers_checked = len(out2.what_to_do)
        g2.invented_levers = invented2[:4]
        if g2.verdict == "grounded" and not invented2:
            g2.repaired = True
            g2.note += " (repaired on the second attempt)"
            out2.what_to_do = kept2
            out2.grounding = g2
            return _tidy_advice(out2)

    fallback = _advice_template(levers, facts, profile, framing)
    fallback.grounding = g
    fallback.grounding.note += (" The composed answer was discarded and the "
                                "catalogue briefing is shown instead.")
    return fallback


def _template(question: str, facts: list[Fact], profile: Profile) -> Synthesis:
    """
    The answer when no model writes it — a real answer, not a stub.

    Served when the key is absent, the call fails, or the repair still does not
    ground. Every number in it came from the pack by construction.
    """
    s = Synthesis(composed_by="template")
    if profile.row_count == 0:
        s.headline = ("The query ran and matched no rows. That means there were "
                      "none in this window, not that the question was wrong.")
        s.caveats = ["An empty result is a finding; check the period and filters "
                     "if you expected rows."]
        s.grounding = Grounding(verdict="not_applicable",
                                note="No model wrote this text.")
        return s
    head = facts[1] if len(facts) > 1 else facts[0]
    s.headline = f"{head.label}: {head.value}."
    s.observed = [f"{f.label} is {f.value}."
                  + (f" ({f.detail})" if f.detail else "")
                  for f in facts[1:5]]
    if profile.truncated:
        s.caveats.append("The result hit the row cap and is truncated.")
    s.grounding = Grounding(verdict="not_applicable",
                            note="Composed from the measured facts directly; no "
                                 "model wrote this text, so there is nothing to "
                                 "ground-check.")
    return s


def _tidy(s: Synthesis) -> Synthesis:
    s.headline = tidy(s.headline)
    s.observed = [tidy(x) for x in s.observed]
    s.caveats = [tidy(x) for x in s.caveats]
    s.hypotheses = [{**h, "text": tidy(h.get("text", ""))} for h in s.hypotheses]
    return s


def _coerce(raw: dict) -> Synthesis:
    s = Synthesis(composed_by="llm")
    s.headline = str(raw.get("headline") or "").strip()
    s.observed = [str(x).strip() for x in (raw.get("observed") or []) if str(x).strip()]
    for h in (raw.get("hypotheses") or [])[:2]:
        if not isinstance(h, dict) or not str(h.get("text", "")).strip():
            continue
        conf = str(h.get("confidence", "low")).lower()
        s.hypotheses.append({
            "text": str(h["text"]).strip(),
            "confidence": conf if conf in ("low", "medium", "high") else "low",
            "testable_with": str(h.get("testable_with", "")).strip()})
    s.caveats = [str(x).strip() for x in (raw.get("caveats") or []) if str(x).strip()]
    return s


def synthesise(question: str, sql: str, profile: Profile, *, persona: str,
               assumptions: list[str], advisory: str = "",
               budget: CallBudget | None = None) -> Synthesis:
    facts = profile.facts
    if not provider.available():
        return _template(question, facts, profile)

    payload = prompts.synth_payload(
        question, sql, profile.text,
        [{"id": f.id, "label": f.label, "value": f.value, "detail": f.detail}
         for f in facts],
        role_persona=persona, assumptions=assumptions, advisory=advisory)

    try:
        raw, _ = llmjson.call(payload, system=prompts.SYNTH_SYSTEM,
                              model=config.SYNTH_MODEL, purpose="synth",
                              budget=budget)
    except Exception as exc:  # noqa: BLE001
        log.warning("synthesis call failed, using the template: %s", exc)
        return _template(question, facts, profile)

    s = _coerce(raw)
    if not s.headline:
        return _template(question, facts, profile)

    g = verify(s.all_text(), facts, profile, question, sql)
    if g.verdict == "grounded":
        s.grounding = g
        return _tidy(s)

    # One repair, pointed at the exact literals.
    log.warning("synthesis rejected: %s", g.note)
    try:
        repair_prompt = (
            payload
            + "\n\n# YOUR PREVIOUS ANSWER WAS REJECTED\n"
            + f"{g.note}\n\nThe offending values: {', '.join(g.ungrounded) or 'none'}"
            + (f"\nThe invalid citations: {', '.join(g.bad_citations)}"
               if g.bad_citations else "")
            # Naming the false claim and the true extreme, because "a number
            # is wrong" sends the model looking at the figures, and here the
            # figures are right and the sentence around them is not.
            + (("\nFALSE SUPERLATIVES — these sentences name the wrong segment:\n  "
                + "\n  ".join(g.wrong_superlatives))
               if g.wrong_superlatives else "")
            + "\n\nRewrite it. Remove or replace every number that is not in FACTS "
              "or PROFILE. Do not add new numbers. If a sentence says a segment is "
              "the highest or the lowest, it must be the one FACTS names as such. "
              "Keep the meaning.")
        raw2, _ = llmjson.call(repair_prompt, system=prompts.SYNTH_SYSTEM,
                               model=config.SYNTH_MODEL, purpose="repair",
                               budget=budget)
        s2 = _coerce(raw2)
        g2 = verify(s2.all_text(), facts, profile, question, sql)
        if s2.headline and g2.verdict == "grounded":
            g2.repaired = True
            g2.note += " (repaired on the second attempt)"
            s2.grounding = g2
            return _tidy(s2)
        log.warning("repair still ungrounded: %s", g2.note)
    except Exception as exc:  # noqa: BLE001
        log.warning("repair call failed: %s", exc)

    out = _template(question, facts, profile)
    out.grounding = g
    out.grounding.note += " It was discarded and the measured answer is shown instead."
    return out


# ── the explain path ────────────────────────────────────────────────────────
@dataclass
class Explanation:
    definition: str = ""
    formula: str = ""
    formula_plain: str = ""
    notes: list[str] = field(default_factory=list)
    industry: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    composed_by: str = "template"
    grounding: Grounding = field(default_factory=Grounding)


def _explain_template(chunks: list[dict]) -> Explanation:
    """
    The offline answer: the authoritative chunks, unedited.

    Worth having even when a model IS available, because it is what a failed or
    unverifiable composition falls back to — and unlike a measurement, a
    definition read straight out of the semantic model needs no prose at all to
    be correct.
    """
    e = Explanation(composed_by="template")
    auth = [c for c in chunks if c.get("authoritative")]
    if not auth:
        e.definition = ("That term is not defined in this system's semantic "
                        "model or glossary.")
        e.grounding = Grounding(verdict="not_applicable",
                                note="Nothing retrieved; no model wrote this.")
        return e
    from paylens2.core import knowledge

    first = auth[0]
    e.definition = first["text"]
    # The nearest chunk is often the glossary's plain-words entry, which has no
    # formula. Take the formula from whichever authoritative chunk carries one,
    # so asking for the formula does not return prose that omits it.
    #
    # And re-read it from the semantic model rather than trusting the chunk.
    # This path skips verify_formula — there is no composition to check — so a
    # chunk embedded before views.yaml last changed would otherwise be printed
    # as fact. Reading the live model makes a stale corpus impossible to show
    # rather than merely unlikely.
    ref = next((c["ref"] for c in auth if c.get("formula")), "")
    e.formula = (knowledge.canonical_formula(ref)
                 or next((c["formula"] for c in auth if c.get("formula")), ""))
    e.terms = [c["ref"] for c in auth]
    e.notes = [c["text"] for c in auth[1:3]]
    e.industry = [c["text"] for c in chunks if not c.get("authoritative")][:2]
    e.sources = sorted({s for c in chunks for s in c.get("sources", [])})
    e.grounding = Grounding(
        verdict="not_applicable",
        note="Read directly from the semantic model and glossary; no model "
             "wrote this text, so there is nothing to ground-check.")
    return e


def explain(question: str, chunks: list[dict], *, persona: str,
            budget: CallBudget | None = None) -> Explanation:
    """
    Compose a definition from retrieved material.

    The grounding analogue for this path is `verify_formula`. A measured answer
    is checked by asserting every number came from the fact pack; there are no
    numbers here, so what gets checked is the arithmetic itself. A composition
    that states a formula which is not the one check 10d enforces is discarded
    and the authoritative chunk shown instead — the prose and the SQL have to
    agree about what the metric IS, or the system contradicts itself depending
    on which question you happen to ask.
    """
    if not chunks:
        return _explain_template(chunks)
    if not provider.available():
        return _explain_template(chunks)

    payload = prompts.explain_payload(question, chunks, role_persona=persona)
    try:
        raw, _ = llmjson.call(payload, system=prompts.EXPLAIN_SYSTEM,
                              model=config.SYNTH_MODEL, purpose="synth",
                              budget=budget)
    except Exception as exc:  # noqa: BLE001
        log.warning("explain call failed, reading the model directly: %s", exc)
        return _explain_template(chunks)

    e = Explanation(composed_by="llm")
    e.definition = tidy(str(raw.get("definition") or "").strip())
    e.formula = str(raw.get("formula") or "").strip()
    e.formula_plain = tidy(str(raw.get("formula_plain") or "").strip())
    e.notes = [tidy(str(x).strip()) for x in (raw.get("notes") or []) if str(x).strip()]
    e.industry = [tidy(str(x).strip()) for x in (raw.get("industry") or [])
                  if str(x).strip()]
    e.sources = sorted({s for c in chunks for s in c.get("sources", [])})
    e.terms = [c["ref"] for c in chunks if c.get("authoritative")]

    if not e.definition:
        log.warning("explain returned no definition; reading the model directly")
        return _explain_template(chunks)

    from paylens2.core import knowledge
    mids = knowledge.mentioned_metrics(f"{question} {e.definition} {e.formula}")
    body = " ".join([e.definition, e.formula, e.formula_plain, *e.notes])
    ok, note = knowledge.verify_formula(body, mids)
    if not ok:
        log.warning("explain rejected: %s", note)
        from backend.core import events
        events.recovery("explain", "the explanation stated a formula that is not "
                                   "the one this system runs — showing the "
                                   "semantic model's own wording instead",
                        detail=note)
        fallback = _explain_template(chunks)
        fallback.grounding = Grounding(
            verdict="rejected", note=note,
            ungrounded=[e.formula or e.formula_plain])
        return fallback

    e.grounding = Grounding(verdict="grounded", note=note, numbers_checked=len(mids))
    return e


# ── the decomposition path ──────────────────────────────────────────────────
def decomposition_facts(dec: dict) -> list[Fact]:
    """
    The decomposition as a numbered pack, so the prose can be ground-checked
    exactly like a measured answer. Python computed every one of these.
    """
    pp = lambda v: f"{v:+.2f}pp"
    out = [
        Fact(id="E1", label=f"{dec['dimension']} overall, baseline",
             value=f"{dec['baseline_value']:.2f}%"),
        Fact(id="E2", label=f"{dec['dimension']} overall, latest",
             value=f"{dec['target_value']:.2f}%"),
        Fact(id="E3", label="Total change", value=pp(dec["delta_pp"]),
             detail="percentage points, not percent"),
        Fact(id="E4", label="Mix effect — traffic moved between segments",
             value=pp(dec["mix_pp"])),
        Fact(id="E5", label="Rate effect — segments changed their own rate",
             value=pp(dec["rate_pp"])),
        Fact(id="E6", label="Share of the move explained by mix",
             value=f"{abs(dec['mix_share_pct']):.1f}%"),
    ]
    for sg in dec["segments"][:4]:
        out.append(Fact(
            id=f"E{len(out) + 1}", label=f"{sg['key']} share",
            value=f"{sg['weight_base'] * 100:.1f}% to {sg['weight_target'] * 100:.1f}%",
            detail=f"its own rate {sg['value_base'] * 100:.2f}% to "
                   f"{sg['value_target'] * 100:.2f}%"))
    return out


def _decomposition_template(dec: dict, facts: list[Fact]) -> Synthesis:
    """The answer with no model involved. The arithmetic already said it."""
    s = Synthesis(composed_by="template", facts=facts)
    share = abs(dec["mix_share_pct"])
    which = {"mix": "traffic mix rather than degraded processing",
             "rate": "the segments themselves getting worse",
             "mixed": "a mix of shifting traffic and changing segment rates"}
    s.headline = (f"{dec['dimension'].replace('_', ' ')} moved "
                  f"{dec['delta_pp']:+.2f}pp "
                  f"({dec['baseline_value']:.2f}% to {dec['target_value']:.2f}%), "
                  f"and {share:.0f}% of that is "
                  f"{which.get(dec['dominant'], 'both effects together')}.")
    s.observed = [f"Mix effect {dec['mix_pp']:+.2f}pp, "
                  f"rate effect {dec['rate_pp']:+.2f}pp [E4][E5]."]
    for sg in dec["segments"][:3]:
        s.observed.append(
            f"{sg['key']} went from {sg['weight_base'] * 100:.1f}% to "
            f"{sg['weight_target'] * 100:.1f}% of traffic, its own rate "
            f"{sg['value_base'] * 100:.2f}% to {sg['value_target'] * 100:.2f}%.")
    s.grounding = Grounding(verdict="not_applicable",
                            note="Composed from the decomposition directly; no "
                                 "model wrote this text.")
    return s


def narrate_decomposition(question: str, dec, *, persona: str,
                          budget: CallBudget | None = None) -> Synthesis:
    d = dec.as_dict() if hasattr(dec, "as_dict") else dec
    facts = decomposition_facts(d)
    if not provider.available():
        return _decomposition_template(d, facts)

    payload = prompts.decompose_payload(
        question, d, [{"id": f.id, "label": f.label, "value": f.value}
                      for f in facts], role_persona=persona)
    try:
        raw, _ = llmjson.call(payload, system=prompts.DECOMPOSE_SYSTEM,
                              model=config.SYNTH_MODEL, purpose="synth",
                              budget=budget)
    except Exception as exc:  # noqa: BLE001
        log.warning("decomposition narration failed, using the template: %s", exc)
        return _decomposition_template(d, facts)

    s = _coerce(raw)
    s.facts = facts
    if not s.headline:
        return _decomposition_template(d, facts)

    # Same grounding rule as a measured answer: every figure in the prose has
    # to be one Python computed. An invented pp figure here would be a claim
    # about causation, which is exactly the thing worth policing.
    # verify() reads allowed numbers from the fact pack AND the profile text.
    # There is no result profile on this path, so the decomposition's own
    # payload stands in: every figure in it was computed in Python, which is
    # exactly the property the check is testing for.
    ctx = profile([])
    ctx.text = prompts.decompose_payload(question, d, [], role_persona="")
    g = verify(s.all_text(), facts, ctx, question, "")
    s.grounding = g
    if g.verdict == "rejected":
        log.warning("decomposition narration rejected: %s", g.note)
        from backend.core import events
        events.recovery("synthesise", "the decomposition write-up stated a figure "
                                      "that was not computed — showing the "
                                      "arithmetic's own wording instead",
                        detail=g.note)
        out = _decomposition_template(d, facts)
        out.grounding = g
        return out
    return _tidy(s)
