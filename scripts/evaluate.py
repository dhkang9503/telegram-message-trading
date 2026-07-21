from __future__ import annotations

import contextlib
import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

import httpx

OPEN_TYPES = {"OPEN_LONG", "OPEN_SHORT"}
CLOSE_TYPES = {"CLOSE_HALF", "CLOSE_ADDS", "CLOSE_ALL"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def build_prompt(system_prompt: str, message: str) -> str:
    return (
        "<|im_start|>system\n" + system_prompt + "<|im_end|>\n"
        "<|im_start|>user\n" + message + "<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


def normalize_result(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict) or not isinstance(obj.get("actions"), list):
        return {"actions": []}
    return {
        "actions": [
            {"type": action.get("type"), "price": action.get("price")}
            for action in obj["actions"]
            if isinstance(action, dict)
        ]
    }


def extract_json(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.replace("[end of text]", "").strip()
    match = re.search(r'\{\s*"actions"\s*:\s*\[.*?\]\s*\}', cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError(f"actions JSON not found: {cleaned!r}")
    return json.loads(match.group(0))


def _percentile(sorted_values: list[float], value: float) -> float | None:
    if not sorted_values:
        return None
    return sorted_values[int((len(sorted_values) - 1) * value)]


@contextlib.contextmanager
def llama_server(
    server_bin: Path,
    model_path: Path,
    port: int,
    n_ctx: int,
    start_timeout_seconds: float,
    log_path: Path,
    gpu_layers: int = 0,
) -> Iterator[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    command = [
        str(server_bin), "-m", str(model_path), "--host", "127.0.0.1",
        "--port", str(port), "-c", str(n_ctx), "-ngl", str(gpu_layers),
    ]
    process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, text=True)
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + start_timeout_seconds
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"llama-server exited with {process.returncode}; see {log_path}")
            try:
                if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise TimeoutError(f"llama-server did not become healthy; see {log_path}")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log_file.close()


def evaluate_gguf(
    *, quant_name: str, model_path: Path, rows: list[dict[str, Any]],
    output_dir: Path, system_prompt: str, server_bin: Path, port: int = 18080,
    n_ctx: int = 512, n_predict: int = 48, temperature: float = 0.0,
    top_k: int = 1, top_p: float = 1.0, repeat_penalty: float = 1.0,
    server_start_timeout_seconds: float = 120.0, gpu_layers: int = 0,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    load_started = time.perf_counter()
    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    exact_count = type_count = price_count = json_valid_count = 0
    error_counts: Counter[str] = Counter()

    with llama_server(
        server_bin, model_path, port, n_ctx, start_timeout_seconds=server_start_timeout_seconds,
        log_path=output_dir / "llama-server.log", gpu_layers=gpu_layers,
    ) as base_url:
        model_load_seconds = time.perf_counter() - load_started
        with httpx.Client(timeout=120.0) as client:
            for row in rows:
                message = row["messages"][1]["content"]
                expected = normalize_result(json.loads(row["messages"][2]["content"]))
                payload = {
                    "prompt": build_prompt(system_prompt, message), "n_predict": n_predict,
                    "temperature": temperature, "top_k": top_k, "top_p": top_p,
                    "repeat_penalty": repeat_penalty,
                    "stop": ["<|im_end|>", "<|endoftext|>"], "stream": False,
                }
                started = time.perf_counter()
                response = client.post(f"{base_url}/completion", json=payload)
                response.raise_for_status()
                latency = time.perf_counter() - started
                latencies.append(latency)
                raw_output = str(response.json().get("content", "")).strip()
                result: dict[str, Any] = {
                    "message": message, "expected": expected, "raw_output": raw_output,
                    "latency_seconds": latency, "json_valid": False, "exact_match": False,
                }
                try:
                    predicted = normalize_result(extract_json(raw_output))
                    result.update({"json_valid": True, "predicted": predicted})
                    json_valid_count += 1
                    expected_types = [a["type"] for a in expected["actions"]]
                    predicted_types = [a["type"] for a in predicted["actions"]]
                    expected_prices = [a["price"] for a in expected["actions"]]
                    predicted_prices = [a["price"] for a in predicted["actions"]]
                    if predicted == expected:
                        exact_count += 1
                        result["exact_match"] = True
                    if predicted_types == expected_types:
                        type_count += 1
                    if predicted_prices == expected_prices:
                        price_count += 1
                    expected_set, predicted_set = set(expected_types), set(predicted_types)
                    if "OPEN_LONG" in expected_set and "OPEN_SHORT" in predicted_set:
                        error_counts["LONG_TO_SHORT"] += 1
                    if "OPEN_SHORT" in expected_set and "OPEN_LONG" in predicted_set:
                        error_counts["SHORT_TO_LONG"] += 1
                    if expected_set & OPEN_TYPES and predicted_set & CLOSE_TYPES:
                        error_counts["OPEN_TO_CLOSE"] += 1
                    if expected_set & CLOSE_TYPES and predicted_set & OPEN_TYPES:
                        error_counts["CLOSE_TO_OPEN"] += 1
                    if not expected_types and predicted_types:
                        error_counts["FALSE_ACTION"] += 1
                    if expected_types and not predicted_types:
                        error_counts["MISSED_ACTION"] += 1
                except Exception as exc:
                    result["parse_error"] = f"{type(exc).__name__}: {exc}"
                    error_counts["INVALID_JSON"] += 1
                results.append(result)

    total = len(results)
    sorted_latencies = sorted(latencies)
    metrics = {
        "quantization": quant_name, "model_path": str(model_path),
        "model_size_mb": model_path.stat().st_size / 1024 / 1024, "test_rows": total,
        "model_load_seconds": model_load_seconds,
        "json_valid_rate": json_valid_count / total if total else 0.0,
        "exact_match_rate": exact_count / total if total else 0.0,
        "action_type_exact_rate": type_count / total if total else 0.0,
        "price_exact_rate": price_count / total if total else 0.0,
        "latency_seconds": {
            "mean": sum(latencies) / len(latencies) if latencies else None,
            "p50": _percentile(sorted_latencies, 0.50),
            "p95": _percentile(sorted_latencies, 0.95), "max": max(latencies) if latencies else None,
        },
        "errors": dict(error_counts),
    }
    (output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "predictions.jsonl").open("w", encoding="utf-8") as file:
        for result in results:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
    with (output_dir / "wrong_predictions.jsonl").open("w", encoding="utf-8") as file:
        for result in results:
            if not result["exact_match"]:
                file.write(json.dumps(result, ensure_ascii=False) + "\n")
    return metrics
