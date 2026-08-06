from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_live_bot_deploy_installs_ci_dependencies_before_validation() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy-live-bot.yml").read_text(
        encoding="utf-8"
    )

    install = "python -m pip install --disable-pip-version-check --quiet -r requirements-ci.txt"
    validate = "python scripts/validate_dataset.py --root ."

    assert install in workflow
    assert validate in workflow
    assert workflow.index(install) < workflow.index(validate)
    assert "--quiet pytest" not in workflow
