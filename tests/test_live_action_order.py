from __future__ import annotations

import ast
from pathlib import Path


def load_prioritize_cancel_actions():
    source = Path("live/live_bot.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "prioritize_cancel_actions"
    )
    function_source = ast.get_source_segment(source, function)
    namespace: dict = {}
    exec(
        "from __future__ import annotations\n" + function_source,
        namespace,
    )
    return namespace["prioritize_cancel_actions"]


prioritize_cancel_actions = load_prioritize_cancel_actions()


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
