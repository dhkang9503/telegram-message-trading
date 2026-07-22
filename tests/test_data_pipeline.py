from pathlib import Path

from scripts.build_train_dataset import build_evaluation_rows, build_training_rows
from scripts.validate_dataset import validate_repository_data


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = (
    "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 "
    "변환하는 파서다. 메시지에 명시된 행동만 추출하고 추측하지 않는다. "
    "출력은 actions 배열을 가진 JSON 하나만 반환한다."
)
# Operational training intentionally includes every real row from all three stored splits.
EXPECTED_TRAIN_ROWS = 2164


def test_repository_data_is_valid():
    report = validate_repository_data(ROOT)
    assert report["splits"]["train"]["rows"] == EXPECTED_TRAIN_ROWS
    assert report["splits"]["validation"]["rows"] == 324
    assert report["splits"]["test"]["rows"] == 325


def test_labeled_feedback_is_explicitly_appended_to_train():
    rows, report = build_training_rows(
        ROOT / "data/base/train.jsonl",
        ROOT / "data/feedback/mistakes_labeled.jsonl",
        SYSTEM_PROMPT,
    )

    assert report["base_rows"] == EXPECTED_TRAIN_ROWS
    assert report["feedback_unique_rows"] > 0
    assert report["feedback_rows_appended"] == report["feedback_unique_rows"]
    assert report["combined_rows"] == (
        report["base_rows"] + report["feedback_rows_appended"]
    )
    assert len(rows) == report["combined_rows"]


def test_validation_and_test_are_evaluated_with_all_mistakes():
    feedback_path = ROOT / "data/feedback/mistakes_labeled.jsonl"

    validation_rows, validation_mistakes, validation_report = build_evaluation_rows(
        ROOT / "data/base/validation.jsonl",
        feedback_path,
        SYSTEM_PROMPT,
    )
    test_rows, test_mistakes, test_report = build_evaluation_rows(
        ROOT / "data/base/test.jsonl",
        feedback_path,
        SYSTEM_PROMPT,
    )

    mistake_count = validation_report["feedback_unique_rows"]
    assert mistake_count > 0
    assert len(validation_mistakes) == mistake_count
    assert len(test_mistakes) == mistake_count
    assert len(validation_rows) == 324 + mistake_count
    assert len(test_rows) == 325 + mistake_count
    assert validation_report["combined_evaluation_rows"] == len(validation_rows)
    assert test_report["combined_evaluation_rows"] == len(test_rows)
