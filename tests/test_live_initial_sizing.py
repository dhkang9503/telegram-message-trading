from __future__ import annotations

import ast
import asyncio
import textwrap
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace

import pytest


def source_nodes(*names: str) -> tuple[str, list[ast.AST], ast.AsyncFunctionDef]:
    source = Path("live/live_bot.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = set(names)
    nodes = [
        node
        for node in module.body
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and node.name in wanted
        )
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in wanted
                for target in node.targets
            )
        )
    ]
    trading_engine = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "TradingEngine"
    )
    open_method = next(
        node
        for node in trading_engine.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "open"
    )
    return source, nodes, open_method


def load_initial_sizing():
    source, nodes, _ = source_nodes(
        "BotError",
        "D",
        "INITIAL_MARGIN_EQUITY_RATIO",
        "initial_margin_from_account",
    )
    namespace = {
        "Decimal": Decimal,
    }
    exec(
        "from __future__ import annotations\n"
        "from typing import Any\n"
        + "\n\n".join(ast.get_source_segment(source, node) for node in nodes),
        namespace,
    )
    return namespace


def load_open_flow():
    source, nodes, open_method = source_nodes(
        "BotError",
        "D",
        "INITIAL_MARGIN_EQUITY_RATIO",
        "initial_margin_from_account",
        "ds",
        "restore_btc_price",
    )
    namespace = {
        "Decimal": Decimal,
        "ROUND_DOWN": ROUND_DOWN,
        "LEVERAGE": Decimal("98"),
        "PRICE_RESTORE_MAX_GAP_RATIO": Decimal("0.03"),
    }
    exec(
        "from __future__ import annotations\n"
        "from typing import Any, Optional\n"
        + "\n\n".join(ast.get_source_segment(source, node) for node in nodes)
        + "\n\n"
        + textwrap.dedent(ast.get_source_segment(source, open_method)),
        namespace,
    )
    return namespace


initial_ns = load_initial_sizing()
BotError = initial_ns["BotError"]
INITIAL_MARGIN_EQUITY_RATIO = initial_ns["INITIAL_MARGIN_EQUITY_RATIO"]
initial_margin_from_account = initial_ns["initial_margin_from_account"]
open_entry = load_open_flow()["open"]


class Config:
    min_trade_usdt = Decimal("5")
    min_trade_num = Decimal("0.0001")
    price_step = Decimal("0.1")
    size_step = Decimal("0.0001")

    def floor_price(self, value: Decimal) -> Decimal:
        units = (value / self.price_step).to_integral_value(rounding=ROUND_DOWN)
        return units * self.price_step

    def floor_size(self, value: Decimal) -> Decimal:
        units = (value / self.size_step).to_integral_value(rounding=ROUND_DOWN)
        return units * self.size_step


class AccountAPI:
    def __init__(self, equity: str = "400"):
        self.equity = equity
        self.calls = 0

    async def account(self):
        self.calls += 1
        return {"usdtEquity": self.equity}


def make_engine(reference: Decimal = Decimal("63900")):
    api = AccountAPI()
    config = Config()
    captured: dict = {}

    async def reference_price():
        return reference

    async def place_opening_order(**kwargs):
        captured.update(kwargs)
        return kwargs

    engine = SimpleNamespace(
        state=SimpleNamespace(
            data={
                "position": {"total_qty": "0"},
                "pending": {"entry_order": None},
                "stopped_position": None,
            }
        ),
        api=api,
        config=config,
        reference_price=reference_price,
        place_opening_order=place_opening_order,
    )
    return engine, api, config, captured


def test_uses_one_point_two_percent_of_account_equity():
    equity, margin = initial_margin_from_account({"usdtEquity": "400"})

    assert equity == Decimal("400")
    assert margin == Decimal("4.8")


def test_preserves_fractional_equity_without_early_rounding():
    equity, margin = initial_margin_from_account({"usdtEquity": "400.3"})

    assert equity == Decimal("400.3")
    assert margin == Decimal("4.8036")


@pytest.mark.parametrize(
    "raw_equity",
    [None, "", "0", "-1", "not-a-number"],
)
def test_rejects_missing_non_positive_or_invalid_equity(raw_equity):
    with pytest.raises(BotError):
        initial_margin_from_account({"usdtEquity": raw_equity})


def test_open_fetches_equity_but_reentry_keeps_previous_margin():
    source = Path("live/live_bot.py").read_text(encoding="utf-8")

    assert "account_equity, margin = initial_margin_from_account(" in source
    assert "await self.api.account()" in source
    assert 'sizing_source = "account_usdt_equity_ratio"' in source
    assert 'margin = D(stopped.get("total_margin_usdt"))' in source
    assert 'sizing_source = "previous_sl_margin_usdt"' in source


def test_limit_entry_quantity_uses_resolved_limit_price():
    engine, api, config, captured = make_engine()

    asyncio.run(open_entry(engine, "OPEN_LONG", 63000, 101, 0))

    notional = Decimal("400") * INITIAL_MARGIN_EQUITY_RATIO * Decimal("98")
    expected = config.floor_size(notional / Decimal("63000"))
    mark_based = config.floor_size(notional / Decimal("63900"))

    assert api.calls == 1
    assert expected != mark_based
    assert captured["qty"] == expected
    assert captured["raw_price"] == 63000
    assert captured["resolved_price"] == Decimal("63000")
    assert captured["entry_sizing"]["margin_usdt"] == "4.8"
    assert captured["entry_sizing"]["sizing_price"] == "63000"


def test_market_entry_quantity_uses_current_reference_price():
    engine, _, config, captured = make_engine()

    asyncio.run(open_entry(engine, "OPEN_SHORT", None, 102, 0))

    notional = Decimal("400") * INITIAL_MARGIN_EQUITY_RATIO * Decimal("98")
    expected = config.floor_size(notional / Decimal("63900"))

    assert captured["qty"] == expected
    assert captured["raw_price"] is None
    assert captured["resolved_price"] is None
    assert captured["entry_sizing"]["sizing_price"] == "63900"
