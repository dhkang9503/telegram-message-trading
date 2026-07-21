from pathlib import Path

path = Path("modal/train_app.py")
text = path.read_text(encoding="utf-8")
old = "    ephemeral_disk=20480,\n"
if old not in text:
    raise SystemExit("Expected ephemeral_disk=20480 setting was not found")
path.write_text(text.replace(old, "", 1), encoding="utf-8")
print("Removed invalid explicit Modal ephemeral_disk request; using Modal default disk quota.")
