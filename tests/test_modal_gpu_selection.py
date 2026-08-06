from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODAL_APP = ROOT / "modal" / "train_app.py"
PREPARE_SCRIPT = ROOT / "scripts" / "prepare_modal_source.py"


def test_modal_training_stays_on_a100_40gb() -> None:
    modal_source = MODAL_APP.read_text(encoding="utf-8")
    prepare_source = PREPARE_SCRIPT.read_text(encoding="utf-8")

    assert 'gpu="A100-40GB"' in modal_source
    assert 'gpu="H100"' not in modal_source
    assert "Prepared A100-40GB Modal training" in prepare_source
    assert "gpu=\"H100\"" not in prepare_source
    assert "Prepared H100 Modal training" not in prepare_source


def test_prepare_modal_source_generates_valid_python(tmp_path: Path) -> None:
    modal_dir = tmp_path / "modal"
    scripts_dir = tmp_path / "scripts"
    modal_dir.mkdir()
    scripts_dir.mkdir()
    shutil.copy2(MODAL_APP, modal_dir / "train_app.py")
    shutil.copy2(PREPARE_SCRIPT, scripts_dir / "prepare_modal_source.py")

    subprocess.run(
        [sys.executable, "scripts/prepare_modal_source.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    generated_path = modal_dir / "train_app.py"
    generated = generated_path.read_text(encoding="utf-8")
    compile(generated, str(generated_path), "exec")
    assert 'gpu="A100-40GB"' in generated
    assert 'gpu="H100"' not in generated
    assert '+ "\\n", encoding="utf-8"' in generated
