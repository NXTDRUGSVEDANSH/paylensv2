"""
The NLQ -> SQL cache.

The cache stores the AUTHORED query and skips call 1 only; everything after it
still runs, so the numbers are measured fresh and the guard still injects the
caller's row filter. These tests pin the two ways that can go wrong.
"""
from __future__ import annotations

import pathlib
import time
from datetime import date

import pytest

from paylens2 import config
from paylens2.core import sqlcache

ROOT = pathlib.Path(__file__).parent.parent
MON = date(2026, 11, 21)


@pytest.fixture(autouse=True)
def _clean():
    sqlcache.clear()
    yield
    sqlcache.clear()


def k(q, role="cxo", today=MON):
    return sqlcache.key(q, role=role, today=today)


# ── the key ─────────────────────────────────────────────────────────────────
def test_retyping_the_same_question_still_hits():
    assert k("success rate last week") == k("  Success Rate Last Week? ")


def test_the_same_words_a_week_later_are_a_miss():
    # THE DATE TRAP. periods.resolve() bakes literal dates into the SQL, so
    # replaying "last week" after the week rolls over would answer a different
    # question with no error anywhere — correct SQL, wrong window.
    assert k("success rate last week", today=date(2026, 11, 21)) \
        != k("success rate last week", today=date(2026, 11, 28))


def test_an_absolute_period_is_stable_across_days():
    # "october" resolves to the same window whenever it is asked, so it should
    # keep hitting. Only RELATIVE phrases are date-sensitive.
    assert k("success rate in october", today=date(2026, 11, 21)) \
        == k("success rate in october", today=date(2026, 11, 28))


def test_a_question_with_no_period_is_day_scoped():
    # No relative phrase means the authoring prompt's "today" may have driven a
    # default window, so the key is pinned to the date rather than assumed safe.
    assert k("how many merchants are there", today=date(2026, 11, 21)) \
        != k("how many merchants are there", today=date(2026, 11, 22))


def test_two_periods_in_one_question_both_land_in_the_key():
    w = sqlcache._windows("last week vs the week before last", MON)
    assert "last week=2026-11-09:2026-11-16" in w
    assert "the week before last=2026-11-02:2026-11-09" in w


def test_a_different_role_is_a_different_key():
    # The persona is in the authoring prompt, so the query itself can differ.
    assert k("success rate last week", role="cxo") \
        != k("success rate last week", role="merchant_user")


def test_a_schema_or_prompt_change_invalidates_everything(monkeypatch):
    before = k("success rate last week")
    monkeypatch.setattr("paylens2.core.retrieval._stamp", lambda: "different-stamp")
    assert k("success rate last week") != before


# ── layer 0 ─────────────────────────────────────────────────────────────────
PAYLOAD = {"sql": "SELECT 1 AS n FROM v_attempts", "intent": "single_value",
           "chart": {"chart_type": "kpi", "y": "n"}, "assumptions": [],
           "playbook_ids": []}


def test_an_entry_expires(monkeypatch):
    monkeypatch.setattr(config, "CACHE_TTL_S", 0)
    sqlcache._l0_put("k1", PAYLOAD)
    time.sleep(0.01)
    assert sqlcache._l0_get("k1") is None


def test_the_oldest_entry_is_evicted_at_capacity(monkeypatch):
    monkeypatch.setattr(config, "CACHE_SIZE", 3)
    for i in range(5):
        sqlcache._l0_put(f"k{i}", PAYLOAD)
    assert sqlcache._l0_get("k0") is None and sqlcache._l0_get("k1") is None
    assert sqlcache._l0_get("k4") is not None
    assert sqlcache.stats()["entries"] == 3


def test_a_read_keeps_an_entry_alive(monkeypatch):
    monkeypatch.setattr(config, "CACHE_SIZE", 2)
    sqlcache._l0_put("a", PAYLOAD)
    sqlcache._l0_put("b", PAYLOAD)
    sqlcache._l0_get("a")            # a becomes most-recent
    sqlcache._l0_put("c", PAYLOAD)   # evicts b, not a
    assert sqlcache._l0_get("a") is not None
    assert sqlcache._l0_get("b") is None


# ── what is stored ──────────────────────────────────────────────────────────
def test_the_chart_spec_is_cached_with_the_query():
    # Without it a cached answer silently loses its visual, which looks like a
    # rendering bug rather than a cache decision.
    class A:
        sql, intent = "SELECT 1", "single_value"
        chart = {"chart_type": "bar", "x": "a", "y": "b"}
        assumptions, playbook_ids = ["last 30 days"], []
    p = sqlcache.payload_of(A())
    assert p["chart"]["chart_type"] == "bar"
    assert p["assumptions"] == ["last 30 days"]


def test_normalised_sql_ignores_layout_but_not_content():
    assert sqlcache.norm_sql("SELECT  a\nFROM t;") == sqlcache.norm_sql("select a from t")
    assert sqlcache.norm_sql("SELECT a FROM t") != sqlcache.norm_sql("SELECT b FROM t")


# ── the invariants the pipeline must keep ───────────────────────────────────
def test_the_cache_is_disabled_cleanly(monkeypatch):
    monkeypatch.setattr(config, "USE_CACHE", False)
    assert sqlcache.lookup("anything", role="cxo", today=MON) is None
    assert sqlcache.remember("q", role="cxo", today=MON, payload=PAYLOAD) == "off"


def test_a_replay_is_revalidated_before_it_runs():
    # The stored query goes back through check 10 and all nine guard checks.
    # A cache that skipped them would be a hole straight past the guard.
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    body = src[src.index("def _replay("):src.index("def ask(")]
    assert "checks.check_v2(sql)" in body and "guard(sql, p, model)" in body
    assert "sqlcache.demote(" in body, "a failed replay must demote, not raise"


def test_only_the_authored_sql_is_stored():
    # sql_executed carries one role's row predicate welded into the text.
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert "payload_of(a)" in src
    assert "sqlcache.remember(question, role=ans.role" in src
    assert "sql_executed" not in src[src.index("def _replay("):src.index("def ask(")]


def test_writeback_runs_before_remember():
    # Both upsert the SAME Chroma record. writeback() writes a fixed metadata
    # block with verified=False; remember() merges into what it finds. In the
    # wrong order the write-back clobbers the promotion and the cache fills up
    # but never serves anything — which is what happened the first time.
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert src.index("retrieval.writeback(question, sql, ok=True)") \
        < src.index("state = sqlcache.remember("), \
        "remember() must merge into the record write-back already wrote"
