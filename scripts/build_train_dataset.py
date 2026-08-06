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


def build_feedback_rows(
    labeled_feedback_path: Path,
    system_prompt: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_rows = read_jsonl(labeled_feedback_path)
    rows: list[dict[str, Any]] = []
    labels_by_message: dict[str, str] = {}
    duplicate_rows = 0

    for index, row in enumerate(source_rows):
        message, actions_obj = validate_labeled_feedback(
            row, f"{labeled_feedback_path}[{index}]"
        )
        canonical = compact_actions(actions_obj)
        previous = labels_by_message.get(message)
        if previous is not None:
            if previous != canonical:
                raise ValueError(
                    f"feedback has conflicting labels for {message!r}: "
                    f"existing={previous}, feedback={canonical}"
                )
            duplicate_rows += 1
            continue
        rows.append(make_chatml(system_prompt, message, actions_obj))
        labels_by_message[message] = canonical

    return rows, {
        "feedback_rows_seen": len(source_rows),
        "feedback_unique_rows": len(rows),
        "feedback_duplicate_rows_skipped": duplicate_rows,
    }


def _base_labels(path: Path, rows: list[dict[str, Any]]) -> dict[str, str]:
    labels_by_message: dict[str, str] = {}
    for index, row in enumerate(rows):
        _, message, canonical = validate_chatml_row(row, f"{path}[{index}]")
        previous = labels_by_message.get(message)
        if previous is not None and previous != canonical:
            raise ValueError(f"{path} has conflicting labels for {message!r}")
        labels_by_message[message] = canonical
    return labels_by_message


def build_training_rows(
    base_train_path: Path,
    labeled_feedback_path: Path,
    system_prompt: str,
    feedback_repeat: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(feedback_repeat, bool) or not isinstance(feedback_repeat, int):
        raise TypeError("feedback_repeat must be an integer")
    if feedback_repeat < 1:
        raise ValueError("feedback_repeat must be at least 1")

    base_rows = read_jsonl(base_train_path)
    base_labels = _base_labels(base_train_path, base_rows)
    feedback_rows, feedback_report = build_feedback_rows(
        labeled_feedback_path, system_prompt
    )

    matching_base = 0
    for index, row in enumerate(feedback_rows):
        _, message, canonical = validate_chatml_row(
            row, f"{labeled_feedback_path}.converted[{index}]"
        )
        previous = base_labels.get(message)
        if previous is not None:
            if previous != canonical:
                raise ValueError(
                    f"feedback conflicts with existing train label for {message!r}: "
                    f"existing={previous}, feedback={canonical}"
                )
            matching_base += 1

    # Deduplicate labeled mistakes first, then repeat only those hard examples.
    # Base training rows remain unchanged, while validation/test builders still
    # include each unique mistake exactly once.
    repeated_feedback_rows = [
        row for _ in range(feedback_repeat) for row in feedback_rows
    ]
    combined = base_rows + repeated_feedback_rows
    report = {
        "base_rows": len(base_rows),
        **feedback_report,
        "feedback_repeat": feedback_repeat,
        "feedback_rows_appended": len(repeated_feedback_rows),
        "feedback_unique_rows_appended": len(feedback_rows),
        "feedback_rows_already_in_base": matching_base,
        "feedback_rows_skipped_as_duplicates": feedback_report[
            "feedback_duplicate_rows_skipped"
        ],
        "combined_rows": len(combined),
    }
    return combined, report


def build_evaluation_rows(
    base_evaluation_path: Path,
    labeled_feedback_path: Path,
    system_prompt: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    base_rows = read_jsonl(base_evaluation_path)
    base_labels = _base_labels(base_evaluation_path, base_rows)
    feedback_rows, feedback_report = build_feedback_rows(
        labeled_feedback_path, system_prompt
    )

    matching_base = 0
    for index, row in enumerate(feedback_rows):
        _, message, canonical = validate_chatml_row(
            row, f"{labeled_feedback_path}.converted[{index}]"
        )
        previous = base_labels.get(message)
        if previous is not None:
            if previous != canonical:
                raise ValueError(
                    f"feedback conflicts with evaluation label for {message!r}: "
                    f"existing={previous}, feedback={canonical}"
                )
            matching_base += 1

    # Evaluation intentionally includes the feedback rows again, even if they
    # overlap a stored split. This is a regression/recovery check: after tuning,
    # every previously observed live mistake must be exercised explicitly.
    combined = base_rows + feedback_rows
    report = {
        "base_evaluation_rows": len(base_rows),
        **feedback_report,
        "feedback_rows_added_to_evaluation": len(feedback_rows),
        "feedback_rows_already_in_base_evaluation": matching_base,
        "combined_evaluation_rows": len(combined),
    }
    return combined, feedback_rows, report


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
    parser.add_argument("--feedback-repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    rows, report = build_training_rows(
        args.base_train,
        args.labeled_feedback,
        args.system_prompt,
        feedback_repeat=args.feedback_repeat,
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
