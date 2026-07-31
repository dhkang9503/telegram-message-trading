from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_dataset import read_jsonl, validate_chatml_row, validate_labeled_feedback


LABEL_OVERRIDE_RELATIVE_PATH = Path("data/label_overrides/partial_add.jsonl")


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


def _find_label_override_path(reference_path: Path) -> Path | None:
    resolved = reference_path.resolve()
    for parent in (resolved.parent, *resolved.parents):
        candidate = parent / LABEL_OVERRIDE_RELATIVE_PATH
        if candidate.exists():
            return candidate
    return None


def _load_label_overrides(reference_path: Path) -> tuple[dict[str, str], Path | None]:
    override_path = _find_label_override_path(reference_path)
    if override_path is None:
        return {}, None

    labels_by_message: dict[str, str] = {}
    for index, row in enumerate(read_jsonl(override_path)):
        if not isinstance(row, dict) or set(row) != {"message", "actions"}:
            raise ValueError(
                f"{override_path}[{index}] must contain exactly message and actions"
            )
        message = row["message"]
        actions = row["actions"]
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"{override_path}[{index}].message must be non-empty")
        if not isinstance(actions, list):
            raise ValueError(f"{override_path}[{index}].actions must be a list")

        # Reuse the repository's ChatML validator so override actions obey the
        # same action/price schema as stored training rows.
        _, _, canonical = validate_chatml_row(
            make_chatml("override-validation", message, {"actions": actions}),
            f"{override_path}[{index}]",
        )
        previous = labels_by_message.get(message)
        if previous is not None and previous != canonical:
            raise ValueError(f"{override_path} has conflicting overrides for {message!r}")
        labels_by_message[message] = canonical
    return labels_by_message, override_path


def apply_label_overrides(
    source_path: Path,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    overrides, override_path = _load_label_overrides(source_path)
    matched_messages: set[str] = set()
    changed_rows = 0

    for index, row in enumerate(rows):
        _, message, canonical = validate_chatml_row(row, f"{source_path}[{index}]")
        replacement = overrides.get(message)
        if replacement is None:
            continue
        matched_messages.add(message)
        if replacement != canonical:
            row["messages"][2]["content"] = replacement
            changed_rows += 1

    return rows, {
        "label_override_path": str(override_path) if override_path else None,
        "label_overrides_defined": len(overrides),
        "label_override_messages_matched": len(matched_messages),
        "label_override_rows_changed": changed_rows,
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base_rows, override_report = apply_label_overrides(
        base_train_path, read_jsonl(base_train_path)
    )
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

    # Mistakes are intentionally appended even when the same labeled message is
    # already present in base train. They are hard examples and should receive
    # explicit weight in every corrective fine-tuning cycle.
    combined = base_rows + feedback_rows
    report = {
        "base_rows": len(base_rows),
        **override_report,
        **feedback_report,
        "feedback_rows_appended": len(feedback_rows),
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
    base_rows, override_report = apply_label_overrides(
        base_evaluation_path, read_jsonl(base_evaluation_path)
    )
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
        **override_report,
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
