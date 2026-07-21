from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_dataset import read_jsonl, validate_chatml_row, validate_labeled_feedback


def compact_actions(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def make_chatml(system_prompt: str, message: str, actions: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
            {"role": "assistant", "content": compact_actions(actions)},
        ]
    }


def build_training_rows(
    base_train_path: Path,
    labeled_feedback_path: Path,
    system_prompt: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base_rows = read_jsonl(base_train_path)
    labels_by_message: dict[str, str] = {}

    for index, row in enumerate(base_rows):
        _, message, canonical = validate_chatml_row(row, f"{base_train_path}[{index}]")
        previous = labels_by_message.get(message)
        if previous is not None and previous != canonical:
            raise ValueError(f"base train has conflicting labels for {message!r}")
        labels_by_message[message] = canonical

    feedback_rows = read_jsonl(labeled_feedback_path)
    appended: list[dict[str, Any]] = []
    skipped_duplicates = 0

    for index, row in enumerate(feedback_rows):
        message, actions_obj = validate_labeled_feedback(
            row, f"{labeled_feedback_path}[{index}]"
        )
        canonical = compact_actions(actions_obj)
        previous = labels_by_message.get(message)
        if previous is not None:
            if previous != canonical:
                raise ValueError(
                    f"feedback conflicts with existing label for {message!r}: "
                    f"existing={previous}, feedback={canonical}"
                )
            skipped_duplicates += 1
            continue

        appended.append(make_chatml(system_prompt, message, actions_obj))
        labels_by_message[message] = canonical

    combined = base_rows + appended
    report = {
        "base_rows": len(base_rows),
        "feedback_rows_seen": len(feedback_rows),
        "feedback_rows_appended": len(appended),
        "feedback_rows_skipped_as_duplicates": skipped_duplicates,
        "combined_rows": len(combined),
    }
    return combined, report


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-train", type=Path, required=True)
    parser.add_argument("--labeled-feedback", type=Path, required=True)
    parser.add_argument("--system-prompt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    rows, report = build_training_rows(
        args.base_train, args.labeled_feedback, args.system_prompt
    )
    write_jsonl(args.output, rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
