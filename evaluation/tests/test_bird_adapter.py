"""
The BIRD adapter, proven against a synthetic database.

These run without the Mini-Dev download, so the adapter is known to work
before the real data arrives — and if it ever stops working, that is visible
here rather than as a mysteriously low EX.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
import sqlglot

from evaluation import config
from evaluation.datasets import bird_adapter


@pytest.fixture
def fake_bird(tmp_path, monkeypatch):
    """A two-table database with a BIRD-style awkward column name."""
    root = tmp_path / "bird_dbs"
    (root / "schools").mkdir(parents=True)
    path = root / "schools" / "schools.sqlite"
    con = sqlite3.connect(str(path))
    con.execute('CREATE TABLE schools (CDSCode TEXT, School TEXT, County TEXT)')
    con.executemany("INSERT INTO schools VALUES (?,?,?)",
                    [("1", "Alpha", "Alameda"), ("2", "Beta", "Fresno")])
    con.execute('CREATE TABLE frpm (CDSCode TEXT, "Free Meal Count (K-12)" INT)')
    con.executemany("INSERT INTO frpm VALUES (?,?)", [("1", 150), ("2", 40)])
    con.commit()
    con.close()
    monkeypatch.setattr(config, "BIRD_DB_ROOT", root)
    bird_adapter._CACHE.clear()
    return root


def test_available_dbs_finds_the_database(fake_bird):
    from evaluation.datasets import bird_loader
    assert bird_loader.available_dbs() == ["schools"]


def test_materialise_copies_every_table(fake_bird):
    con = bird_adapter.materialise("schools")
    names = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    assert names == {"schools", "frpm"}
    assert con.execute("SELECT count(*) FROM schools").fetchone()[0] == 2


def test_bound_swaps_the_catalog_and_restores_it(fake_bird):
    from backend.core import db
    real = db.table_columns()
    assert "v_attempts" in real
    with bird_adapter.bound("schools"):
        inside = db.table_columns()
        assert "schools" in inside and "v_attempts" not in inside
    assert "v_attempts" in db.table_columns(), "the warehouse must come back"


def test_an_unbound_thread_still_sees_the_warehouse(fake_bird):
    """
    The property that makes concurrency safe.

    The rebind is a module attribute swap and therefore process-wide, so it
    has to resolve per thread or N workers on N databases would validate
    against each other's schema — and that failure mode is passing, not
    erroring.
    """
    from backend.core import db
    seen: dict[str, bool] = {}

    def other():
        seen["warehouse"] = "v_attempts" in db.table_columns()

    with bird_adapter.bound("schools"):
        assert "schools" in db.table_columns()
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert seen["warehouse"] is True


def test_model_for_declares_no_metrics_so_check_10d_is_inert(fake_bird):
    model = bird_adapter.model_for("schools")
    assert sorted(model.allowed_tables) == ["frpm", "schools"]
    assert model.roles["cxo"].row_scope is None
    # The denied-function list is inherited from PayLens, not re-typed.
    assert "read_csv" in model.denied_functions


def test_the_full_guard_passes_a_real_bird_query(fake_bird):
    from backend.core import rbac
    from backend.core.guard import guard
    from paylens2.core import checks

    sql = ('SELECT T1.School FROM schools AS T1 JOIN frpm AS T2 '
           'ON T1.CDSCode = T2.CDSCode WHERE T2."Free Meal Count (K-12)" > 100')
    model = bird_adapter.model_for("schools")
    p = rbac.principal_for("cxo", {})
    with bird_adapter.bound("schools"):
        passed = checks.check_v2(sql, dialect=bird_adapter.BIRD_DIALECT,
                                 skip=bird_adapter.BIRD_SKIP)
        assert len(passed) == 5
        g = guard(sql, p, model, dialect=bird_adapter.BIRD_DIALECT)
    assert g.ok
    # All nine still run, and RBAC passes with nothing to inject rather than
    # being skipped — a BIRD database has no tenant to scope to.
    assert len(g.checks_passed) == 9
    assert "c9_rbac_present" in g.checks_passed
    assert g.injected_predicates == []


def test_the_guard_still_blocks_a_hallucinated_table(fake_bird):
    from backend.core import rbac
    from backend.core.guard import GuardBlocked, guard
    model = bird_adapter.model_for("schools")
    p = rbac.principal_for("cxo", {})
    with bird_adapter.bound("schools"):
        with pytest.raises(GuardBlocked) as e:
            guard("SELECT 1 FROM made_up_table", p, model,
                  dialect=bird_adapter.BIRD_DIALECT)
    assert e.value.check == "c4_table_allowlist"


def test_the_guard_still_blocks_a_write(fake_bird):
    from backend.core import rbac
    from backend.core.guard import GuardBlocked, guard
    model = bird_adapter.model_for("schools")
    p = rbac.principal_for("cxo", {})
    with bird_adapter.bound("schools"):
        with pytest.raises(GuardBlocked):
            guard("DROP TABLE schools", p, model,
                  dialect=bird_adapter.BIRD_DIALECT)


def test_schema_card_quotes_the_awkward_column(fake_bird):
    card = bird_adapter.schema_card_for("schools")
    assert '"Free Meal Count (K-12)"' in card
    assert "v_attempts" not in card
