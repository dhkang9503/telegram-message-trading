from __future__ import annotations

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
