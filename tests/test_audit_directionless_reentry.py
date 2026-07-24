from __future__ import annotations

import json
import re
from pathlib import Path


def _message_and_actions(row: dict) -> tuple[str, object]:
    if "messages" in row:
        return row["messages"][1]["content"], json.loads(row["messages"][2]["content"])["actions"]
    return row["source"]["message"], row["label"]["correct_actions"]


def test_audit_directionless_reentry_candidates() -> None:
    paths = [
        Path("data/base/train.jsonl"),
        Path("data/base/validation.jsonl"),
        Path("data/base/test.jsonl"),
        Path("data/feedback/mistakes_labeled.jsonl"),
    ]
    candidates = []
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            row = json.loads(line)
            message, actions = _message_and_actions(row)
            compact = re.sub(r"\s+", "", message)
            if "재진입" in compact and "롱" not in compact and "숏" not in compact:
                candidates.append(
                    {
                        "path": str(path),
                        "line": line_number,
                        "message": message,
                        "actions": actions,
                    }
                )
    raise AssertionError(json.dumps(candidates, ensure_ascii=False, indent=2))
