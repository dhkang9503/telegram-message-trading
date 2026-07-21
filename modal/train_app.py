from __future__ import annotations

import gc
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import modal

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_LOCAL = REPO_ROOT / "configs"
DATA_LOCAL = REPO_ROOT / "data"
SCRIPTS_LOCAL = REPO_ROOT / "scripts"

LLAMA_CPP_COMMIT = "91d2fc387529940230555abd297a8b5e99737d3f"
ARTIFACT_VOLUME_NAME = "telegram-parser-artifacts"
HF_CACHE_VOLUME_NAME = "telegram-parser-hf-cache"

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.1-devel-ubuntu22.04",
        add_python="3.12",
    )
    .apt_install(
        "build-essential",
        "cmake",
        "curl",
        "git",
        "libcurl4-openssl-dev",
        "pkg-config",
    )
    .pip_install(
        "torch==2.11.0",
        index_url="https://download.pytorch.org/whl/cu128",
    )
    .pip_install(
        "transformers==4.51.3",
        "tokenizers==0.21.1",
        "datasets==3.5.0",
        "peft==0.15.2",
        "accelerate==1.6.0",
        "huggingface_hub==0.30.2",
        "safetensors>=0.5.3",
        "sentencepiece",
        "protobuf",
        "psutil",
        "pyyaml",
        "httpx",
    )
    .run_commands(
        "git clone https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp",
        f"git -C /opt/llama.cpp checkout {LLAMA_CPP_COMMIT}",
        "cmake -S /opt/llama.cpp -B /opt/llama.cpp/build "
        "-DCMAKE_BUILD_TYPE=Release "
        "-DGGML_CUDA=ON "
        "-DLLAMA_BUILD_TESTS=OFF "
        "-DLLAMA_BUILD_EXAMPLES=ON "
        "-DLLAMA_BUILD_SERVER=ON",
        "cmake --build /opt/llama.cpp/build "
        "--target llama-quantize llama-server "
        "--parallel 8",
    )
    .env(
        {
            "HF_HOME": "/cache/huggingface",
            "HF_HUB_CACHE": "/cache/huggingface/hub",
            "TRANSFORMERS_CACHE": "/cache/huggingface/hub",
            "HF_DATASETS_CACHE": "/cache/datasets",
            "TORCH_HOME": "/cache/torch",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONPATH": "/repo",
        }
    )
    .add_local_dir(str(CONFIG_LOCAL), "/repo/configs")
    .add_local_dir(str(DATA_LOCAL), "/repo/data")
    .add_local_dir(str(SCRIPTS_LOCAL), "/repo/scripts")
)

