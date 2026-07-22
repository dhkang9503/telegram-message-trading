from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

# When this file is executed as `python scripts/evaluate_cli.py`, Python puts the
# scripts directory (not the repository root) on sys.path. Add the repository
# root explicitly so absolute imports such as `scripts.evaluate` work both as a
# direct script and as a module.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.evaluate import evaluate_gguf, read_jsonl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--server-bin", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--quant-name", default="Q8_0")
    parser.add_argument("--gpu-layers", type=int, default=0)
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    ev = config["evaluation"]
    metrics = evaluate_gguf(
        quant_name=args.quant_name,
        model_path=args.model,
        rows=read_jsonl(args.data),
        output_dir=args.output_dir,
        system_prompt=config["model"]["system_prompt"],
        server_bin=args.server_bin,
        n_ctx=int(ev["n_ctx"]),
        n_predict=int(ev["n_predict"]),
        temperature=float(ev["temperature"]),
        top_k=int(ev["top_k"]),
        top_p=float(ev["top_p"]),
        repeat_penalty=float(ev["repeat_penalty"]),
        server_start_timeout_seconds=float(ev["server_start_timeout_seconds"]),
        gpu_layers=args.gpu_layers,
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
