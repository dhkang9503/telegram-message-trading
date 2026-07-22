from pathlib import Path

from scripts.build_train_dataset import build_training_rows
from scripts.validate_dataset import validate_repository_data


ROOT = Path(__file__).resolve().parents[1]
# Base train contains only the user-identified real channel rows after synthetic cleanup.
EXPECTED_REAL_TRAIN_ROWS = 1515


def test_repository_data_is_valid():
    report = validate_repository_data(ROOT)
    assert report["splits"]["train"]["rows"] == EXPECTED_REAL_TRAIN_ROWS
    assert report["splits"]["validation"]["rows"] == 324
    assert report["splits"]["test"]["rows"] == 325


def test_labeled_feedback_is_appended_to_train():
    rows, report = build_training_rows(
        ROOT / "data/base/train.jsonl",
        ROOT / "data/feedback/mistakes_labeled.jsonl",
        (
            "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 "
            "변환하는 파서다. 메시지에 명시된 행동만 추출하고 추측하지 않는다. "
            "출력은 actions 배열을 가진 JSON 하나만 반환한다."
        ),
    )

    assert report["base_rows"] == EXPECTED_REAL_TRAIN_ROWS
    assert report["feedback_rows_seen"] > 0
    assert (
        report["feedback_rows_seen"]
        == report["feedback_rows_appended"]
        + report["feedback_rows_skipped_as_duplicates"]
    )
    assert report["combined_rows"] == (
        report["base_rows"] + report["feedback_rows_appended"]
    )
    assert len(rows) == report["combined_rows"]