app = modal.App("telegram-parser-training", image=image)
artifact_volume = modal.Volume.from_name(ARTIFACT_VOLUME_NAME, create_if_missing=True)
hf_cache_volume = modal.Volume.from_name(HF_CACHE_VOLUME_NAME, create_if_missing=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@app.function(
    gpu="A100-40GB",
    cpu=8.0,
    memory=32768,
    ephemeral_disk=40960,
    timeout=4 * 60 * 60,
    volumes={
        "/artifacts": artifact_volume,
        "/cache": hf_cache_volume,
    },
)
def train_and_evaluate(run_id: str, git_sha: str, quant_types_csv: str) -> str:
    import numpy as np
    import torch
    import yaml
    from datasets import Dataset, DatasetDict
    from huggingface_hub import snapshot_download
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForSeq2Seq,
        Trainer,
        TrainingArguments,
    )
    from transformers.trainer_utils import get_last_checkpoint

    sys.path.insert(0, "/repo")
    from scripts.build_train_dataset import build_training_rows, write_jsonl
    from scripts.compare_baseline import compare_metrics
    from scripts.evaluate import evaluate_gguf, read_jsonl
    from scripts.validate_dataset import validate_repository_data

    config_path = Path("/repo/configs/train.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    system_prompt = config["model"]["system_prompt"]
    quant_types = [
        item.strip().upper()
        for item in quant_types_csv.split(",")
        if item.strip()
    ]
    allowed_quants = {"Q4_K_M", "Q5_K_M", "Q8_0"}
    if not quant_types or not set(quant_types) <= allowed_quants:
        raise ValueError(f"quant_types must be a subset of {sorted(allowed_quants)}")

    run_root = Path("/artifacts/runs") / run_id
    manifest_path = run_root / "manifest.json"
    if manifest_path.exists():
        artifact_volume.reload()
        if manifest_path.exists():
            return manifest_path.read_text(encoding="utf-8")

    run_root.mkdir(parents=True, exist_ok=True)
    checkpoints_dir = run_root / "checkpoints"
    adapter_dir = run_root / "lora_adapter"
    merged_dir = run_root / "merged_model"
    evaluation_dir = run_root / "evaluation"
    gguf_dir = run_root / "gguf"
    dataset_dir = run_root / "dataset"
    for path in [
        checkpoints_dir, adapter_dir, merged_dir,
        evaluation_dir, gguf_dir, dataset_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)

    seed = int(config["reproducibility"]["seed"])
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    validation_report = validate_repository_data(Path("/repo"))
    (dataset_dir / "repository_validation.json").write_text(
        json.dumps(validation_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    combined_rows, build_report = build_training_rows(
        Path("/repo/data/base/train.jsonl"),
        Path("/repo/data/feedback/mistakes_labeled.jsonl"),
        system_prompt,
    )
    combined_train_path = dataset_dir / "train.combined.jsonl"
    write_jsonl(combined_train_path, combined_rows)
    (dataset_dir / "build_report.json").write_text(
        json.dumps(build_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copy2("/repo/data/base/validation.jsonl", dataset_dir / "validation.jsonl")
    shutil.copy2("/repo/data/base/test.jsonl", dataset_dir / "test.jsonl")
    shutil.copy2("/repo/data/regression/live_failures.jsonl", dataset_dir / "live_failures.jsonl")
    shutil.copy2(config_path, run_root / "train.yaml")

    model_id = config["model"]["base_model_id"]
    base_model_dir = Path("/cache/models") / model_id.replace("/", "--")
    if not (base_model_dir / "model.safetensors").exists():
        base_model_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=model_id,
            local_dir=str(base_model_dir),
            max_workers=8,
        )
        hf_cache_volume.commit()

    tokenizer = AutoTokenizer.from_pretrained(str(base_model_dir), use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    max_length = int(config["model"]["max_length"])

    raw_rows = {
        "train": combined_rows,
        "validation": read_jsonl(Path("/repo/data/base/validation.jsonl")),
        "test": read_jsonl(Path("/repo/data/base/test.jsonl")),
    }

    def tokenize_row(example: dict[str, Any]) -> dict[str, Any]:
        messages = example["messages"]
        prompt_ids = tokenizer.apply_chat_template(
            messages[:2],
            tokenize=True,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        completion_ids = tokenizer(
            messages[2]["content"] + tokenizer.eos_token,
            add_special_tokens=False,
        )["input_ids"]
        input_ids = prompt_ids + completion_ids
        labels = [-100] * len(prompt_ids) + completion_ids.copy()
        attention_mask = [1] * len(input_ids)

        if len(input_ids) > max_length:
            overflow = len(input_ids) - max_length
            prompt_cut = min(overflow, len(prompt_ids))
            input_ids = input_ids[prompt_cut:]
            labels = labels[prompt_cut:]
            attention_mask = attention_mask[prompt_cut:]
        if len(input_ids) > max_length:
            input_ids = input_ids[-max_length:]
            labels = labels[-max_length:]
            attention_mask = attention_mask[-max_length:]
        if not any(label != -100 for label in labels):
            raise ValueError("All assistant tokens were truncated")
        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "labels": labels,
            "length": len(input_ids),
        }

    dataset = DatasetDict(
        {split: Dataset.from_list(rows) for split, rows in raw_rows.items()}
    )
    tokenized = dataset.map(
        tokenize_row,
        remove_columns=["messages"],
        num_proc=1,
        desc="tokenize",
    )

    use_bf16 = torch.cuda.is_bf16_supported()
    model_dtype = torch.bfloat16 if use_bf16 else torch.float16
    base_model = AutoModelForCausalLM.from_pretrained(
        str(base_model_dir),
        torch_dtype=model_dtype,
        device_map={"": 0},
    )
    base_model.config.use_cache = False

    lora = config["training"]["lora"]
    lora_config = LoraConfig(
        r=int(lora["r"]),
        lora_alpha=int(lora["alpha"]),
        lora_dropout=float(lora["dropout"]),
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=list(lora["target_modules"]),
    )
    model = get_peft_model(base_model, lora_config)
    model.enable_input_require_grads()

    data_collator = DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        padding=True,
        label_pad_token_id=-100,
        return_tensors="pt",
    )
    training = config["training"]
    training_args = TrainingArguments(
        output_dir=str(checkpoints_dir),
        overwrite_output_dir=False,
        num_train_epochs=float(training["num_epochs"]),
        learning_rate=float(training["learning_rate"]),
        weight_decay=float(training["weight_decay"]),
        warmup_ratio=float(training["warmup_ratio"]),
        lr_scheduler_type=str(training["lr_scheduler_type"]),
        max_grad_norm=float(training["max_grad_norm"]),
        per_device_train_batch_size=int(training["train_batch_size"]),
        per_device_eval_batch_size=int(training["eval_batch_size"]),
        gradient_accumulation_steps=int(training["gradient_accumulation_steps"]),
        fp16=not use_bf16,
        bf16=use_bf16,
        gradient_checkpointing=True,
        eval_strategy="epoch",
        save_strategy="steps",
        save_steps=int(training["save_steps"]),
        save_total_limit=int(training["save_total_limit"]),
        logging_strategy="steps",
        logging_steps=int(training["logging_steps"]),
        report_to="none",
        seed=seed,
        data_seed=int(config["reproducibility"]["data_seed"]),
        group_by_length=True,
        length_column_name="length",
        dataloader_num_workers=0,
        remove_unused_columns=True,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized["train"],
        eval_dataset=tokenized["validation"],
        data_collator=data_collator,
    )
    last_checkpoint = get_last_checkpoint(str(checkpoints_dir))
    train_result = trainer.train(resume_from_checkpoint=last_checkpoint)
    trainer.save_model(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    trainer.save_metrics("train", train_result.metrics)
    trainer.save_state()

    validation_metrics = trainer.evaluate(tokenized["validation"])
    (evaluation_dir / "validation_metrics.json").write_text(
        json.dumps(validation_metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    model.config.use_cache = True
    merged_model = model.merge_and_unload()
    merged_model.save_pretrained(str(merged_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(merged_dir))
    try:
        merged_model.generation_config.save_pretrained(str(merged_dir))
    except Exception:
        pass

    tokenizer_config_path = merged_dir / "tokenizer_config.json"
    tokenizer_config = json.loads(tokenizer_config_path.read_text(encoding="utf-8"))
    if isinstance(tokenizer_config.get("extra_special_tokens"), list):
        tokenizer_config.pop("extra_special_tokens")
        tokenizer_config_path.write_text(
            json.dumps(tokenizer_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    AutoTokenizer.from_pretrained(str(merged_dir), use_fast=True)

    del merged_model, model, base_model, trainer
    gc.collect()
    torch.cuda.empty_cache()

    llama_root = Path("/opt/llama.cpp")
    converter = llama_root / "convert_hf_to_gguf.py"
    quantize_bin = llama_root / "build/bin/llama-quantize"
    server_bin = llama_root / "build/bin/llama-server"
    f16_path = gguf_dir / f"{run_id}-F16.gguf"
    subprocess.run(
        [
            sys.executable, str(converter), str(merged_dir),
            "--outfile", str(f16_path), "--outtype", "f16",
        ],
        cwd=str(llama_root),
        check=True,
    )

    quant_paths: dict[str, Path] = {}
    for quant_type in quant_types:
        output_path = gguf_dir / f"{run_id}-{quant_type}.gguf"
        subprocess.run(
            [str(quantize_bin), str(f16_path), str(output_path), quant_type],
            check=True,
        )
        quant_paths[quant_type] = output_path

    if not bool(config["export"]["keep_f16_gguf"]):
        f16_path.unlink(missing_ok=True)

    eval_config = config["evaluation"]
    test_rows = read_jsonl(Path("/repo/data/base/test.jsonl"))
    regression_rows = read_jsonl(Path("/repo/data/regression/live_failures.jsonl"))
    quant_metrics: dict[str, Any] = {}
    regression_metrics: dict[str, Any] = {}

    for index, (quant_type, model_path) in enumerate(quant_paths.items()):
        quant_metrics[quant_type] = evaluate_gguf(
            quant_name=quant_type,
            model_path=model_path,
            rows=test_rows,
            output_dir=evaluation_dir / "original_test" / quant_type,
            system_prompt=system_prompt,
            server_bin=server_bin,
            port=int(eval_config["server_port"]) + index,
            n_ctx=int(eval_config["n_ctx"]),
            n_predict=int(eval_config["n_predict"]),
            temperature=float(eval_config["temperature"]),
            top_k=int(eval_config["top_k"]),
            top_p=float(eval_config["top_p"]),
            repeat_penalty=float(eval_config["repeat_penalty"]),
            server_start_timeout_seconds=float(
                eval_config["server_start_timeout_seconds"]
            ),
        )
        if regression_rows:
            regression_metrics[quant_type] = evaluate_gguf(
                quant_name=f"{quant_type}_LIVE_REGRESSION",
                model_path=model_path,
                rows=regression_rows,
                output_dir=evaluation_dir / "live_regression" / quant_type,
                system_prompt=system_prompt,
                server_bin=server_bin,
                port=int(eval_config["server_port"]) + 100 + index,
                n_ctx=int(eval_config["n_ctx"]),
                n_predict=int(eval_config["n_predict"]),
                temperature=float(eval_config["temperature"]),
                top_k=int(eval_config["top_k"]),
                top_p=float(eval_config["top_p"]),
                repeat_penalty=float(eval_config["repeat_penalty"]),
                server_start_timeout_seconds=float(
                    eval_config["server_start_timeout_seconds"]
                ),
            )

    (evaluation_dir / "benchmark_summary.json").write_text(
        json.dumps(quant_metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if regression_metrics:
        (evaluation_dir / "live_regression_summary.json").write_text(
            json.dumps(regression_metrics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    q8_metrics = quant_metrics.get("Q8_0")
    gate_report = (
        compare_metrics(q8_metrics, config)
        if q8_metrics is not None
        else {
            "deploy_eligible": False,
            "checks": {},
            "reason": "Q8_0 was not built",
        }
    )
    (evaluation_dir / "release_gate.json").write_text(
        json.dumps(gate_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    artifacts = {}
    for quant_type, path in quant_paths.items():
        artifacts[quant_type] = {
            "path": str(path.relative_to(Path("/artifacts"))),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": now_iso(),
        "git_sha": git_sha,
        "base_model_id": model_id,
        "llama_cpp_commit": config["reproducibility"]["llama_cpp_commit"],
        "dataset": {
            **build_report,
            "combined_train_sha256": sha256_file(combined_train_path),
            "validation_sha256": sha256_file(dataset_dir / "validation.jsonl"),
            "test_sha256": sha256_file(dataset_dir / "test.jsonl"),
        },
        "training": {
            "dtype": str(model_dtype),
            "train_metrics": train_result.metrics,
            "validation_metrics": validation_metrics,
        },
        "evaluation": {
            "original_test": quant_metrics,
            "live_regression": regression_metrics,
            "release_gate": gate_report,
        },
        "artifacts": artifacts,
        "deploy_eligible": bool(gate_report["deploy_eligible"]),
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    artifact_volume.commit()
    hf_cache_volume.commit()
    return manifest_text


@app.local_entrypoint()
def main(
    run_id: str,
    git_sha: str,
    quant_types: str = "Q8_0",
) -> str:
    return train_and_evaluate.remote(run_id, git_sha, quant_types)
