from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEW_PROMPT = (
    "너는 BTCUSDT 리딩 메시지를 actions JSON으로 변환한다. "
    "모든 수신자가 지금 즉시 실행할 명확한 거래 지시만 추출한다. "
    "선택·권고·조건·미래 계획이거나 특정 상태의 사람에게만 적용되면 actions는 빈 배열이다. "
    "OPEN_REENTRY의 side는 long, short 또는 null만 사용한다. "
    "애매하면 actions는 빈 배열이다. JSON만 출력한다."
)


def update_config() -> None:
    path = ROOT / "configs" / "train.yaml"
    text = path.read_text(encoding="utf-8")
    text, count = re.subn(
        r"(?m)^(  system_prompt: >-\n)(    .*)$",
        lambda match: match.group(1) + "    " + NEW_PROMPT,
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not replace configs/train.yaml system_prompt")
    path.write_text(text, encoding="utf-8")


def update_live_prompt() -> None:
    path = ROOT / "live" / "live_bot.py"
    text = path.read_text(encoding="utf-8")
    replacement = (
        "SYSTEM_PROMPT = (\n"
        "    \"너는 BTCUSDT 리딩 메시지를 actions JSON으로 변환한다. \"\n"
        "    \"모든 수신자가 지금 즉시 실행할 명확한 거래 지시만 추출한다. \"\n"
        "    \"선택·권고·조건·미래 계획이거나 특정 상태의 사람에게만 적용되면 \"\n"
        "    \"actions는 빈 배열이다. OPEN_REENTRY의 side는 long, short 또는 null만 사용한다. \"\n"
        "    \"애매하면 actions는 빈 배열이다. JSON만 출력한다.\"\n"
        ")"
    )
    text, count = re.subn(
        r"SYSTEM_PROMPT = \(\n.*?\n\)\nALLOWED_ACTIONS",
        replacement + "\nALLOWED_ACTIONS",
        text,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise RuntimeError("Could not replace live SYSTEM_PROMPT")
    path.write_text(text, encoding="utf-8")


def update_chatml() -> None:
    paths = [
        ROOT / "data" / "base" / "train.jsonl",
        ROOT / "data" / "base" / "validation.jsonl",
        ROOT / "data" / "base" / "test.jsonl",
        ROOT / "data" / "regression" / "live_failures.jsonl",
    ]
    for path in paths:
        output: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            row["messages"][0]["content"] = NEW_PROMPT
            output.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        path.write_text(("\n".join(output) + "\n") if output else "", encoding="utf-8")


def update_contract_test() -> None:
    path = ROOT / "tests" / "test_system_prompt_contract.py"
    text = path.read_text(encoding="utf-8")
    old = '    assert "\'비트 자유롭게\'는 CLOSE_ALL이다." in expected\n'
    new = (
        '    assert "OPEN_REENTRY의 side는 long, short 또는 null만 사용한다." in expected\n'
        '    assert "\'비트 자유롭게\'는 CLOSE_ALL이다." not in expected\n'
    )
    if old not in text:
        raise RuntimeError("Could not find old channel-phrase assertion")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    update_config()
    update_live_prompt()
    update_chatml()
    update_contract_test()
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
