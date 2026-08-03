from __future__ import annotations

from live.live_bot import prioritize_cancel_actions


def action(action_type: str, price: int | None = None) -> dict:
    return {"type": action_type, "price": price}


def test_cancel_add_runs_before_add_without_mutating_model_output():
    actions = [action("ADD"), action("CANCEL_ADD")]

    reordered = prioritize_cancel_actions(actions)

    assert reordered == [action("CANCEL_ADD"), action("ADD")]
    assert actions == [action("ADD"), action("CANCEL_ADD")]


def test_cancel_actions_and_other_actions_keep_relative_order():
    actions = [
        action("ADD"),
        action("CANCEL_STOP"),
        action("SET_STOP", 67110),
        action("CANCEL_ADD"),
    ]

    assert prioritize_cancel_actions(actions) == [
        action("CANCEL_STOP"),
        action("CANCEL_ADD"),
        action("ADD"),
        action("SET_STOP", 67110),
    ]


def test_non_cancel_actions_keep_model_order():
    actions = [action("ADD", 800), action("SET_STOP", 67110)]

    assert prioritize_cancel_actions(actions) == actions


def test_single_action_is_unchanged():
    actions = [action("CANCEL_ADD")]

    assert prioritize_cancel_actions(actions) == actions
