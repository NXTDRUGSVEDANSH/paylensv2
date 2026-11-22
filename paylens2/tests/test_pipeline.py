"""v2 end to end, in the modes that need no live model."""
from __future__ import annotations

import pytest

from paylens2.core import model as v2model
from paylens2.core.author import Authored, validate
from paylens2.core.pipeline import ask


def test_unknown_role_is_refused():
    out = ask("success rate last week", role="not_a_role")
    assert out.ok is False and out.refused


@pytest.mark.skipif(not v2model.views_present(),
                    reason="v2 views not built: python -m paylens2.schema.build")
def test_missing_claim_is_refused_not_widened():
    out = ask("success rate last week", role="merchant_user", claims={})
    assert out.refused
    assert "claim" in out.refusal_reason.lower()


@pytest.mark.skipif(v2model.views_present(),
                    reason="views are built, so this path cannot be exercised")
def test_missing_views_fail_closed():
    out = ask("success rate last week")
    assert out.refused and "views are not built" in out.refusal_reason


def test_a_clarification_with_no_usable_options_is_downgraded():
    """Downgraded to a re-author, never to a claim about the user's data."""
    a = validate(Authored(action="clarify", clarify_question="which?", options=[]))
    assert a.action == "reauthor"


def test_an_option_that_is_not_a_question_is_dropped():
    from paylens2.core.author import _coerce
    a = _coerce({"action": "clarify", "question": "which measure?",
                 "options": [{"label": "GMV", "question": "gmv"},
                             {"label": "SR", "question": "success rate last week"}]})
    assert [o["label"] for o in a.options] == ["SR"]


def test_invented_playbook_ids_are_dropped():
    from paylens2.core.author import _coerce
    a = _coerce({"action": "sql", "sql": "SELECT 1 AS n FROM v_attempts",
                 "playbook_ids": ["retry_strategy", "made_up_playbook"]})
    assert a.playbook_ids == ["retry_strategy"]
    assert a.invented_playbooks == ["made_up_playbook"]


def test_a_refusal_naming_something_real_is_flagged_as_wrong():
    from paylens2.core.author import needs_reauthor
    a = validate(Authored(action="refuse", missing_concept="issuer_bank"))
    assert needs_reauthor(a)
    b = validate(Authored(action="refuse", missing_concept="chargebacks"))
    assert not needs_reauthor(b)


def test_v2_semantic_model_is_view_only():
    m = v2model.get_model()
    assert m.allowed_tables == {"v_attempts", "v_settlements"}
    assert all(d.table.startswith("v_") for d in m.dimensions.values())


def test_every_role_scope_anchors_on_a_view():
    """The regression guard for the hole described in test_rbac.py."""
    for role in v2model.get_model().roles.values():
        if not role.row_scope:
            continue
        anchor = role.row_scope.get("anchor_table") or role.row_scope.get("table")
        assert anchor and anchor.startswith("v_"), \
            f"role {role.key} anchors RBAC on '{anchor}', not a v2 view"


def test_every_refusal_path_agrees_on_ok():
    """
    A caller checking `ok` must not get a different answer depending on which
    rung refused. The data-refusal branch used to leave ok=True while fail()
    set it false.
    """
    import inspect

    from paylens2.core import pipeline
    src = inspect.getsource(pipeline.ask)
    # Every place that sets refused must also set ok False nearby.
    for i, line in enumerate(src.splitlines()):
        if "ans.refused = True" in line:
            window = "\n".join(src.splitlines()[max(0, i - 4):i + 2])
            assert "ans.ok" in window, f"refusal at line {i} does not set ok"
