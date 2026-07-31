from __future__ import annotations

import json
from pathlib import Path

from scripts.build_train_dataset import apply_label_overrides
from scripts.validate_dataset import read_jsonl


ROOT = Path(__file__).resolve().parents[1]


def actions_for(rows: list[dict], message: str) -> list[list[dict]]:
    matches = []
    for row in rows:
        if row["messages"][1]["content"] == message:
            matches.append(json.loads(row["messages"][2]["content"])["actions"])
    return matches


def test_partial_add_overrides_apply_to_every_stored_split():
    expected_changed_rows = {
        "train.jsonl": 48,
        "validation.jsonl": 10,
        "test.jsonl": 12,
    }
    for filename, expected in expected_changed_rows.items():
        path = ROOT / "data/base" / filename
        rows, report = apply_label_overrides(path, read_jsonl(path))
        assert report["label_overrides_defined"] == 48
        assert report["label_override_rows_changed"] == expected

        for row in rows:
            message = row["messages"][1]["content"]
            if message in {
                "조금만 더살게요!",
                "쪼금만 더 탈게요 지금!",
                "위로 열린차트라 물좀 타보고 안되면 본전에 정리하던지 할게요!",
            }:
                actions = json.loads(row["messages"][2]["content"])["actions"]
                assert actions == []


def test_non_add_actions_are_preserved_for_mixed_messages():
    train_path = ROOT / "data/base/train.jsonl"
    rows, _ = apply_label_overrides(train_path, read_jsonl(train_path))

    assert actions_for(rows, "물타기 취소하고 지금 반탈게요!") == [
        [{"type": "CANCEL_ADD", "price": None}]
    ]
    assert actions_for(rows, "아니다 지금 짤짤이 더 태우고 손절 790으로 맞출게요!") == [
        [{"type": "SET_STOP", "price": 790}]
    ]


def test_official_full_add_messages_are_unchanged():
    train_path = ROOT / "data/base/train.jsonl"
    rows, _ = apply_label_overrides(train_path, read_jsonl(train_path))

    assert actions_for(rows, "물 ㅂㅈ")
    assert all(
        any(action["type"] == "ADD" for action in actions)
        for actions in actions_for(rows, "물 ㅂㅈ")
    )
    assert actions_for(rows, "59350 물타기 하나 걸겠습니다!") == [
        [{"type": "ADD", "price": 59350}]
    ]


def test_live_partial_add_is_a_no_action_mistake():
    rows = read_jsonl(ROOT / "data/feedback/mistakes_labeled.jsonl")
    labels = {
        row["source"]["message"]: row["label"]["correct_actions"]
        for row in rows
    }

    assert labels["물 조금만 더 태울게요!"] == []
    assert labels["첫비중 더 살게요!"] == []
    assert labels["물ㅂㅈ"] == [{"type": "ADD", "price": None}]
