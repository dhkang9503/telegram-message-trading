from __future__ import annotations

import gc
import hashlib
import json
import random
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import modal

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_LOCAL = REPO_ROOT / "configs"
DATA_LOCAL = REPO_ROOT / "data"
SCRIPTS_LOCAL = REPO_ROOT / "scripts"

ARTIFACT_VOLUME_NAME = "telegram-parser-artifacts"
HF_CACHE_VOLUME_NAME = "telegram-parser-hf-cache"

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.1-runtime-ubuntu22.04",
        add_python="3.12",
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

OPEN_TYPES = {"OPEN_LONG", "OPEN_SHORT"}
CLOSE_TYPES = {"CLOSE_HALF", "CLOSE_ADDS", "CLOSE_ALL"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def normalize_result(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict) or not isinstance(obj.get("actions"), list):
        return {"actions": []}
    actions = []
    for action in obj["actions"]:
        if isinstance(action, dict):
            actions.append({"type": action.get("type"), "price": action.get("price")})
    return {"actions": actions}


def classify_errors(expected: dict[str, Any], predicted: dict[str, Any], counts: Counter[str]) -> None:
    expected_types = [item["type"] for item in expected["actions"]]
    predicted_types = [item["type"] for item in predicted["actions"]]
    expected_set = set(expected_types)
    predicted_set = set(predicted_types)
    if "OPEN_LONG" in expected_set and "OPEN_SHORT" in predicted_set:
        counts["LONG_TO_SHORT"] += 1
    if "OPEN_SHORT" in expected_set and "OPEN_LONG" in predicted_set:
        counts["SHORT_TO_LONG"] += 1
    if expected_set & OPEN_TYPES and predicted_set & CLOSE_TYPES:
        counts["OPEN_TO_CLOSE"] += 1
    if expected_set & CLOSE_TYPES and predicted_set & OPEN_TYPES:
        counts["CLOSE_TO_OPEN"] += 1
    if not expected_types and predicted_types:
        counts["FALSE_ACTION"] += 1
    if expected_types and not predicted_types:
        counts["MISSED_ACTION"] += 1


@app.function(
    gpu="A100-40GB",
    cpu=8.0,
    memory=32768,
    ephemeral_disk=20480,
    timeout=4 * 60 * 60,
    volumes={"/artifacts": artifact_volume, "/cache": hf_cache_volume},
)
def train_and_evaluate(run_id: str, git_sha: str) -> str:
    import numpy as np
    import torch
    import yaml
    from datasets import Dataset, DatasetDict
    from huggingface_hub import snapshot_download
    from peft import LoraConfig, get_peft_model
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
    from scripts.validate_dataset import validate_repository_data

    config_path = Path("/repo/configs/train.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    system_prompt = config["model"]["system_prompt"]

    run_root = Path("/artifacts/runs") / run_id
    training_manifest_path = run_root / "training_manifest.json"
    if training_manifest_path.exists():
        artifact_volume.reload()
        if training_manifest_path.exists():
            return training_manifest_path.read_text(encoding="utf-8")

    checkpoints_dir = run_root / "checkpoints"
    adapter_dir = run_root / "lora_adapter"
    merged_dir = run_root / "merged_model"
    evaluation_dir = run_root / "evaluation" / "transformers"
    dataset_dir = run_root / "dataset"
    for path in (checkpoints_dir, adapter_dir, merged_dir, evaluation_dir, dataset_dir):
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
    shutil.copy2("/repo/data/base/validation.jsonl", dataset_dir / "validation.jsonl")
    shutil.copy2("/repo/data/base/test.jsonl", dataset_dir / "test.jsonl")
    shutil.copy2(config_path, run_root / "train.yaml")

    model_id = config["model"]["base_model_id"]
    base_model_dir = Path("/cache/models") / model_id.replace("/", "--")
    if not (base_model_dir / "model.safetensors").exists():
        base_model_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=model_id, local_dir=str(base_model_dir), max_workers=8)
        hf_cache_volume.commit()

    tokenizer = AutoTokenizer.from_pretrained(str(base_model_dir), use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    max_length = int(config["model"]["max_length"])

    raw_rows = {
        "train": combined_rows,
        "validation": read_jsonl(Path("/repo/data/base/validation.jsonl")),
    }

    def tokenize_row(example: dict[str, Any]) -> dict[str, Any]:
        messages = example["messages"]
        prompt_ids = tokenizer.apply_chat_template(
            messages[:2], tokenize=True, add_generation_prompt=True, enable_thinking=False
        )
        completion_ids = tokenizer(
            messages[2]["content"] + tokenizer.eos_token, add_special_tokens=False
        )["input_ids"]
        input_ids = prompt_ids + completion_ids
        labels = [-100] * len(prompt_ids) + completion_ids.copy()
        attention_mask = [1] * len(input_ids)
        if len(input_ids) > max_length:
            overflow = len(input_ids) - max_length
            cut = min(overflow, len(prompt_ids))
            input_ids = input_ids[cut:]
            labels = labels[cut:]
            attention_mask = attention_mask[cut:]
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

    dataset = DatasetDict({name: Dataset.from_list(rows) for name, rows in raw_rows.items()})
    tokenized = dataset.map(
        tokenize_row, remove_columns=["messages"], num_proc=1, desc="tokenize"
    )

    use_bf16 = torch.cuda.is_bf16_supported()
    model_dtype = torch.bfloat16 if use_bf16 else torch.float16
    base_model = AutoModelForCausalLM.from_pretrained(
        str(base_model_dir), torch_dtype=model_dtype, device_map={"": 0}
    )
    base_model.config.use_cache = False

    lora = config["training"]["lora"]
    model = get_peft_model(
        base_model,
        LoraConfig(
            r=int(lora["r"]),
            lora_alpha=int(lora["alpha"]),
            lora_dropout=float(lora["dropout"]),
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=list(lora["target_modules"]),
        ),
    )
    model.enable_input_require_grads()

    training = config["training"]
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
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
        ),
        train_dataset=tokenized["train"],
        eval_dataset=tokenized["validation"],
        data_collator=DataCollatorForSeq2Seq(
            tokenizer=tokenizer,
            padding=True,
            label_pad_token_id=-100,
            return_tensors="pt",
        ),
    )

    last_checkpoint = get_last_checkpoint(str(checkpoints_dir))
    train_result = trainer.train(resume_from_checkpoint=last_checkpoint)
    trainer.save_model(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    validation_metrics = trainer.evaluate(tokenized["validation"])

    model.config.use_cache = True
    merged_model = model.merge_and_unload()
    merged_model.save_pretrained(str(merged_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(merged_dir))

    del model, base_model, trainer
    gc.collect()
    torch.cuda.empty_cache()

    merged_model.eval()
    test_rows = read_jsonl(Path("/repo/data/base/test.jsonl"))
    exact = json_valid = type_exact = price_exact = 0
    error_counts: Counter[str] = Counter()
    predictions: list[dict[str, Any]] = []
    generation = config["evaluation"]

    for row in test_rows:
        messages = row["messages"]
        prompt = tokenizer.apply_chat_template(
            messages[:2], tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        inputs = tokenizer(prompt, return_tensors="pt").to(merged_model.device)
        with torch.inference_mode():
            output = merged_model.generate(
                **inputs,
                max_new_tokens=int(generation["n_predict"]),
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        raw = tokenizer.decode(output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        expected = normalize_result(json.loads(messages[2]["content"]))
        result: dict[str, Any] = {
            "message": messages[1]["content"],
            "expected": expected,
            "raw_output": raw,
            "json_valid": False,
            "exact_match": False,
        }
        try:
            start = raw.find("{")
            end = raw.rfind("}")
            predicted = normalize_result(json.loads(raw[start:end + 1]))
            result["predicted"] = predicted
            result["json_valid"] = True
            json_valid += 1
            if predicted == expected:
                exact += 1
                result["exact_match"] = True
            if [a["type"] for a in predicted["actions"]] == [a["type"] for a in expected["actions"]]:
                type_exact += 1
            if [a["price"] for a in predicted["actions"]] == [a["price"] for a in expected["actions"]]:
                price_exact += 1
            classify_errors(expected, predicted, error_counts)
        except Exception as exc:
            result["parse_error"] = f"{type(exc).__name__}: {exc}"
            error_counts["INVALID_JSON"] += 1
        predictions.append(result)

    total = len(test_rows)
    transformers_test = {
        "backend": "transformers-merged-fp16",
        "test_rows": total,
        "json_valid_rate": json_valid / total if total else 0.0,
        "exact_match_rate": exact / total if total else 0.0,
        "action_type_exact_rate": type_exact / total if total else 0.0,
        "price_exact_rate": price_exact / total if total else 0.0,
        "errors": dict(error_counts),
    }
    (evaluation_dir / "test_metrics.json").write_text(
        json.dumps(transformers_test, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (evaluation_dir / "test_predictions.jsonl").open("w", encoding="utf-8") as file:
        for item in predictions:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")

    manifest = {
        "schema_version": 2,
        "stage": "trained",
        "run_id": run_id,
        "created_at": now_iso(),
        "git_sha": git_sha,
        "base_model_id": model_id,
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
        "evaluation": {"transformers_test": transformers_test},
        "artifacts": {
            "merged_model_path": str(merged_dir.relative_to(Path("/artifacts"))),
            "adapter_path": str(adapter_dir.relative_to(Path("/artifacts"))),
        },
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    training_manifest_path.write_text(text, encoding="utf-8")
    artifact_volume.commit()
    hf_cache_volume.commit()
    return text


@app.local_entrypoint()
def main(run_id: str, git_sha: str) -> str:
    return train_and_evaluate.remote(run_id, git_sha)
