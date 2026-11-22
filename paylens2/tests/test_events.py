"""
The event channel, and the recoveries it makes visible.

WHY THESE EXIST
---------------
The UI used to animate a fixed list of stages on a 1100ms timer. It advanced
whether or not anything had happened, and on a measured run it gave the guard
checks (18ms) the same screen time as the model call (7,668ms). Meanwhile the
pipeline really was repairing malformed JSON, recovering truncated responses
and retrying the provider on most requests — and every one of those events
existed only as a log line on the server's stdout.

These pin the replacement: events are emitted by the code that does the work,
recoveries land on the answer, and an unbound channel costs nothing so the
classic pipeline is unaffected by the emit calls in shared modules.
"""
from __future__ import annotations

import pathlib

from backend.core import events
from paylens2.core import jsonx

ROOT = pathlib.Path(__file__).parent.parent
APP_JS = ROOT / "frontend" / "app.js"
ROUTES = ROOT / "api" / "routes.py"


# ── the channel ─────────────────────────────────────────────────────────────
def test_emitting_without_a_channel_is_a_no_op():
    # provider.py and jsonx.py emit unconditionally. The classic pipeline never
    # binds a channel, so this has to be free and silent rather than an error.
    events.emit("stage", "author", status="start")
    events.recovery("author", "nothing is listening")
    assert events.current() is None


def test_a_bound_channel_collects_and_forwards_live():
    seen: list[dict] = []
    ch = events.Channel(sink=seen.append)
    with events.bind(ch):
        events.emit("stage", "author", status="start", message="writing")
        events.recovery("author", "repaired something")
    assert [e["stage"] for e in ch.events] == ["author", "author"]
    assert len(seen) == 2, "the sink must see events as they happen, not at the end"
    assert [e["kind"] for e in ch.of_kind("recovery")] == ["recovery"]


def test_the_channel_is_unbound_again_afterwards():
    with events.bind(events.Channel()):
        assert events.current() is not None
    assert events.current() is None


def test_a_consumer_that_goes_away_does_not_break_the_request():
    # A closed SSE connection must not take the pipeline down with it: the work
    # is still worth finishing for the caller waiting on the final answer.
    def dead(_ev):
        raise BrokenPipeError("client disconnected")

    ch = events.Channel(sink=dead)
    with events.bind(ch):
        events.emit("stage", "author", status="start")
        events.emit("stage", "author", status="done")
    assert len(ch.events) == 2, "events are still recorded after the sink dies"
    assert ch.sink is None, "a dead sink is dropped rather than retried"


def test_the_stage_helper_reports_a_real_duration():
    ch = events.Channel()
    with events.bind(ch):
        with events.stage("execute", "running it"):
            pass
    start, done = ch.events
    assert start["status"] == "start" and done["status"] == "done"
    assert done["ms"] >= 0 and "ms" in done


# ── the recoveries that used to be invisible ────────────────────────────────
def test_repairing_malformed_json_is_reported_as_a_recovery():
    # This is the real case: Qwen 2.5 Coder writes correct SQL inside careless
    # JSON. It was repaired silently on most requests and nobody could tell.
    broken = '{"action": "sql", sql: "SELECT 1 AS n FROM v_attempts",}'
    ch = events.Channel()
    with events.bind(ch):
        out = jsonx.loads(broken)
    assert out["action"] == "sql"
    msgs = [e["message"] for e in ch.of_kind("recovery")]
    assert any("malformed" in m for m in msgs), msgs


def test_a_truncated_response_is_reported_as_a_recovery():
    cut = '{"action": "sql", "sql": "SELECT 1 AS n FROM v_attempts", "chart": {"x'
    ch = events.Channel()
    with events.bind(ch):
        out = jsonx.loads(cut)
    assert "SELECT" in out["sql"]
    assert ch.of_kind("recovery"), "a recovered truncation must be reported"


# ── the wiring, so the timer cannot come back unnoticed ─────────────────────
def test_the_pipeline_accepts_a_listener_and_records_recoveries():
    import inspect

    from paylens2.core.pipeline import V2Answer, ask

    assert "on_event" in inspect.signature(ask).parameters
    assert "recoveries" in V2Answer.__dataclass_fields__


def test_the_streaming_route_exists():
    src = ROUTES.read_text(encoding="utf-8")
    assert '"/ask/stream"' in src
    assert "text/event-stream" in src
    assert "on_event=" in src, "the route must pass a listener into the pipeline"


def test_the_ui_reads_events_rather_than_running_a_timer():
    js = APP_JS.read_text(encoding="utf-8")
    assert "runStreamed" in js and "/v2/ask/stream" in js
    assert "STAGE_LABEL" in js
    # The old fake: a fixed list advanced on an interval regardless of the server.
    assert "const STAGES = [" not in js, "the hardcoded stage list is back"
    assert "function runStages" not in js, "the timer-driven animation is back"
    # And the plain endpoint stays as the fallback, so a proxy that will not
    # pass an event stream still gets an answer.
    assert "runPlain" in js and '"/v2/ask"' in js


def test_recoveries_survive_a_reload_and_are_shown():
    js = APP_JS.read_text(encoding="utf-8")
    assert "recoveries: d.recoveries" in js, "trim() drops them, so a reload loses them"
    assert "self-repair" in js, "nothing surfaces them to the reader"
