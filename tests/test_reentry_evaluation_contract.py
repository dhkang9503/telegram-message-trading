from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def load_normalize_result(path: str):
    source = Path(path).read_text(encoding="utf-8")
    module = ast.parse(source)
    node = next(
        item
        for item in module.body
        if isinstance(item, ast.FunctionDef) and item.name == "normalize_result"
    )
    namespace = {"Any": Any}
    exec(
        "from __future__ import annotations\n"
        + ast.get_source_segment(source, node),
        namespace,
    )
    return namespace["normalize_result"]


def assert_reentry_side_contract(normalize_result):
    expected = normalize_result(
        {"actions": [{"type": "OPEN_REENTRY", "price": None, "side": "long"}]}
    )
    wrong_side = normalize_result(
        {"actions": [{"type": "OPEN_REENTRY", "price": None, "side": "short"}]}
    )
    missing_side = normalize_result(
        {"actions": [{"type": "OPEN_REENTRY", "price": None}]}
    )
    normal_open = normalize_result(
        {"actions": [{"type": "OPEN_LONG", "price": None, "side": "long"}]}
    )

    assert expected == {
        "actions": [
            {"type": "OPEN_REENTRY", "price": None, "side": "long"}
        ]
    }
    assert wrong_side != expected
    assert missing_side == {
        "actions": [
            {"type": "OPEN_REENTRY", "price": None, "side": None}
        ]
    }
    assert normal_open == {
        "actions": [{"type": "OPEN_LONG", "price": None}]
    }


def test_local_evaluator_preserves_reentry_side():
    assert_reentry_side_contract(load_normalize_result("scripts/evaluate.py"))


def test_modal_evaluator_preserves_reentry_side():
    assert_reentry_side_contract(load_normalize_result("modal/train_app.py"))


def test_all_evaluators_report_side_accuracy():
    for path in (
        "scripts/evaluate.py",
        "modal/train_app.py",
        "scripts/prepare_modal_source.py",
    ):
        source = Path(path).read_text(encoding="utf-8")
        assert '"action_side_exact_rate"' in source
        assert 'a.get("side")' in source
