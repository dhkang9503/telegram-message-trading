from __future__ import annotations

"""Keep the parser contract identical across training data and live inference."""

import ast
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET_MESSAGE = "못 줄이신분들 지금 줄이세요!!"


def configured_prompt() -> str:
    config = yaml.safe_load((ROOT / "configs" / "train.yaml").read_text(encoding="utf-8"))
    return config["model"]["system_prompt"]


def live_prompt() -> str:
    source = (ROOT / "live" / "live_bot.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT" for target in node.targets)
    )
    return ast.literal_eval(assignment.value)


def chatml_paths() -> list[Path]:
    return [
        ROOT / "data" / "base" / "train.jsonl",
        ROOT / "data" / "base" / "validation.jsonl",
        ROOT / "data" / "base" / "test.jsonl",
        ROOT / "data" / "regression" / "live_failures.jsonl",
    ]


def test_system_prompt_is_identical_in_config_live_and_chatml_data():
    expected = configured_prompt()
    assert live_prompt() == expected
    assert "수신자의 선택이나 상태에 따라 적용 여부가 달라지면" in expected
    assert "채널 표현 '비트 자유롭게'는 CLOSE_ALL이다." in expected

    for path in chatml_paths():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            assert row["messages"][0]["content"] == expected, (path, line_number)


def test_conditional_close_adds_message_is_labeled_no_action_everywhere():
    labels: list[list[dict]] = []
    for path in chatml_paths():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["messages"][1]["content"] == TARGET_MESSAGE:
                labels.append(json.loads(row["messages"][2]["content"])["actions"])

    feedback_path = ROOT / "data" / "feedback" / "mistakes_labeled.jsonl"
    for line in feedback_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["source"]["message"] == TARGET_MESSAGE:
            labels.append(row["label"]["correct_actions"])

    assert labels
    assert all(actions == [] for actions in labels)
