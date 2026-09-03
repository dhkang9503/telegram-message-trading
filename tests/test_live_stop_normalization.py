from __future__ import annotations

import ast
from decimal import Decimal, ROUND_DOWN
from pathlib import Path


def load_stop_normalization():
    source = Path("live/live_bot.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    names = {
        "BotError",
        "D",
        "restore_btc_price",
        "stop_action_validation_error",
        "validate_and_deduplicate_stop_actions",
    }
    nodes = [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in names
    ]
    namespace = {
        "Decimal": Decimal,
        "ROUND_DOWN": ROUND_DOWN,
        "PRICE_RESTORE_MAX_GAP_RATIO": Decimal("0.03"),
    }
    exec(
        "from __future__ import annotations\n"
        "from typing import Any, Optional\n"
        + "\n\n".join(ast.get_source_segment(source, node) for node in nodes),
        namespace,
    )
    return namespace


class Config:
    price_step = Decimal("0.1")

    def floor_price(self, value: Decimal) -> Decimal:
        units = (value / self.price_step).to_integral_value(
            rounding=ROUND_DOWN
        )
        return units * self.price_step


ns = load_stop_normalization()
validate_and_deduplicate_stop_actions = ns[
    "validate_and_deduplicate_stop_actions"
]


def action(action_type: str, price: int | None = None) -> dict:
    return {"type": action_type, "price": price}


def test_validation_happens_before_stop_deduplication():
    actions = [
        action("ADD", 64100),
        action("SET_STOP", 64410),
        action("SET_STOP", 64),
    ]

    normalized, rejected, deduplicated = (
        validate_and_deduplicate_stop_actions(
            actions, Decimal("63872.2"), "short", Config()
        )
    )

    assert normalized == [
        action("ADD", 64100),
        action("SET_STOP", 64410),
    ]
    assert rejected[0]["action"] == action("SET_STOP", 64)
    assert "must be above mark price" in rejected[0]["reason"]
    assert deduplicated == []


def test_last_valid_stop_wins_after_validation():
    actions = [
        action("SET_STOP", 64410),
        action("ADD", 64100),
        action("SET_STOP", 64500),
    ]

    normalized, rejected, deduplicated = (
        validate_and_deduplicate_stop_actions(
            actions, Decimal("63872.2"), "short", Config()
        )
    )

    assert normalized == [
        action("ADD", 64100),
        action("SET_STOP", 64500),
    ]
    assert rejected == []
    assert deduplicated == [
        {
            "action": action("SET_STOP", 64410),
            "reason": "superseded_by_later_valid_set_stop",
        }
    ]


def test_long_stop_must_be_below_mark_price():
    actions = [
        action("SET_STOP", 63000),
        action("SET_STOP", 64410),
    ]

    normalized, rejected, deduplicated = (
        validate_and_deduplicate_stop_actions(
            actions, Decimal("63872.2"), "long", Config()
        )
    )

    assert normalized == [action("SET_STOP", 63000)]
    assert rejected[0]["action"] == action("SET_STOP", 64410)
    assert deduplicated == []


def test_non_stop_actions_are_not_changed():
    actions = [action("ADD", 64100), action("CANCEL_ADD")]

    normalized, rejected, deduplicated = (
        validate_and_deduplicate_stop_actions(
            actions, Decimal("63872.2"), "short", Config()
        )
    )

    assert normalized == actions
    assert rejected == []
    assert deduplicated == []


def test_initial_margin_uses_account_equity_ratio():
    source = Path("live/live_bot.py").read_text(encoding="utf-8")

    assert 'INITIAL_MARGIN_EQUITY_RATIO = Decimal("0.01")' in source
    assert 'sizing_source = "account_usdt_equity_ratio"' in source
    assert "INITIAL_MARGIN_USDT" not in source
    assert "fixed_6_usdt" not in source
