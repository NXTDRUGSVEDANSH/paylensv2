"""
Data-quality checks and the two-source reconciliation.

A number with a problem nobody mentioned is worse than no number. These run
against the window an answer actually used and the findings reach the reader's
caveats, not only a tab.
"""
from __future__ import annotations

import pathlib

import pytest

from backend.core import rbac
from paylens2 import config
from paylens2.core import dq
from paylens2.core.model import get_model

ROOT = pathlib.Path(__file__).parent.parent
WIN = ("2026-11-09", "2026-11-16")


@pytest.fixture(scope="module")
def run():
    return dq._runner(rbac.principal_for("cxo", {}), get_model())


def by_id(results):
    return {r["id"]: r for r in results}


# ── reading the window off the answer's own SQL ─────────────────────────────
def test_the_window_comes_from_the_query_not_the_question():
    sql = ("SELECT count(*) FROM v_attempts WHERE txn_ts >= DATE '2026-11-09' "
           "AND txn_ts < DATE '2026-11-16'")
    assert dq.window_of(sql) == ("2026-11-09", "2026-11-16")


def test_a_query_with_no_dates_has_no_window():
    assert dq.window_of("SELECT count(*) FROM v_attempts") is None


# ── the checks, against the planted faults ──────────────────────────────────
def test_the_planted_null_issuer_rate_is_reported(run):
    r = by_id(dq.window_probe(run, WIN, ["issuer_bank"]))["null_issuer"]
    assert r["status"] == "warn"
    assert 1.5 < r["evidence"]["null_pct"] < 3.0, "the planted rate is ~2.1%"
    assert "will not sum to the total" in r["message"]


def test_de_duplication_is_verified_rather_than_assumed(run):
    # v1 counted duplicates in fact_transactions. v2's allowlist blocks the raw
    # tables and that is not relaxed for a probe, so the check confirms none
    # survived into the view — it fails loudly if de-duplication ever breaks.
    r = by_id(dq.window_probe(run, WIN, []))["duplicate_txn_ids"]
    assert r["status"] == "pass"
    assert "counted once" in r["message"]


def test_negative_amounts_are_surfaced_as_info_not_a_warning(run):
    r = by_id(dq.window_probe(run, WIN, []))["negative_amount"]
    assert r["severity"] == "info", "a known correction is not a defect"


def test_a_window_clear_of_the_part_ingested_tail_passes(run):
    assert dq.check_partial_days(run, WIN)["status"] == "pass"


def test_the_part_ingested_tail_is_reported_when_the_window_reaches_it(run):
    r = dq.check_partial_days(run, ("2026-11-14", "2026-11-21"))
    assert r["status"] == "warn"
    assert "understated" in r["message"]


def test_a_column_nobody_grouped_by_is_not_checked(run):
    # Noise is how a real warning gets ignored.
    ids = [r["id"] for r in dq.window_probe(run, WIN, [])]
    assert "null_issuer" not in ids


def test_an_unknown_column_cannot_be_interpolated(run):
    # Column names go into the SQL text, so they are filtered against the real
    # view schema before they get there.
    out = dq.window_probe(run, WIN, ["'; DROP TABLE x; --"])
    assert "null_issuer" not in [r["id"] for r in out]


# ── two sources disagreeing ─────────────────────────────────────────────────
def test_the_planted_settlement_mismatch_is_found(run):
    r = dq.reconcile(run, WIN)
    assert r["status"] == "warn"
    assert r["evidence"]["affected"], "the planted mismatch was not detected"
    assert r["evidence"]["precedence"] == "v_attempts"


def test_the_disagreement_names_a_precedence_rather_than_picking_quietly(run):
    # Never averages the two, never silently serves the larger. A reader handed
    # one number is entitled to know the other existed and disagreed.
    r = dq.reconcile(run, WIN)
    assert "system of record" in r["message"]


# ── wiring ──────────────────────────────────────────────────────────────────
def test_probes_go_through_the_guard():
    # A background query is the easiest place to leak rows a role may not see:
    # it is not user-initiated, so it never looks like the thing to check.
    src = (ROOT / "core" / "dq.py").read_text(encoding="utf-8")
    body = src[src.index("def _runner("):src.index("def _where(")]
    assert "guard(sql, p, model" in body


def test_a_failing_probe_never_fails_the_answer(monkeypatch, run):
    def boom(*_a, **_k):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(dq, "window_probe", boom)
    out = dq.run_checks("SELECT 1 FROM v_attempts WHERE txn_ts >= DATE '2026-11-09' "
                        "AND txn_ts < DATE '2026-11-16'", [],
                        p=rbac.principal_for("cxo", {}), model=get_model())
    assert isinstance(out, list), "a broken check must not raise"


def test_findings_are_ordered_warnings_first(run):
    out = dq.run_checks("SELECT issuer_bank FROM v_attempts WHERE "
                        "txn_ts >= DATE '2026-11-09' AND txn_ts < DATE '2026-11-16'",
                        ["issuer_bank"], p=rbac.principal_for("cxo", {}),
                        model=get_model())
    statuses = [r["status"] for r in out]
    assert statuses.index("warn") < statuses.index("pass")


def test_it_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(config, "USE_DQ", False)
    assert dq.run_checks("SELECT 1", [], p=rbac.principal_for("cxo", {}),
                         model=get_model()) == []


def test_warnings_reach_the_caveats_not_just_the_tab():
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert "ans.caveats.append(c[\"message\"])" in src
    assert "ans.caveats = list(s.caveats)" in src
    assert src.index("ans.caveats = list(s.caveats)") \
        < src.index("ans.caveats.append(c[\"message\"])"), \
        "findings must be appended after the composition, or they are overwritten"
