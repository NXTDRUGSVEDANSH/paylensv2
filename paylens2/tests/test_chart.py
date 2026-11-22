"""
Chart selection: the model proposes before any row exists, the result decides.
"""
from __future__ import annotations

from paylens2.core.chart import rule_chart, validate

BREAKDOWN = [{"payment_method": "UPI", "success_rate": 0.8879, "attempts": 26938},
             {"payment_method": "CARD", "success_rate": 0.9006, "attempts": 10775}]
SINGLE = [{"success_rate": 0.8660, "attempts": 48976}]
TREND = [{"bucket": "2026-11-01", "success_rate": 0.87},
         {"bucket": "2026-11-02", "success_rate": 0.88}]
WIDE = [{"issuer_bank": f"Bank {i}", "success_rate": 0.8 + i / 100} for i in range(16)]


def test_a_valid_proposal_is_honoured_untouched():
    spec = validate({"chart_type": "bar", "x": "payment_method",
                     "y": "success_rate", "title": "SR by rail"},
                    BREAKDOWN, ["success_rate"])
    assert spec["chart_type"] == "bar" and spec["overrides"] == []
    assert spec["title"] == "SR by rail"


def test_a_column_that_is_not_in_the_result_is_overridden():
    spec = validate({"chart_type": "line", "x": "not_a_column", "y": "success_rate"},
                    BREAKDOWN, ["success_rate"])
    assert spec["chart_type"] == "bar"
    assert any("not in the result" in o for o in spec["overrides"])


def test_a_non_numeric_y_is_rejected():
    spec = validate({"chart_type": "bar", "x": "success_rate", "y": "payment_method"},
                    BREAKDOWN, ["success_rate"])
    assert any("not numeric" in o for o in spec["overrides"])


def test_an_identifier_is_never_an_axis():
    rows = [{"merchant_id": 1042, "attempts": 5}, {"merchant_id": 2201, "attempts": 9}]
    spec = validate({"chart_type": "bar", "x": "attempts", "y": "merchant_id"},
                    rows, [])
    assert any("identifier" in o for o in spec["overrides"])


def test_a_kpi_needs_exactly_one_row():
    spec = validate({"chart_type": "kpi", "y": "success_rate"},
                    BREAKDOWN, ["success_rate"])
    assert spec["chart_type"] != "kpi"
    assert any("one row" in o for o in spec["overrides"])


def test_one_row_becomes_a_kpi_not_a_bar():
    assert rule_chart(SINGLE, ["success_rate"])["chart_type"] == "kpi"
    spec = validate({"chart_type": "bar", "x": "success_rate", "y": "attempts"},
                    SINGLE, ["attempts"])
    assert spec["chart_type"] == "kpi"


def test_many_categories_go_horizontal():
    spec = validate({"chart_type": "bar", "x": "issuer_bank", "y": "success_rate"},
                    WIDE, ["success_rate"])
    assert spec["chart_type"] == "horizontal_bar"
    assert any("categories" in o for o in spec["overrides"])


def test_a_time_column_implies_a_line():
    assert rule_chart(TREND, ["success_rate"])["chart_type"] == "line"


def test_no_proposal_derives_one_and_says_so():
    spec = validate(None, BREAKDOWN, ["success_rate"])
    assert spec["chart_type"] == "bar"
    assert any("no chart proposed" in o for o in spec["overrides"])


def test_an_unknown_chart_type_falls_back():
    spec = validate({"chart_type": "sankey", "x": "payment_method", "y": "success_rate"},
                    BREAKDOWN, ["success_rate"])
    assert spec["chart_type"] == "bar"
    assert any("not a chart this UI draws" in o for o in spec["overrides"])


def test_a_waterfall_is_refused_because_v2_computes_no_decomposition():
    """Drawing one from ordinary rows would picture an analysis never performed."""
    spec = validate({"chart_type": "waterfall", "x": "component", "y": "success_rate"},
                    BREAKDOWN, ["success_rate"])
    assert spec["chart_type"] != "waterfall"


def test_no_rows_is_a_table_not_a_broken_chart():
    spec = validate({"chart_type": "bar", "x": "a", "y": "b"}, [], [])
    assert spec["chart_type"] == "table"
    assert any("no rows" in o for o in spec["overrides"])


def test_a_series_column_that_does_not_exist_is_dropped_not_fatal():
    spec = validate({"chart_type": "bar", "x": "payment_method",
                     "y": "success_rate", "series": "ghost"},
                    BREAKDOWN, ["success_rate"])
    assert spec["chart_type"] == "bar" and spec["series"] is None
    assert any("series column" in o for o in spec["overrides"])


# ── the UI actually shows it ────────────────────────────────────────────────
# A chart spec reaching the API means nothing if the page never draws it. The
# first attempt at this shipped a validator, a renderer and a palette, and the
# measurement card still had no Chart tab — a str.replace had silently matched
# nothing. These pin the wiring so that cannot recur unnoticed.

import pathlib

APP_JS = pathlib.Path(__file__).parent.parent / "frontend" / "app.js"
INDEX = pathlib.Path(__file__).parent.parent / "frontend" / "index.html"
STYLE = pathlib.Path(__file__).parent.parent / "frontend" / "style.css"


def test_the_page_loads_the_chart_library():
    # Served from this app's own frontend directory. It used to come over a
    # /shared mount of the v1 UI so one fix would land in both front ends;
    # with v1 gone there is no second front end, and a mount pointing at a
    # directory that no longer exists is a failure waiting to happen.
    html = INDEX.read_text(encoding="utf-8")
    assert 'src="charts.js"' in html
    assert (INDEX.parent / "charts.js").is_file(), "the renderer is not shipped"
    assert 'id="tip"' in html, "charts.js writes tooltips into #tip"


def test_the_stylesheet_defines_the_series_tokens_charts_js_reads():
    css = STYLE.read_text(encoding="utf-8")
    for tok in ("--s1", "--s7", "--grid", ".kpi", ".axis", ".legend"):
        assert tok in css, f"{tok} missing; charts.js reads it off :root"


def test_both_cards_render_a_chart_host():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'data-chart="main"' in js, "measurement card has no chart host"
    assert 'data-chart="${esc(e.id)}"' in js, "advice card has no chart host"


def test_both_cards_offer_a_chart_tab():
    js = APP_JS.read_text(encoding="utf-8")
    assert 'tab("chart", "Chart", true)' in js
    assert 'tab("chart", "Charts", true)' in js


def test_the_hosts_are_filled_after_the_node_is_built():
    js = APP_JS.read_text(encoding="utf-8")
    # The call lives in paint(), which fills the answer half of a turn for both
    # a freshly streamed answer and one rebuilt from storage. Pinning the call
    # site rather than the whole expression: the host variable was renamed once
    # already and the point is that SOMETHING fills the hosts.
    assert "drawCharts(host, t)" in js, "nothing ever fills the chart hosts"
    assert "function drawCharts" in js and "function chartNode" in js
    assert "function paint" in js and "paint(wrap, t)" in js


def test_the_chart_spec_survives_a_reload():
    js = APP_JS.read_text(encoding="utf-8")
    assert "chart: d.chart" in js, "trim() drops the spec, so a reload loses the chart"
