from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_train_dataset import build_evaluation_rows, build_training_rows

SYSTEM_PROMPT = "test system prompt"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def chatml(message: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
            {"role": "assistant", "content": '{"actions":[]}'},
        ]
    }


def feedback(message: str) -> dict:
    return {
        "source": {"message": message},
        "label": {"status": "labeled", "correct_actions": []},
    }


def test_training_repeats_only_deduplicated_feedback(tmp_path: Path) -> None:
    base_path = tmp_path / "train.jsonl"
    feedback_path = tmp_path / "mistakes.jsonl"
    write_jsonl(base_path, [chatml("base")])
    write_jsonl(
        feedback_path,
        [feedback("mistake-a"), feedback("mistake-a"), feedback("mistake-b")],
    )

    rows, report = build_training_rows(
        base_path,
        feedback_path,
        SYSTEM_PROMPT,
        feedback_repeat=4,
    )

    messages = [row["messages"][1]["content"] for row in rows]
    assert messages == [
        "base",
        "mistake-a",
        "mistake-b",
        "mistake-a",
        "mistake-b",
        "mistake-a",
        "mistake-b",
        "mistake-a",
        "mistake-b",
    ]
    assert report["base_rows"] == 1
    assert report["feedback_rows_seen"] == 3
    assert report["feedback_unique_rows"] == 2
    assert report["feedback_duplicate_rows_skipped"] == 1
    assert report["feedback_repeat"] == 4
    assert report["feedback_unique_rows_appended"] == 2
    assert report["feedback_rows_appended"] == 8
    assert report["combined_rows"] == 9


def test_evaluation_keeps_each_unique_feedback_once(tmp_path: Path) -> None:
    base_path = tmp_path / "test.jsonl"
    feedback_path = tmp_path / "mistakes.jsonl"
    write_jsonl(base_path, [chatml("base")])
    write_jsonl(
        feedback_path,
        [feedback("mistake-a"), feedback("mistake-a"), feedback("mistake-b")],
    )

    combined, mistake_rows, report = build_evaluation_rows(
        base_path,
        feedback_path,
        SYSTEM_PROMPT,
    )

    assert [row["messages"][1]["content"] for row in combined] == [
        "base",
        "mistake-a",
        "mistake-b",
    ]
    assert len(mistake_rows) == 2
    assert report["feedback_rows_added_to_evaluation"] == 2
    assert report["combined_evaluation_rows"] == 3


@pytest.mark.parametrize("value", [0, -1])
def test_training_rejects_non_positive_repeat(tmp_path: Path, value: int) -> None:
    base_path = tmp_path / "train.jsonl"
    feedback_path = tmp_path / "mistakes.jsonl"
    write_jsonl(base_path, [chatml("base")])
    write_jsonl(feedback_path, [feedback("mistake")])

    with pytest.raises(ValueError, match="at least 1"):
        build_training_rows(
            base_path,
            feedback_path,
            SYSTEM_PROMPT,
            feedback_repeat=value,
        )
