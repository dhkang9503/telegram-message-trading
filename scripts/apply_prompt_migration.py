from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OLD_PROMPT = (
    "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 변환하는 파서다. "
    "메시지에 명시된 행동만 추출하고 추측하지 않는다. "
    "출력은 actions 배열을 가진 JSON 하나만 반환한다."
)
NEW_PROMPT = (
    "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 변환하는 파서다. "
    "현재 즉시 실행할 확정 지시만 추출하고 추측하지 않는다. "
    "'하실 분들', '원하시면', '못 하신 분들', '대응 안 되시는 분들', '평단 좋으신 분들'처럼 "
    "수신자의 선택이나 상태에 따라 적용 여부가 달라지면 명령형이어도 actions는 빈 배열이다. "
    "조건·가정·권고·미래 계획·개인 행동만 나타내는 문장도 actions는 빈 배열이다. "
    "대상 제한 없이 지금 실행하라는 행동이 명확한 경우에만 액션을 추출한다. "
    "채널 표현 '비트 자유롭게'는 CLOSE_ALL이다. "
    "애매하면 actions는 빈 배열이다. "
    "출력은 actions 배열을 가진 JSON 하나만 반환한다."
)
TARGET_MESSAGE = "못 줄이신분들 지금 줄이세요!!"
EMPTY_ACTIONS_TEXT = json.dumps({"actions": []}, ensure_ascii=False, separators=(",", ":"))


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def update_config() -> None:
    path = ROOT / "configs" / "train.yaml"
    text = path.read_text(encoding="utf-8")
    old = f"  system_prompt: >-\n    {OLD_PROMPT}"
    new = f"  system_prompt: >-\n    {NEW_PROMPT}"
    if text.count(old) != 1:
        raise RuntimeError("configs/train.yaml does not contain exactly one expected prompt")
    write_text(path, text.replace(old, new))


def update_live_bot() -> None:
    path = ROOT / "live" / "live_bot.py"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r'SYSTEM_PROMPT = \(\n(?:    ".*"\n)+\)')
    replacement = '''SYSTEM_PROMPT = (
    "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 "
    "변환하는 파서다. 현재 즉시 실행할 확정 지시만 추출하고 추측하지 않는다. "
    "'하실 분들', '원하시면', '못 하신 분들', '대응 안 되시는 분들', "
    "'평단 좋으신 분들'처럼 수신자의 선택이나 상태에 따라 적용 여부가 달라지면 "
    "명령형이어도 actions는 빈 배열이다. 조건·가정·권고·미래 계획·개인 행동만 "
    "나타내는 문장도 actions는 빈 배열이다. 대상 제한 없이 지금 실행하라는 행동이 "
    "명확한 경우에만 액션을 추출한다. 채널 표현 '비트 자유롭게'는 CLOSE_ALL이다. "
    "애매하면 actions는 빈 배열이다. 출력은 actions 배열을 가진 JSON 하나만 반환한다."
)'''
    text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise RuntimeError("live/live_bot.py SYSTEM_PROMPT block was not found exactly once")
    write_text(path, text)


def migrate_chatml_file(path: Path) -> tuple[int, int]:
    raw = path.read_text(encoding="utf-8")
    prompt_replacements = raw.count(OLD_PROMPT)
    raw = raw.replace(OLD_PROMPT, NEW_PROMPT)
    changed_labels = 0
    output: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "messages" in row:
            messages = row["messages"]
            if messages[0]["content"] != NEW_PROMPT:
                raise RuntimeError(f"{path}: unexpected system prompt")
            if messages[1]["content"] == TARGET_MESSAGE:
                messages[2]["content"] = EMPTY_ACTIONS_TEXT
                line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                changed_labels += 1
        output.append(line)
    write_text(path, "\n".join(output) + "\n")
    return prompt_replacements, changed_labels


def migrate_feedback() -> int:
    path = ROOT / "data" / "feedback" / "mistakes_labeled.jsonl"
    changed = 0
    output: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("source", {}).get("message") == TARGET_MESSAGE:
            row["label"]["correct_actions"] = []
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            changed += 1
        output.append(line)
    if changed != 1:
        raise RuntimeError(f"expected one feedback label for {TARGET_MESSAGE!r}, found {changed}")
    write_text(path, "\n".join(output) + "\n")
    return changed


