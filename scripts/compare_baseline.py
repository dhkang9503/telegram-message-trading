from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def compare_metrics(metrics: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    gate = config["baseline"]["release_gate"]
    errors = metrics.get("errors", {})
    checks = {
        "json_valid_rate": {
            "actual": metrics.get("json_valid_rate", 0.0),
            "operator": ">=",
            "required": gate["min_json_valid_rate"],
        },
        "exact_match_rate": {
            "actual": metrics.get("exact_match_rate", 0.0),
            "operator": ">=",
            "required": gate["min_exact_match_rate"],
        },
        "LONG_TO_SHORT": {
            "actual": errors.get("LONG_TO_SHORT", 0),
            "operator": "<=",
            "required": gate["max_long_to_short"],
        },
        "SHORT_TO_LONG": {
            "actual": errors.get("SHORT_TO_LONG", 0),
            "operator": "<=",
            "required": gate["max_short_to_long"],
        },
        "OPEN_TO_CLOSE": {
            "actual": errors.get("OPEN_TO_CLOSE", 0),
            "operator": "<=",
            "required": gate["max_open_to_close"],
        },
        "CLOSE_TO_OPEN": {
            "actual": errors.get("CLOSE_TO_OPEN", 0),
            "operator": "<=",
            "required": gate["max_close_to_open"],
        },
    }
    for check in checks.values():
        if check["operator"] == ">=":
            check["passed"] = check["actual"] >= check["required"]
        else:
            check["passed"] = check["actual"] <= check["required"]
    return {
        "deploy_eligible": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "deployed_baseline": config["baseline"]["deployed_q8"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fail-if-ineligible", action="store_true")
    args = parser.parse_args()

    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    report = compare_metrics(metrics, config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_if_ineligible and not report["deploy_eligible"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
