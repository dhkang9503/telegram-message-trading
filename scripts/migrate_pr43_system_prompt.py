from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CURRENT_PROMPT = (
    "너는 BTCUSDT 리딩 메시지를 actions JSON으로 변환한다. "
    "모든 수신자가 지금 즉시 실행할 명확한 거래 지시만 추출한다. "
    "선택·권고·조건·미래 계획이거나 특정 상태의 사람에게만 적용되면 "
    "actions는 빈 배열이다. OPEN_REENTRY의 side는 long, short 또는 null만 사용한다. "
    "애매하면 actions는 빈 배열이다. JSON만 출력한다."
)
PR43_PROMPT = (
    "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 "
    "변환하는 파서다. 메시지에 명시된 행동만 추출하고 추측하지 않는다. "
    "출력은 actions 배열을 가진 JSON 하나만 반환한다."
)

CHATML_PATHS = [
    ROOT / "data/base/train.jsonl",
    ROOT / "data/base/validation.jsonl",
    ROOT / "data/base/test.jsonl",
    ROOT / "data/regression/live_failures.jsonl",
]


def replace_exact(path: Path, old: str, new: str, *, expected_count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected_count:
        raise RuntimeError(
            f"{path}: expected {expected_count} occurrence(s), found {count}"
        )
    path.write_text(text.replace(old, new), encoding="utf-8")


def migrate_chatml(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = 0
    output: list[str] = []

    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        row = json.loads(line)
        messages = row.get("messages")
        if not isinstance(messages, list) or not messages:
            raise RuntimeError(f"{path}:{line_number}: missing messages")
        system = messages[0]
        if system.get("role") != "system":
            raise RuntimeError(f"{path}:{line_number}: first message is not system")
        prompt = system.get("content")
        if prompt != CURRENT_PROMPT:
            raise RuntimeError(
                f"{path}:{line_number}: unexpected system prompt {prompt!r}"
            )
        system["content"] = PR43_PROMPT
        changed += 1
        output.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))

    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return changed


def read_live_prompt(path: Path) -> str:
    module = ast.parse(path.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT"
            for target in node.targets
        )
    )
    value = ast.literal_eval(assignment.value)
    if not isinstance(value, str):
        raise RuntimeError("live SYSTEM_PROMPT is not a string")
    return value


def main() -> None:
    config_path = ROOT / "configs/train.yaml"
    replace_exact(config_path, CURRENT_PROMPT, PR43_PROMPT)

    live_path = ROOT / "live/live_bot.py"
    old_live_block = '''SYSTEM_PROMPT = (
    "너는 BTCUSDT 리딩 메시지를 actions JSON으로 변환한다. "
    "모든 수신자가 지금 즉시 실행할 명확한 거래 지시만 추출한다. "
    "선택·권고·조건·미래 계획이거나 특정 상태의 사람에게만 적용되면 "
    "actions는 빈 배열이다. OPEN_REENTRY의 side는 long, short 또는 null만 사용한다. "
    "애매하면 actions는 빈 배열이다. JSON만 출력한다."
)
'''
    new_live_block = '''SYSTEM_PROMPT = (
    "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 "
    "변환하는 파서다. 메시지에 명시된 행동만 추출하고 추측하지 않는다. "
    "출력은 actions 배열을 가진 JSON 하나만 반환한다."
)
'''
    replace_exact(live_path, old_live_block, new_live_block)

    changed_rows = {
        str(path.relative_to(ROOT)): migrate_chatml(path) for path in CHATML_PATHS
    }

    test_path = ROOT / "tests/test_system_prompt_contract.py"
    test_text = test_path.read_text(encoding="utf-8")
    marker = 'TARGET_MESSAGE = "못 줄이신분들 지금 줄이세요!!"\n'
    if marker not in test_text:
        raise RuntimeError("test prompt marker was not found")
    test_text = test_text.replace(
        marker,
        marker + f'PR43_SYSTEM_PROMPT = {PR43_PROMPT!r}\n',
        1,
    )
    old_assertions = '''    assert "특정 상태의 사람에게만 적용되면" in expected
    assert "OPEN_REENTRY의 side는 long, short 또는 null만 사용한다." in expected
    assert "'비트 자유롭게'는 CLOSE_ALL이다." not in expected
'''
    new_assertions = '''    assert expected == PR43_SYSTEM_PROMPT
'''
    if test_text.count(old_assertions) != 1:
        raise RuntimeError("current prompt assertions were not found exactly once")
    test_path.write_text(
        test_text.replace(old_assertions, new_assertions, 1),
        encoding="utf-8",
    )

    config_text = config_path.read_text(encoding="utf-8")
    if CURRENT_PROMPT in config_text:
        raise RuntimeError("current prompt remains in config")
    if PR43_PROMPT not in config_text:
        raise RuntimeError("PR 43 prompt was not installed in config")
    if read_live_prompt(live_path) != PR43_PROMPT:
        raise RuntimeError("PR 43 prompt was not installed in live bot")
    if "PR43_SYSTEM_PROMPT" not in test_path.read_text(encoding="utf-8"):
        raise RuntimeError("prompt contract test was not updated")

    print(json.dumps({"changed_chatml_rows": changed_rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
