from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from test_live_trading_engine_scenarios import BotError, live


def iter_labeled_messages():
    for path in sorted(Path("data").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if "messages" in row:
                message = row["messages"][1]["content"]
                actions = json.loads(row["messages"][2]["content"])["actions"]
                yield message, actions
            elif isinstance(row.get("label"), dict) and isinstance(
                row["label"].get("correct_actions"), list
            ):
                yield row["source"]["message"], row["label"]["correct_actions"]


def labels_by_message():
    labels = {}
    for message, actions in iter_labeled_messages():
        canonical = json.dumps(actions, ensure_ascii=False, sort_keys=True)
        previous = labels.setdefault(message, canonical)
        assert previous == canonical, f"conflicting label for {message!r}"
    return {message: json.loads(actions) for message, actions in labels.items()}


def is_explicit_directional_reentry(message: str, action_type: str) -> bool:
    word = "롱" if action_type == "OPEN_LONG" else "숏"
    match = re.search(fr"{word}\s*재진입", message)
    if not match:
        return False
    if re.search(r"재진입\s*(?:없|안\s*할|하지\s*않)", message):
        return False
    prefix = message[: match.start()]
    return not re.search(
        r"(?:만약|손절(?:나면|나도|걸리면)|짤리면|걸리면|나오면)\s*$",
        prefix,
    )


def test_reentry_dataset_uses_one_explicit_contract():
    for message, actions in iter_labeled_messages():
        for action in actions:
            if action["type"] in {"OPEN_LONG", "OPEN_SHORT"}:
                assert not is_explicit_directional_reentry(message, action["type"]), (
                    message,
                    action,
                )
            if action["type"] == "OPEN_REENTRY":
                assert set(action) == {"type", "price", "side"}
                assert action["side"] in {None, "long", "short"}
            else:
                assert "side" not in action


def test_known_directional_and_directionless_reentry_labels():
    labels = labels_by_message()
    assert labels["롱 재진입 ㅂㅈ"] == [
        {"type": "OPEN_REENTRY", "price": None, "side": "long"}
    ]
    assert labels["롱 재진입 ㅂㅈ 손절난 동일비중이에요"] == [
        {"type": "OPEN_REENTRY", "price": None, "side": "long"}
    ]
    assert labels["비트 숏 재진입 할게요!"] == [
        {"type": "OPEN_REENTRY", "price": None, "side": "short"}
    ]
    assert labels["재진입 한번만 더 볼게요!"] == [
        {"type": "OPEN_REENTRY", "price": None, "side": None}
    ]


def test_negated_or_conditional_reentry_is_not_executed_now():
    labels = labels_by_message()
    assert labels["롱 ㅂㅈ 손절나도 재진입 없어요 !"] == [
        {"type": "OPEN_LONG", "price": None}
    ]
    assert labels["재진입 나오면 손절 난 동일비중으로 재진입들 하시면돼요!"] == []
    assert labels["86890손절 걸고 걸리면 재진입 한번만 더 보겠습니다!"] == [
        {"type": "SET_STOP", "price": 86890}
    ]


def test_parse_actions_accepts_directional_reentry():
    actions = live.parse_actions(
        '{"actions":[{"type":"OPEN_REENTRY","price":null,"side":"short"}]}'
    )
    assert actions == [{"type": "OPEN_REENTRY", "price": None, "side": "short"}]


def test_parse_actions_keeps_old_directionless_reentry_compatible():
    actions = live.parse_actions(
        '{"actions":[{"type":"OPEN_REENTRY","price":null}]}'
    )
    assert actions == [{"type": "OPEN_REENTRY", "price": None, "side": None}]


@pytest.mark.parametrize("side", ["LONG", "buy", "", 1])
def test_parse_actions_rejects_invalid_reentry_side(side):
    raw = json.dumps(
        {"actions": [{"type": "OPEN_REENTRY", "price": None, "side": side}]}
    )
    with pytest.raises(BotError, match="Invalid OPEN_REENTRY side"):
        live.parse_actions(raw)


def test_parse_actions_rejects_side_on_normal_open():
    with pytest.raises(BotError, match="Invalid action object"):
        live.parse_actions(
            '{"actions":[{"type":"OPEN_LONG","price":null,"side":"long"}]}'
        )
