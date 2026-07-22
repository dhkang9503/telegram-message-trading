from pathlib import Path

path = Path("modal/train_app.py")
text = path.read_text(encoding="utf-8")

old_disk = "    ephemeral_disk=20480,\n"
if old_disk not in text:
    raise SystemExit("Expected ephemeral_disk=20480 setting was not found")
text = text.replace(old_disk, "", 1)

replacements = {
    '"/repo/data/base/validation.jsonl"': '"/repo/data/runtime/validation-plus-mistakes.jsonl"',
    '"/repo/data/base/test.jsonl"': '"/repo/data/runtime/test-plus-mistakes.jsonl"',
}
for old, new in replacements.items():
    occurrences = text.count(old)
    if occurrences == 0:
        raise SystemExit(f"Expected Modal dataset path was not found: {old}")
    text = text.replace(old, new)
    print(f"Replaced {occurrences} occurrence(s): {old} -> {new}")

path.write_text(text, encoding="utf-8")
print("Removed invalid explicit Modal disk request and enabled mistake-aware validation/test data.")
