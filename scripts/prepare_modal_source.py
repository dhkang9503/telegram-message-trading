from pathlib import Path

path = Path("modal/train_app.py")
text = path.read_text(encoding="utf-8")

replacements = {
    '    ephemeral_disk=20480,\n': '',
    '"/repo/data/base/validation.jsonl"': '"/repo/data/runtime/validation-plus-mistakes.jsonl"',
    '"/repo/data/base/test.jsonl"': '"/repo/data/runtime/test-plus-mistakes.jsonl"',
}
for old, new in replacements.items():
    occurrences = text.count(old)
    if occurrences == 0:
        raise SystemExit(f"Expected Modal source fragment was not found: {old!r}")
    text = text.replace(old, new)
    print(f"Replaced {occurrences} occurrence(s): {old!r} -> {new!r}")

training_build = '''    combined_rows, build_report = build_training_rows(
        Path("/repo/data/base/train.jsonl"),
        Path("/repo/data/feedback/mistakes_labeled.jsonl"),
        system_prompt,
    )
'''
training_build_with_repeat = '''    combined_rows, build_report = build_training_rows(
        Path("/repo/data/base/train.jsonl"),
        Path("/repo/data/feedback/mistakes_labeled.jsonl"),
        system_prompt,
        feedback_repeat=int(config["training"]["feedback_repeat"]),
    )
'''
if text.count(training_build) != 1:
    raise SystemExit("Expected training dataset builder call was not found exactly once")
text = text.replace(training_build, training_build_with_repeat, 1)

copy_test = (
    '    shutil.copy2("/repo/data/runtime/test-plus-mistakes.jsonl", '
    'dataset_dir / "test.jsonl")\n'
)
if text.count(copy_test) != 1:
    raise SystemExit("Expected runtime test dataset copy was not found exactly once")
text = text.replace(
    copy_test,
    copy_test
    + '    shutil.copy2("/repo/data/runtime/mistakes-eval.jsonl", '
    'dataset_dir / "mistakes.jsonl")\n',
    1,
)

start_marker = "    merged_model.eval()\n"
end_marker = "    manifest = {\n"
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit("Expected merged-model evaluation block was not found")

new_evaluation_block = '''    generation = config["evaluation"]

    def evaluate_merged(data_path: Path, output_dir: Path) -> dict[str, Any]:
        rows = read_jsonl(data_path)
        exact = json_valid = type_exact = price_exact = side_exact = 0
        error_counts: Counter[str] = Counter()
        predictions: list[dict[str, Any]] = []

        for row in rows:
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
            raw = tokenizer.decode(
                output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
            ).strip()
            expected = normalize_result(json.loads(messages[2]["content"]))
            result: dict[str, Any] = {
                "message": messages[1]["content"],
                "expected": expected,
                "raw_output": raw,
                "json_valid": False,
                "exact_match": False,
            }
            try:
                start_index = raw.find("{")
                end_index = raw.rfind("}")
                predicted = normalize_result(json.loads(raw[start_index:end_index + 1]))
                result["predicted"] = predicted
                result["json_valid"] = True
                json_valid += 1
                if predicted == expected:
                    exact += 1
                    result["exact_match"] = True
                if [a["type"] for a in predicted["actions"]] == [
                    a["type"] for a in expected["actions"]
                ]:
                    type_exact += 1
                if [a["price"] for a in predicted["actions"]] == [
                    a["price"] for a in expected["actions"]
                ]:
                    price_exact += 1
                if [a.get("side") for a in predicted["actions"]] == [
                    a.get("side") for a in expected["actions"]
                ]:
                    side_exact += 1
                classify_errors(expected, predicted, error_counts)
            except Exception as exc:
                result["parse_error"] = f"{type(exc).__name__}: {exc}"
                error_counts["INVALID_JSON"] += 1
            predictions.append(result)

        total = len(rows)
        metrics = {
            "backend": "transformers-merged-fp16",
            "test_rows": total,
            "json_valid_rate": json_valid / total if total else 0.0,
            "exact_match_rate": exact / total if total else 0.0,
            "action_type_exact_rate": type_exact / total if total else 0.0,
            "price_exact_rate": price_exact / total if total else 0.0,
            "action_side_exact_rate": side_exact / total if total else 0.0,
            "errors": dict(error_counts),
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8"
        )
        with (output_dir / "predictions.jsonl").open("w", encoding="utf-8") as file:
            for item in predictions:
                file.write(json.dumps(item, ensure_ascii=False) + "\\n")
        return metrics

    merged_model.eval()
    transformers_test_plus_mistakes = evaluate_merged(
        Path("/repo/data/runtime/test-plus-mistakes.jsonl"),
        evaluation_dir / "test_plus_mistakes",
    )
    transformers_mistake_recovery = evaluate_merged(
        Path("/repo/data/runtime/mistakes-eval.jsonl"),
        evaluation_dir / "mistake_recovery",
    )
    shutil.copy2(
        evaluation_dir / "test_plus_mistakes" / "metrics.json",
        evaluation_dir / "test_metrics.json",
    )
    shutil.copy2(
        evaluation_dir / "test_plus_mistakes" / "predictions.jsonl",
        evaluation_dir / "test_predictions.jsonl",
    )

'''
text = text[:start] + new_evaluation_block + text[end:]

old_dataset_hash = '            "test_sha256": sha256_file(dataset_dir / "test.jsonl"),\n'
if text.count(old_dataset_hash) != 1:
    raise SystemExit("Expected test dataset hash entry was not found exactly once")
text = text.replace(
    old_dataset_hash,
    old_dataset_hash
    + '            "mistakes_sha256": sha256_file(dataset_dir / "mistakes.jsonl"),\n',
    1,
)

old_manifest_evaluation = '        "evaluation": {"transformers_test": transformers_test},\n'
if text.count(old_manifest_evaluation) != 1:
    raise SystemExit("Expected Transformers manifest evaluation entry was not found exactly once")
text = text.replace(
    old_manifest_evaluation,
    '''        "evaluation": {
            "transformers_test": transformers_test_plus_mistakes,
            "transformers_test_plus_mistakes": transformers_test_plus_mistakes,
            "transformers_mistake_recovery": transformers_mistake_recovery,
        },
''',
    1,
)

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")
print("Prepared A100-40GB Modal training with 4x feedback and merged-model mistake evaluation.")
