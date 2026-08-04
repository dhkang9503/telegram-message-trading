from __future__ import annotations

import ast
import json
import re
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_price_alignment():
    source = (ROOT / "live/live_bot.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    names = {
        "BotError",
        "D",
        "extract_source_price_tokens",
        "_has_significant_leading_zero",
        "align_action_prices_to_source",
        "restore_btc_price",
    }
    nodes = [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in names
    ]
    namespace: dict[str, Any] = {
        "Decimal": Decimal,
        "ROUND_DOWN": ROUND_DOWN,
        "PRICE_RESTORE_MAX_GAP_RATIO": Decimal("0.03"),
        "SOURCE_PRICE_TOKEN_RE": re.compile(
            r"(?<![\d,])(\d[\d,]*(?:\.\d+)?)(?![\d,]|\.\d)"
        ),
    }
    exec(
        "from __future__ import annotations\n"
        "from typing import Any\n"
        + "\n\n".join(
            ast.get_source_segment(source, node) for node in nodes
        ),
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


ns = load_price_alignment()
BotError = ns["BotError"]
align_action_prices_to_source = ns["align_action_prices_to_source"]
restore_btc_price = ns["restore_btc_price"]


def action(action_type: str, price: int | float | None = None) -> dict:
    return {"type": action_type, "price": price}


def dataset_actions(message: str) -> list[dict]:
    for path in sorted((ROOT / "data/base").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["messages"][1]["content"] == message:
                return json.loads(row["messages"][2]["content"])["actions"]
    raise AssertionError(f"Dataset message not found: {message!r}")


def feedback_actions(message: str) -> list[dict]:
    path = ROOT / "data/feedback/mistakes_labeled.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["source"]["message"] == message:
            return row["label"]["correct_actions"]
    raise AssertionError(f"Feedback message not found: {message!r}")


def restored_prices(
    source_text: str,
    actions: list[dict],
    reference: str,
) -> list[Decimal | None]:
    aligned = align_action_prices_to_source(source_text, actions)
    return [
        None
        if item["price"] is None
        else restore_btc_price(
            item["price"], Decimal(reference), Config()
        )
        for item in aligned
    ]


def test_feedback_leading_zero_fragment_restores_64050():
    message = "다들 계시죠? 050 물타기 걸어두고 보겠습니다!"
    actions = feedback_actions(message)

    aligned = align_action_prices_to_source(message, actions)

    assert aligned == [{"type": "ADD", "price": "050"}]
    assert restored_prices(message, actions, "63734.2") == [
        Decimal("64050")
    ]


def test_sentence_period_after_leading_zero_price_is_not_a_decimal():
    message = "050. 물타기 걸게요"
    actions = [action("ADD", 50)]

    assert align_action_prices_to_source(message, actions) == [
        {"type": "ADD", "price": "050"}
    ]
    assert restored_prices(message, actions, "63734.2") == [
        Decimal("64050")
    ]


def test_two_different_short_prices_align_independently():
    message = "150 손절 050 물타기 걸게요"
    actions = [action("SET_STOP", 150), action("ADD", 50)]

    aligned = align_action_prices_to_source(message, actions)

    assert aligned == [
        {"type": "SET_STOP", "price": 150},
        {"type": "ADD", "price": "050"},
    ]
    assert restored_prices(message, actions, "63734.2") == [
        Decimal("64150"),
        Decimal("64050"),
    ]


@pytest.mark.parametrize(
    ("message", "reference", "expected"),
    [
        (
            "800물타기 67110손절 걸고보겠습니다!",
            "67350",
            [Decimal("67800"), Decimal("67110")],
        ),
        (
            "650물타기 910손절 걸고 보겠습니다!",
            "67600",
            [Decimal("67650"), Decimal("67910")],
        ),
        (
            "59350물타기 58990손절들 거시면돼요!",
            "59100",
            [Decimal("59350"), Decimal("58990")],
        ),
    ],
)
def test_dataset_priced_actions_restore_from_source_tokens(
    message: str,
    reference: str,
    expected: list[Decimal],
):
    actions = dataset_actions(message)

    assert restored_prices(message, actions, reference) == expected


def test_full_price_dataset_action_is_unchanged():
    message = "59350 물타기 하나 걸겠습니다!"
    actions = dataset_actions(message)

    assert align_action_prices_to_source(message, actions) == actions
    assert restored_prices(message, actions, "59100") == [
        Decimal("59350")
    ]


def test_three_digit_zero_suffix_restores_next_thousand():
    message = "000 물타기 걸게요"
    actions = [action("ADD", 0)]

    assert align_action_prices_to_source(message, actions) == [
        {"type": "ADD", "price": "000"}
    ]
    assert restored_prices(message, actions, "63734.2") == [
        Decimal("64000")
    ]


def test_plain_numeric_zero_remains_invalid():
    with pytest.raises(BotError, match="Price must be positive"):
        restore_btc_price(0, Decimal("63734.2"), Config())


def test_unmatched_or_ambiguous_price_fails_closed():
    with pytest.raises(BotError, match="Could not uniquely align"):
        align_action_prices_to_source(
            "150 손절 걸게요", [action("ADD", 50)]
        )
    with pytest.raises(BotError, match="Could not uniquely align"):
        align_action_prices_to_source(
            "050 손절, 50 물타기", [action("ADD", 50)]
        )
