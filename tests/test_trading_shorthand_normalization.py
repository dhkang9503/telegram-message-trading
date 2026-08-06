from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_BOT = ROOT / "live" / "live_bot.py"
FEEDBACK = ROOT / "data" / "feedback" / "mistakes_labeled.jsonl"


def load_normalizer():
    tree = ast.parse(LIVE_BOT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "normalize_trading_shorthand"
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re}
    exec(compile(module, str(LIVE_BOT), "exec"), namespace)
    return namespace["normalize_trading_shorthand"]


@pytest.mark.parametrize(
    ("prefix", "canonical"),
    [
        ("물", "물 ㅂㅈ"),
        ("롱", "롱 ㅂㅈ"),
        ("숏", "숏 ㅂㅈ"),
        ("반익", "반익 ㅂㅈ"),
        ("익자유", "익자유 ㅂㅈ"),
        ("익절", "익절 ㅂㅈ"),
    ],
)
def test_shorthand_whitespace_variants_normalize(prefix: str, canonical: str) -> None:
    normalize = load_normalizer()
    variants = [
        f"{prefix}ㅂㅈ",
        f"{prefix} ㅂㅈ",
        f"{prefix}  ㅂ ㅈ",
        f"  {prefix} ㅂㅈ  ",
    ]
    for variant in variants:
        assert normalize(variant) == canonical


def test_normalizer_does_not_rewrite_natural_language_or_typos() -> None:
    normalize = load_normalizer()
    natural = "오늘은 익자유 ㅂㅈ 아니고 조금 더 볼게요"
    typo = "익자유 ㅂㅈㅈ"
    assert normalize(natural) == natural
    assert normalize(typo) == typo


def test_pr52_training_rows_are_removed_but_original_compact_add_remains() -> None:
    messages = {
        json.loads(line)["source"]["message"]
        for line in FEEDBACK.read_text(encoding="utf-8").splitlines()
    }
    removed = {
        "물 ㅂㅈ",
        "롱 ㅂㅈ",
        "롱ㅂㅈ",
        "숏 ㅂㅈ",
        "숏ㅂㅈ",
        "반익 ㅂㅈ",
        "반익ㅂㅈ",
        "익자유 ㅂㅈ",
        "익자유ㅂㅈ",
        "익절 ㅂㅈ",
        "익절ㅂㅈ",
    }
    assert messages.isdisjoint(removed)
    assert "물ㅂㅈ" in messages
