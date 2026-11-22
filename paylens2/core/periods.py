"""
Common periods, resolved in Python and handed to the model as literal dates.

WHY NOT JUST TELL IT THE CONVENTION
-----------------------------------
The first attempt put the rule in the prompt: "'last week' means the previous
complete ISO week, Monday to Sunday — NOT the trailing 7 days". Qwen 2.5 Coder
read it and still emitted `txn_ts >= DATE '2026-11-14'`, the trailing seven days,
because finding the previous Monday from a Saturday is arithmetic and models are
bad at arithmetic. That is the project's founding principle arriving in the time
dimension: Python computes, the model picks.

So the dates are computed here and pasted into the prompt. The model copies a
literal instead of deriving one, and v2 lands on the same window the classic
resolver would have chosen for the same phrase — which matters, because two
pipelines disagreeing about "last week" is the same defect as two pipelines
disagreeing about "success rate".

All ranges are HALF-OPEN: start <= txn_ts < end.
"""
from __future__ import annotations

from datetime import date, timedelta


def resolve(today: date) -> list[tuple[str, date, date]]:
    """[(phrase, start, end_exclusive)] — the periods people actually ask for."""
    monday = today - timedelta(days=today.weekday())        # ISO week start
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month
    last_month_start = (first_of_month - timedelta(days=1)).replace(day=1)

    out: list[tuple[str, date, date]] = [
        ("today", today, today + timedelta(days=1)),
        ("yesterday", today - timedelta(days=1), today),
        ("this week", monday, today + timedelta(days=1)),
        ("last week", monday - timedelta(days=7), monday),
        ("this month", first_of_month, today + timedelta(days=1)),
        ("last month", last_month_start, last_month_end),
        ("last 7 days", today - timedelta(days=7), today + timedelta(days=1)),
        ("last 30 days", today - timedelta(days=30), today + timedelta(days=1)),
        ("last 90 days", today - timedelta(days=90), today + timedelta(days=1)),
    ]
    # The week before last, because a "compared with the week before" question
    # needs a baseline and deriving one is the same arithmetic trap.
    out.append(("the week before last", monday - timedelta(days=14),
                monday - timedelta(days=7)))
    # Named months of the current year, so "in October" is a literal too.
    for m in range(1, 13):
        start = date(today.year, m, 1)
        if start > today:
            continue
        end = date(today.year + (m == 12), (m % 12) + 1, 1)
        out.append((start.strftime("%B").lower(), start, end))
    return out


def card(today: date) -> str:
    rows = resolve(today)
    width = max(len(p) for p, _, _ in rows)
    lines = [f"## PERIODS — already resolved for you. Copy these dates.",
             "",
             f"Today is {today.isoformat()} ({today.strftime('%A')}). Ranges are "
             f"half-open: txn_ts >= start AND txn_ts < end.",
             ""]
    for phrase, lo, hi in rows:
        lines.append(f"  {phrase:<{width}}  >= DATE '{lo}'  AND < DATE '{hi}'")
    lines += ["",
              "Do not compute a date yourself. If the question names a period not "
              "listed here, derive it from today's date above and say so in "
              "`assumptions`."]
    return "\n".join(lines)
