from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_train_dataset import build_evaluation_rows, write_jsonl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-evaluation", type=Path, required=True)
    parser.add_argument("--labeled-feedback", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--combined-output", type=Path, required=True)
    parser.add_argument("--feedback-output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    combined, feedback_rows, report = build_evaluation_rows(
        args.base_evaluation,
        args.labeled_feedback,
        config["model"]["system_prompt"],
    )
    write_jsonl(args.combined_output, combined)
    write_jsonl(args.feedback_output, feedback_rows)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
