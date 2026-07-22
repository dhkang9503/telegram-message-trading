from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Support both `python scripts/finalize_manifest.py` and
# `python -m scripts.finalize_manifest` from the repository root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.compare_baseline import compare_metrics


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--training-manifest", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--remote-model-path", required=True)
    args = parser.parse_args()

    manifest = json.loads(args.training_manifest.read_text(encoding="utf-8"))
    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    gate = compare_metrics(metrics, config)
    manifest.update(
        {
            "stage": "complete",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "evaluation": {
                **manifest.get("evaluation", {}),
                "original_test": {"Q8_0": metrics},
                "release_gate": gate,
            },
            "artifacts": {
                **manifest.get("artifacts", {}),
                "Q8_0": {
                    "path": args.remote_model_path,
                    "size_bytes": args.model.stat().st_size,
                    "sha256": sha256_file(args.model),
                },
            },
            "deploy_eligible": bool(gate["deploy_eligible"]),
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
