from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ALLOWED_ACTIONS = {
    "OPEN_LONG", "OPEN_SHORT", "OPEN_REENTRY", "ADD", "SET_STOP", "SET_TP",
    "CLOSE_HALF", "CLOSE_ADDS", "CLOSE_ALL", "CANCEL_ADD", "CANCEL_STOP",
}
EXPECTED_ROLES = ["system", "user", "assistant"]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: row must be an object")
            rows.append(row)
    return rows


def validate_actions(value: Any, location: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"actions"}:
        raise ValueError(f"{location}: assistant output must contain only 'actions'")
    actions = value["actions"]
    if not isinstance(actions, list):
        raise ValueError(f"{location}: actions must be a list")
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or set(action) != {"type", "price"}:
            raise ValueError(f"{location}: action[{index}] must contain type and price")
        if action["type"] not in ALLOWED_ACTIONS:
            raise ValueError(f"{location}: unsupported action type {action['type']!r}")
        if action["price"] is not None and not isinstance(action["price"], (int, float)):
            raise ValueError(f"{location}: price must be null or a number")
    return actions


def validate_message_text(message: str, location: str) -> None:
    if "<img>" in message:
        raise ValueError(f"{location}: literal <img> tags are not allowed in model input")


def validate_chatml_row(row: dict[str, Any], location: str) -> tuple[str, str, str]:
    if set(row) != {"messages"}:
        raise ValueError(f"{location}: top-level keys must be exactly ['messages']")
    messages = row["messages"]
    if not isinstance(messages, list) or len(messages) != 3:
        raise ValueError(f"{location}: messages must contain exactly three entries")
    roles = [message.get("role") if isinstance(message, dict) else None for message in messages]
    if roles != EXPECTED_ROLES:
        raise ValueError(f"{location}: role order must be {EXPECTED_ROLES}, got {roles}")

    contents: list[str] = []
    for index, message in enumerate(messages):
        if set(message) != {"role", "content"}:
            raise ValueError(f"{location}: message[{index}] must contain role and content")
        if not isinstance(message["content"], str) or not message["content"].strip():
            raise ValueError(f"{location}: message[{index}].content must be non-empty")
        contents.append(message["content"])

    validate_message_text(contents[1], f"{location}.user")

    try:
        assistant_obj = json.loads(contents[2])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{location}: assistant content is not JSON: {exc}") from exc
    validate_actions(assistant_obj, f"{location}.assistant")
    canonical = json.dumps(assistant_obj, ensure_ascii=False, separators=(",", ":"))
    return contents[0], contents[1], canonical


def validate_labeled_feedback(row: dict[str, Any], location: str) -> tuple[str, dict[str, Any]]:
    if "messages" in row:
        _, message, assistant = validate_chatml_row(row, location)
        return message, json.loads(assistant)

    source = row.get("source")
    label = row.get("label")
    if not isinstance(source, dict) or not isinstance(source.get("message"), str):
        raise ValueError(f"{location}: labeled feedback requires source.message")
    validate_message_text(source["message"], f"{location}.source.message")
    if not isinstance(label, dict):
        raise ValueError(f"{location}: labeled feedback requires label")
    if label.get("status") not in {"labeled", "approved"}:
        raise ValueError(f"{location}: label.status must be labeled or approved")
    correct_actions = label.get("correct_actions")
    obj = {"actions": correct_actions}
    validate_actions(obj, f"{location}.label")
    return source["message"], obj


def inspect_split(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_jsonl(path)
    systems = Counter()
    messages = Counter()
    labels_by_message: dict[str, set[str]] = defaultdict(set)

    for index, row in enumerate(rows):
        system, message, canonical = validate_chatml_row(row, f"{path}[{index}]")
        systems[system] += 1
        messages[message] += 1
        labels_by_message[message].add(canonical)

    conflicts = {
        message: sorted(labels)
        for message, labels in labels_by_message.items()
        if len(labels) > 1
    }
    if conflicts:
        sample = next(iter(conflicts.items()))
        raise ValueError(f"{path}: conflicting labels for the same message: {sample}")

    report = {
        "path": str(path),
        "rows": len(rows),
        "unique_messages": len(messages),
        "duplicate_rows_by_message": sum(count - 1 for count in messages.values()),
        "system_prompt_variants": len(systems),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return rows, report


def validate_repository_data(root: Path) -> dict[str, Any]:
    split_rows: dict[str, list[dict[str, Any]]] = {}
    report: dict[str, Any] = {"splits": {}, "overlap": {}, "feedback": {}}

    for split in ("train", "validation", "test"):
        path = root / "data" / "base" / f"{split}.jsonl"
        rows, split_report = inspect_split(path)
        split_rows[split] = rows
        report["splits"][split] = split_report

    message_sets = {
        split: {row["messages"][1]["content"] for row in rows}
        for split, rows in split_rows.items()
    }
    pairs = (("train", "validation"), ("train", "test"), ("validation", "test"))
    for left, right in pairs:
        report["overlap"][f"{left}_{right}"] = len(message_sets[left] & message_sets[right])

    pending_path = root / "data" / "feedback" / "mistakes_pending.jsonl"
    labeled_path = root / "data" / "feedback" / "mistakes_labeled.jsonl"
    regression_path = root / "data" / "regression" / "live_failures.jsonl"

    pending_rows = read_jsonl(pending_path)
    report["feedback"]["pending_rows"] = len(pending_rows)

    labeled_rows = read_jsonl(labeled_path)
    seen_feedback: dict[str, str] = {}
    for index, row in enumerate(labeled_rows):
        message, obj = validate_labeled_feedback(row, f"{labeled_path}[{index}]")
        label_text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        previous = seen_feedback.get(message)
        if previous is not None and previous != label_text:
            raise ValueError(f"{labeled_path}: conflicting labeled feedback for {message!r}")
        seen_feedback[message] = label_text
    report["feedback"]["labeled_rows"] = len(labeled_rows)

    regression_rows = read_jsonl(regression_path)
    for index, row in enumerate(regression_rows):
        validate_chatml_row(row, f"{regression_path}[{index}]")
    report["feedback"]["regression_rows"] = len(regression_rows)

    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--write-report", type=Path)
    args = parser.parse_args()

    report = validate_repository_data(args.root.resolve())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.write_report:
        args.write_report.parent.mkdir(parents=True, exist_ok=True)
        args.write_report.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
