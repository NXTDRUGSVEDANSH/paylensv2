"""
Periods are computed in Python, and the prompt carries conversation context.

The period table exists because telling the model the convention did not work:
asked for "last week" on a Saturday it returned the trailing seven days despite
an explicit instruction not to. Finding the previous Monday is arithmetic, and
arithmetic is the one thing this project never leaves to a model.
"""
from __future__ import annotations

from datetime import date

from paylens2.core import catalogue, periods, prompts

SAT = date(2026, 11, 21)          # a Saturday


def _p(phrase, today=SAT):
    return next((lo, hi) for p, lo, hi in periods.resolve(today) if p == phrase)


def test_last_week_is_the_previous_complete_iso_week():
    lo, hi = _p("last week")
    assert (lo, hi) == (date(2026, 11, 9), date(2026, 11, 16))
    assert lo.weekday() == 0                      # Monday
    assert (hi - lo).days == 7                    # half-open, a full week


def test_last_week_is_not_the_trailing_seven_days():
    assert _p("last week") != _p("last 7 days")


def test_this_week_starts_on_monday_and_ends_today():
    lo, hi = _p("this week")
    assert lo == date(2026, 11, 16) and hi == date(2026, 11, 22)


def test_last_month_is_a_whole_calendar_month():
    assert _p("last month") == (date(2026, 10, 1), date(2026, 11, 1))


def test_the_week_before_last_is_the_baseline_for_last_week():
    assert _p("the week before last")[1] == _p("last week")[0]


def test_month_names_resolve_but_not_future_ones():
    names = [p for p, _, _ in periods.resolve(SAT)]
    assert "october" in names
    assert "december" not in names                # has not happened yet


def test_january_rolls_into_the_next_year():
    lo, hi = _p("december", date(2026, 12, 15))
    assert (lo, hi) == (date(2026, 12, 1), date(2027, 1, 1))


def test_the_card_states_literal_dates():
    c = periods.card(SAT)
    assert "2026-11-09" in c and "2026-11-16" in c
    assert "Do not compute a date yourself" in c


def test_the_prompt_carries_prior_turns():
    p = prompts.author_payload(
        "and CARD instead", "SCHEMA", role_persona="p", fewshots=[],
        catalogue=catalogue.summaries(), today=SAT.isoformat(),
        conversation=[{"question": "success rate by method last week",
                       "sql": "SELECT 1 AS n FROM v_attempts"}],
        periods_card=periods.card(SAT))
    assert "EARLIER IN THIS CONVERSATION" in p
    assert "success rate by method last week" in p
    # The rule that keeps a follow-up from overriding what the user typed.
    assert "only to fill a gap" in p


def test_a_first_question_carries_no_conversation_block():
    p = prompts.author_payload(
        "success rate last week", "SCHEMA", role_persona="p", fewshots=[],
        catalogue=catalogue.summaries(), today=SAT.isoformat(),
        periods_card=periods.card(SAT))
    assert "EARLIER IN THIS CONVERSATION" not in p