def update_validator() -> None:
    path = ROOT / "scripts" / "validate_dataset.py"
    text = path.read_text(encoding="utf-8")

    old = "from typing import Any\n\nALLOWED_ACTIONS"
    new = "from typing import Any\n\nimport yaml\n\nALLOWED_ACTIONS"
    if text.count(old) != 1:
        raise RuntimeError("validator import anchor not found")
    text = text.replace(old, new)

    old = "def inspect_split(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:\n"
    new = (
        "def inspect_split(\n"
        "    path: Path, expected_system_prompt: str | None = None\n"
        ") -> tuple[list[dict[str, Any]], dict[str, Any]]:\n"
    )
    if text.count(old) != 1:
        raise RuntimeError("inspect_split signature anchor not found")
    text = text.replace(old, new)

    old = (
        "        system, message, canonical = validate_chatml_row(row, f\"{path}[{index}]\")\n"
        "        systems[system] += 1\n"
    )
    new = (
        "        system, message, canonical = validate_chatml_row(row, f\"{path}[{index}]\")\n"
        "        if expected_system_prompt is not None and system != expected_system_prompt:\n"
        "            raise ValueError(\n"
        "                f\"{path}[{index}]: system prompt does not match configs/train.yaml\"\n"
        "            )\n"
        "        systems[system] += 1\n"
    )
    if text.count(old) != 1:
        raise RuntimeError("inspect_split loop anchor not found")
    text = text.replace(old, new)

    old = (
        "    split_rows: dict[str, list[dict[str, Any]]] = {}\n"
        "    report: dict[str, Any] = {\"splits\": {}, \"overlap\": {}, \"feedback\": {}}\n\n"
        "    for split in (\"train\", \"validation\", \"test\"):\n"
    )
    new = (
        "    split_rows: dict[str, list[dict[str, Any]]] = {}\n"
        "    report: dict[str, Any] = {\"splits\": {}, \"overlap\": {}, \"feedback\": {}}\n"
        "    config = yaml.safe_load((root / \"configs\" / \"train.yaml\").read_text(encoding=\"utf-8\"))\n"
        "    expected_system_prompt = config[\"model\"][\"system_prompt\"]\n\n"
        "    for split in (\"train\", \"validation\", \"test\"):\n"
    )
    if text.count(old) != 1:
        raise RuntimeError("validate_repository_data config anchor not found")
    text = text.replace(old, new)

    old = "        rows, split_report = inspect_split(path)\n"
    new = "        rows, split_report = inspect_split(path, expected_system_prompt)\n"
    if text.count(old) != 1:
        raise RuntimeError("inspect_split call anchor not found")
    text = text.replace(old, new)

    old = (
        "    regression_rows = read_jsonl(regression_path)\n"
        "    for index, row in enumerate(regression_rows):\n"
        "        validate_chatml_row(row, f\"{regression_path}[{index}]\")\n"
    )
    new = (
        "    regression_rows = read_jsonl(regression_path)\n"
        "    for index, row in enumerate(regression_rows):\n"
        "        system, _, _ = validate_chatml_row(row, f\"{regression_path}[{index}]\")\n"
        "        if system != expected_system_prompt:\n"
        "            raise ValueError(\n"
        "                f\"{regression_path}[{index}]: system prompt does not match configs/train.yaml\"\n"
        "            )\n"
    )
    if text.count(old) != 1:
        raise RuntimeError("regression validation anchor not found")
    text = text.replace(old, new)
    write_text(path, text)


def update_scenario_test() -> None:
    path = ROOT / "tests" / "test_live_trading_engine_scenarios.py"
    text = path.read_text(encoding="utf-8")
    needle = f'        "{TARGET_MESSAGE}",\n'
    if text.count(needle) != 1:
        raise RuntimeError("scenario fixture for old label not found exactly once")
    write_text(path, text.replace(needle, ""))


def create_contract_test() -> None:
    path = ROOT / "tests" / "test_system_prompt_contract.py"
    content = '''from __future__ import annotations

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
'''
    write_text(path, content)


def main() -> None:
    update_config()
    update_live_bot()

    total_prompts = 0
    total_chatml_labels = 0
    for path in (
        ROOT / "data" / "base" / "train.jsonl",
        ROOT / "data" / "base" / "validation.jsonl",
        ROOT / "data" / "base" / "test.jsonl",
        ROOT / "data" / "regression" / "live_failures.jsonl",
    ):
        prompts, labels = migrate_chatml_file(path)
        total_prompts += prompts
        total_chatml_labels += labels

    if total_prompts == 0:
        raise RuntimeError("no stored ChatML prompts were migrated")
    migrate_feedback()
    update_validator()
    update_scenario_test()
    create_contract_test()

    print(
        json.dumps(
            {
                "prompt_rows_migrated": total_prompts,
                "chatml_labels_changed": total_chatml_labels,
                "feedback_labels_changed": 1,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
