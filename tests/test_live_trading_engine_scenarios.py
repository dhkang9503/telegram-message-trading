from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest


def load_live_bot():
    name = "live_bot_scenario_test_module"
    if name in sys.modules:
        return sys.modules[name]

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules.setdefault("dotenv", dotenv)

    class StubAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

    class StubHTTPStatusError(Exception):
        def __init__(self, *args, response=None, **kwargs):
            super().__init__(*args)
            self.response = response

    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = StubAsyncClient
    httpx.HTTPStatusError = StubHTTPStatusError
    sys.modules.setdefault("httpx", httpx)

    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = object
    telethon.events = types.SimpleNamespace(NewMessage=lambda *args, **kwargs: None)
    sys.modules.setdefault("telethon", telethon)

    spec = importlib.util.spec_from_file_location(name, Path("live/live_bot.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


live = load_live_bot()
D = live.D
BotError = live.BotError


class MemoryState:
    def __init__(self):
        self.data = live.default_state()
        self.save_count = 0

    def save(self):
        self.save_count += 1

    def is_processed(self, message_id: int) -> bool:
        return message_id in self.data["processed_message_ids"]

    def claim_message(self, message_id: int):
        if message_id not in self.data["processed_message_ids"]:
            self.data["processed_message_ids"].append(message_id)
        self.data["last_telegram_message_id"] = message_id
        self.save()


class FakeBitgetClient:
    def __init__(self, equity="400", mark_price="63900"):
        self.equity = equity
        self.mark_price = Decimal(mark_price)
        self.position_row: dict[str, Any] | None = None
        self.order_rows: list[dict[str, Any]] = []
        self.plan_rows: list[dict[str, Any]] = []
        self.plan_history_rows: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []
        self.account_calls = 0
        self.next_id = 1
        self.fail_next_place_order: Exception | None = None
        self.fail_next_place_trailing: Exception | None = None
        self.fail_cancel_order_ids: set[str] = set()
        self.fail_cancel_plan_ids: set[str] = set()

    def new_id(self, prefix: str) -> str:
        value = f"{prefix}-{self.next_id}"
        self.next_id += 1
        return value

    def record(self, method: str, **kwargs):
        self.calls.append({"method": method, **kwargs})

    async def account(self):
        self.account_calls += 1
        return {
            "usdtEquity": self.equity,
            "posMode": live.POSITION_MODE,
            "marginMode": live.MARGIN_MODE,
            "crossedMarginLeverage": live.ds(live.LEVERAGE),
        }

    async def ticker(self):
        return {"markPrice": live.ds(self.mark_price), "lastPr": live.ds(self.mark_price)}

    async def position(self):
        row = copy.deepcopy(self.position_row)
        if row is None:
            return None
        row.setdefault("markPrice", live.ds(self.mark_price))
        row.setdefault("leverage", live.ds(live.LEVERAGE))
        if "unrealizedPL" not in row:
            direction = Decimal("1") if row["holdSide"] == "long" else Decimal("-1")
            row["unrealizedPL"] = live.ds(
                (self.mark_price - D(row["openPriceAvg"]))
                * D(row["total"])
                * direction
            )
        return row

    async def pending_orders(self):
        return copy.deepcopy(self.order_rows)

    async def pending_plans(self):
        return copy.deepcopy(self.plan_rows)

    async def plan_history(self, order_id: str):
        self.record("plan_history", order_id=order_id)
        return copy.deepcopy(self.plan_history_rows.get(order_id, []))

    async def place_order(
        self,
        *,
        side: str,
        size: Decimal,
        order_type: str,
        client_oid: str,
        price: Decimal | None = None,
        reduce_only: bool = False,
    ):
        self.record(
            "place_order",
            side=side,
            size=size,
            order_type=order_type,
            client_oid=client_oid,
            price=price,
            reduce_only=reduce_only,
        )
        if self.fail_next_place_order:
            error = self.fail_next_place_order
            self.fail_next_place_order = None
            raise error

        order_id = self.new_id("order")
        if order_type == "limit":
            assert price is not None
            self.order_rows.append(
                {
                    "orderId": order_id,
                    "clientOid": client_oid,
                    "size": live.ds(size),
                    "price": live.ds(price),
                    "side": side,
                }
            )
            return {"orderId": order_id}

        if reduce_only:
            if self.position_row is None:
                raise BotError("Fake exchange has no position to reduce")
            remaining = max(Decimal("0"), D(self.position_row["total"]) - size)
            if remaining == 0:
                self.position_row = None
            else:
                entry = D(self.position_row["openPriceAvg"])
                self.position_row["total"] = live.ds(remaining)
                self.position_row["marginSize"] = live.ds(
                    remaining * entry / live.LEVERAGE
                )
            return {"orderId": order_id}

        position_side = "long" if side == "buy" else "short"
        fill_price = price or self.mark_price
        if self.position_row is None:
            total = size
            average = fill_price
            self.position_row = {
                "total": live.ds(total),
                "holdSide": position_side,
                "marginSize": live.ds(total * average / live.LEVERAGE),
                "openPriceAvg": live.ds(average),
                "breakEvenPrice": live.ds(average),
                "cTime": "1700000000000",
            }
        else:
            if self.position_row["holdSide"] != position_side:
                raise BotError("Fake exchange position side mismatch")
            old_total = D(self.position_row["total"])
            old_average = D(self.position_row["openPriceAvg"])
            total = old_total + size
            average = (old_total * old_average + size * fill_price) / total
            self.position_row.update(
                {
                    "total": live.ds(total),
                    "marginSize": live.ds(total * average / live.LEVERAGE),
                    "openPriceAvg": live.ds(average),
                    "breakEvenPrice": live.ds(average),
                }
            )
        return {"orderId": order_id}

    async def cancel_order(self, order_id: str):
        self.record("cancel_order", order_id=order_id)
        if order_id in self.fail_cancel_order_ids:
            raise BotError(f"Injected cancel failure: {order_id}")
        self.order_rows = [
            row for row in self.order_rows if str(row["orderId"]) != str(order_id)
        ]
        return {"orderId": order_id}

    async def flash_close(self):
        self.record("flash_close")
        self.position_row = None
        return {"closed": True}

    async def place_plan(self, *, plan_type, hold_side, trigger_price, client_oid):
        self.record(
            "place_plan",
            plan_type=plan_type,
            hold_side=hold_side,
            trigger_price=trigger_price,
            client_oid=client_oid,
        )
        order_id = self.new_id("plan")
        self.plan_rows.append(
            {
                "orderId": order_id,
                "clientOid": client_oid,
                "triggerPrice": live.ds(trigger_price),
                "planType": plan_type,
                "holdSide": hold_side,
            }
        )
        return {"orderId": order_id}

    async def place_trailing_plan(
        self,
        *,
        hold_side,
        trigger_price,
        size,
        range_rate,
        client_oid,
    ):
        self.record(
            "place_trailing_plan",
            hold_side=hold_side,
            trigger_price=trigger_price,
            size=size,
            range_rate=range_rate,
            client_oid=client_oid,
        )
        if self.fail_next_place_trailing:
            error = self.fail_next_place_trailing
            self.fail_next_place_trailing = None
            raise error
        order_id = self.new_id("trailing")
        self.plan_rows.append(
            {
                "orderId": order_id,
                "clientOid": client_oid,
                "triggerPrice": live.ds(trigger_price),
                "planType": "moving_plan",
                "holdSide": hold_side,
                "size": live.ds(size),
                "rangeRate": live.ds(range_rate),
            }
        )
        return {"orderId": order_id}

    async def cancel_plan(self, order_id: str, plan_type: str):
        self.record("cancel_plan", order_id=order_id, plan_type=plan_type)
        if order_id in self.fail_cancel_plan_ids:
            raise BotError(f"Injected plan cancel failure: {order_id}")
        target = next(
            (
                row
                for row in self.plan_rows
                if str(row["orderId"]) == str(order_id)
            ),
            None,
        )
        if (
            target is not None
            and target.get("planType") == "moving_plan"
            and plan_type != "track_plan"
        ):
            raise BotError(
                f"Moving plan cancellation requires track_plan, got {plan_type}"
            )
        self.plan_rows = [
            row for row in self.plan_rows if str(row["orderId"]) != str(order_id)
        ]
        return {"orderId": order_id}

    def trigger_stop(self, order_id: str):
        plan = next(
            row for row in self.plan_rows if str(row["orderId"]) == str(order_id)
        )
        self.plan_rows = [
            row for row in self.plan_rows if str(row["orderId"]) != str(order_id)
        ]
        self.position_row = None
        self.plan_history_rows[str(order_id)] = [
            {
                "orderId": str(order_id),
                "planStatus": "executed",
                "planType": plan["planType"],
                "executeOrderId": self.new_id("executed"),
                "uTime": "1700000001000",
            }
        ]


class ScenarioEngine(live.TradingEngine):
    def __init__(self, api, state, config):
        super().__init__(api, state, config)
        self.events = []

    def log(self, event: str, **data):
        self.events.append({"event": event, **data})

    async def wait_change(self, before: Decimal, increase: bool):
        return None

    async def wait_until_flat(self):
        if await self.api.position() is not None:
            raise BotError("Fake exchange position did not become flat")


@pytest.fixture
def config():
    return live.ContractConfig(
        min_trade_num=Decimal("0.0001"),
        min_trade_usdt=Decimal("5"),
        size_step=Decimal("0.0001"),
        price_step=Decimal("0.1"),
        max_leverage=Decimal("125"),
    )


@pytest.fixture
def rig(config):
    api = FakeBitgetClient()
    state = MemoryState()
    return ScenarioEngine(api, state, config), api, state, config


def run(coro):
    return asyncio.run(coro)


def execute(engine, action_type, price=None, message_id=1, index=0, source="test"):
    return run(
        engine.execute(
            {"type": action_type, "price": price},
            message_id,
            index,
            source,
        )
    )


def mutations(api, start=0):
    names = {
        "place_order",
        "cancel_order",
        "place_plan",
        "place_trailing_plan",
        "cancel_plan",
        "flash_close",
    }
    return [call for call in api.calls[start:] if call["method"] in names]


@pytest.mark.parametrize(
    ("action", "order_side", "position_side"),
    [("OPEN_LONG", "buy", "long"), ("OPEN_SHORT", "sell", "short")],
)
def test_market_open_uses_equity_ratio_and_correct_direction(
    rig, action, order_side, position_side
):
    engine, api, state, config = rig
    result = execute(engine, action)
    expected = config.floor_size(
        Decimal("400") * Decimal("0.01") * live.LEVERAGE / api.mark_price
    )
    placed = mutations(api)[0]
    assert (placed["side"], placed["order_type"], placed["size"]) == (
        order_side,
        "market",
        expected,
    )
    assert result["margin_usdt"] == "4"
    assert state.data["position"]["side"] == position_side
    assert D(state.data["position"]["initial_qty"]) == expected
    assert D(state.data["position"]["added_qty"]) == 0


@pytest.mark.parametrize(
    ("action", "side"), [("OPEN_LONG", "buy"), ("OPEN_SHORT", "sell")]
)
def test_limit_open_uses_resolved_price_and_stays_pending(rig, action, side):
    engine, api, state, config = rig
    result = execute(engine, action, 63000)
    expected = config.floor_size(
        Decimal("400") * Decimal("0.01") * live.LEVERAGE / Decimal("63000")
    )
    placed = mutations(api)[0]
    assert (placed["side"], placed["price"], placed["size"]) == (
        side,
        Decimal("63000"),
        expected,
    )
    assert result["sizing_price"] == "63000"
    assert state.data["position"]["total_qty"] == "0"
    assert state.data["pending"]["entry_order"]["price"] == "63000"


def test_open_rejected_when_position_exists(rig):
    engine, api, _, _ = rig
    execute(engine, "OPEN_LONG")
    before = len(mutations(api))
    with pytest.raises(BotError, match="position already exists"):
        execute(engine, "OPEN_SHORT", message_id=2)
    assert len(mutations(api)) == before


def test_open_and_add_are_safely_rejected_while_entry_limit_is_pending(rig):
    engine, api, _, _ = rig
    execute(engine, "OPEN_LONG", 63000)
    before = len(mutations(api))
    with pytest.raises(BotError, match="entry order already pending"):
        execute(engine, "OPEN_LONG", message_id=2)
    with pytest.raises(BotError, match="initial entry limit order"):
        execute(engine, "ADD", 62500, message_id=3)
    assert len(mutations(api)) == before


@pytest.mark.parametrize(
    ("open_action", "add_side", "position_side"),
    [("OPEN_LONG", "buy", "long"), ("OPEN_SHORT", "sell", "short")],
)
def test_market_add_doubles_position_and_tracks_added_quantity(
    rig, open_action, add_side, position_side
):
    engine, api, state, _ = rig
    execute(engine, open_action)
    initial = D(state.data["position"]["total_qty"])
    start = len(api.calls)
    execute(engine, "ADD", message_id=2)
    placed = mutations(api, start)[0]
    assert (placed["side"], placed["size"]) == (add_side, initial)
    assert state.data["position"]["side"] == position_side
    assert D(state.data["position"]["initial_qty"]) == initial
    assert D(state.data["position"]["added_qty"]) == initial
    assert D(state.data["position"]["total_qty"]) == initial * 2


def test_second_add_is_rejected_while_limit_add_is_pending(rig):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "ADD", 63000, message_id=2)
    before = len(mutations(api))
    with pytest.raises(BotError, match="another ADD limit order"):
        execute(engine, "ADD", 62500, message_id=3)
    assert len(mutations(api)) == before
    assert len(state.data["pending"]["add_orders"]) == 1


def test_cancel_is_prioritized_before_replacement_add(rig):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "ADD", 63000, message_id=2)
    old_id = state.data["pending"]["add_orders"][0]["order_id"]
    start = len(api.calls)
    actions = live.prioritize_cancel_actions(
        [{"type": "ADD", "price": 62500}, {"type": "CANCEL_ADD", "price": None}]
    )
    for index, action in enumerate(actions):
        run(engine.execute(action, 3, index, "기존 물타기 취소 후 지금 물타기"))
    calls = mutations(api, start)
    assert [call["method"] for call in calls] == ["cancel_order", "place_order"]
    assert calls[0]["order_id"] == old_id
    assert calls[1]["price"] == Decimal("62500")
    assert len(state.data["pending"]["add_orders"]) == 1
    assert state.data["pending"]["add_orders"][0]["price"] == "62500"


def test_cancel_failure_keeps_pending_add_and_blocks_replacement(rig):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "ADD", 63000, message_id=2)
    old_id = state.data["pending"]["add_orders"][0]["order_id"]
    api.fail_cancel_order_ids.add(old_id)
    with pytest.raises(BotError, match="Injected cancel failure"):
        execute(engine, "CANCEL_ADD", message_id=3)
    assert state.data["pending"]["add_orders"][0]["order_id"] == old_id
    with pytest.raises(BotError, match="another ADD limit order"):
        execute(engine, "ADD", 62500, message_id=4)


def test_cancel_stop_is_prioritized_before_replacement_stop(rig):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "SET_STOP", 63000, message_id=2)
    old_id = state.data["pending"]["stop_order"]["order_id"]
    start = len(api.calls)
    actions = live.prioritize_cancel_actions(
        [{"type": "SET_STOP", "price": 63200}, {"type": "CANCEL_STOP", "price": None}]
    )
    for index, action in enumerate(actions):
        run(engine.execute(action, 3, index, "기존 손절 취소 후 새 손절"))
    calls = mutations(api, start)
    assert [call["method"] for call in calls] == ["cancel_plan", "place_plan"]
    assert calls[0]["order_id"] == old_id
    assert calls[1]["trigger_price"] == Decimal("63200")
    assert len(api.plan_rows) == 1
    assert state.data["pending"]["stop_order"]["price"] == "63200"


def test_close_half_reduces_position_without_flipping_side(rig):
    engine, api, state, config = rig
    execute(engine, "OPEN_LONG")
    total = D(state.data["position"]["total_qty"])
    expected = config.floor_size(total / 2)
    start = len(api.calls)
    execute(engine, "CLOSE_HALF", message_id=2)
    placed = mutations(api, start)[0]
    assert placed["reduce_only"] is True
    assert (placed["side"], placed["size"]) == ("sell", expected)
    assert state.data["position"]["side"] == "long"
    assert D(state.data["position"]["total_qty"]) == total - expected


@pytest.mark.parametrize(
    "source",
    [
        "첫비중 만들고 볼게요 ! 조금 더 산것도 날리세요 !",
    ],
)
def test_mistake_close_adds_preserves_initial_position(rig, source):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    initial = D(state.data["position"]["initial_qty"])
    execute(engine, "ADD", message_id=2)
    start = len(api.calls)
    run(engine.execute({"type": "CLOSE_ADDS", "price": None}, 3, 0, source))
    placed = mutations(api, start)[0]
    assert placed["reduce_only"] is True
    assert placed["size"] == initial
    assert D(state.data["position"]["initial_qty"]) == initial
    assert D(state.data["position"]["added_qty"]) == 0
    assert D(state.data["position"]["total_qty"]) == initial


@pytest.mark.parametrize(
    "source",
    [
        "손절 ㅂㅈ",
        "손절ㅂㅈ",
        "비트 자유롭게!",
        "슬슬 정리들 하세요 ! 집 가는중",
        "지금 터심들 돼요!",
    ],
)
def test_mistake_close_all_clears_position_orders_and_plans(rig, source):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "ADD", 63000, message_id=2)
    execute(engine, "SET_STOP", 62500, message_id=3)
    execute(engine, "SET_TP", None, message_id=4)
    run(engine.execute({"type": "CLOSE_ALL", "price": None}, 5, 0, source))
    assert api.position_row is None
    assert api.order_rows == []
    assert api.plan_rows == []
    assert D(state.data["position"]["total_qty"]) == 0
    assert state.data["pending"]["entry_order"] is None
    assert state.data["pending"]["add_orders"] == []
    assert state.data["pending"]["stop_order"] is None
    assert state.data["pending"]["tp_order"] is None


def test_close_all_while_flat_cancels_pending_entry_only(rig):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG", 63000)
    result = execute(engine, "CLOSE_ALL", message_id=2)
    assert result == {"flat": True, "opening_orders_cancelled": True}
    assert api.position_row is None
    assert api.order_rows == []
    assert state.data["pending"]["entry_order"] is None
    assert not any(call["method"] == "flash_close" for call in api.calls)


def test_close_all_rejected_when_completely_flat(rig):
    engine, api, _, _ = rig
    with pytest.raises(BotError, match="no position or pending opening order"):
        execute(engine, "CLOSE_ALL")
    assert mutations(api) == []


def test_stop_execution_cancels_pending_add_and_saves_reentry_snapshot(rig):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "ADD", 63000, message_id=2)
    pending_add_id = state.data["pending"]["add_orders"][0]["order_id"]
    execute(engine, "SET_STOP", 62500, message_id=3)
    stop_id = state.data["pending"]["stop_order"]["order_id"]
    previous_margin = state.data["position"]["margin_size_usdt"]
    api.trigger_stop(stop_id)
    run(engine.reconcile())
    assert any(
        call["method"] == "cancel_order" and call["order_id"] == pending_add_id
        for call in api.calls
    )
    assert api.order_rows == []
    assert D(state.data["position"]["total_qty"]) == 0
    stopped = state.data["stopped_position"]
    assert stopped["side"] == "long"
    assert stopped["total_margin_usdt"] == previous_margin
    assert stopped["stop_order_id"] == stop_id
    assert stopped["available_for_reentry"] is True


def prepare_stopped_position(rig):
    engine, api, state, config = rig
    execute(engine, "OPEN_SHORT")
    execute(engine, "SET_STOP", 64500, message_id=2)
    stop_id = state.data["pending"]["stop_order"]["order_id"]
    api.trigger_stop(stop_id)
    run(engine.reconcile())
    return engine, api, state, config


def test_mistake_open_reentry_reuses_stopped_margin_and_side(rig):
    engine, api, state, config = prepare_stopped_position(rig)
    stopped_margin = D(state.data["stopped_position"]["total_margin_usdt"])
    account_calls = api.account_calls
    result = run(
        engine.execute(
            {"type": "OPEN_REENTRY", "price": None},
            3,
            0,
            "아까 재진입 한 비중이랑 똑같이들 잡으심돼요 물 한번 타고 손절 볼거에요",
        )
    )
    expected = config.floor_size(stopped_margin * live.LEVERAGE / api.mark_price)
    placed = [call for call in mutations(api) if call["method"] == "place_order"][-1]
    assert api.account_calls == account_calls
    assert (placed["side"], placed["size"]) == ("sell", expected)
    assert result["sizing_source"] == "previous_sl_margin_usdt"
    assert D(result["margin_usdt"]) == stopped_margin
    assert state.data["stopped_position"]["available_for_reentry"] is False
    assert state.data["stopped_position"]["consumed_by_message_id"] == 3


def test_reentry_order_failure_does_not_consume_allowance(rig):
    engine, api, state, _ = prepare_stopped_position(rig)
    api.fail_next_place_order = BotError("Injected order failure")
    with pytest.raises(BotError, match="Injected order failure"):
        execute(engine, "OPEN_REENTRY", message_id=3)
    assert state.data["stopped_position"]["available_for_reentry"] is True
    assert state.data["stopped_position"]["consumed_at"] is None


def test_reentry_without_verified_stop_is_rejected(rig):
    engine, api, _, _ = rig
    with pytest.raises(BotError, match="no verified SL-closed position"):
        execute(engine, "OPEN_REENTRY")
    assert mutations(api) == []


def test_manual_flatten_does_not_create_reentry_allowance(rig):
    engine, api, state, _ = rig
    execute(engine, "OPEN_LONG")
    api.position_row = None
    run(engine.reconcile())
    assert D(state.data["position"]["total_qty"]) == 0
    assert state.data["stopped_position"] is None


def test_unknown_manual_pending_order_fails_closed(rig):
    engine, api, _, _ = rig
    api.order_rows.append(
        {
            "orderId": "manual-order",
            "clientOid": "manual-client-oid",
            "size": "0.001",
            "price": "63000",
        }
    )
    with pytest.raises(BotError, match="Untracked pending BTC order"):
        run(engine.reconcile())


def test_multiple_stop_plans_fail_closed(rig):
    engine, api, _, _ = rig
    api.plan_rows.extend(
        [
            {
                "orderId": "stop-1",
                "clientOid": "tg1-0-sl-a",
                "triggerPrice": "63000",
                "planType": "pos_loss",
            },
            {
                "orderId": "stop-2",
                "clientOid": "tg2-0-sl-b",
                "triggerPrice": "62500",
                "planType": "pos_loss",
            },
        ]
    )
    with pytest.raises(BotError, match="Multiple BTC stop-loss"):
        run(engine.reconcile())


def test_position_side_mismatch_fails_closed(rig):
    engine, api, state, _ = rig
    state.data["position"].update(
        {"side": "long", "initial_qty": "0.001", "total_qty": "0.001"}
    )
    api.position_row = {
        "total": "0.001",
        "holdSide": "short",
        "marginSize": "1",
        "openPriceAvg": "63900",
        "breakEvenPrice": "63900",
        "cTime": "1700000000000",
    }
    with pytest.raises(BotError, match="Position side mismatch"):
        run(engine.reconcile())


def test_cancel_stop_clears_failed_stop_even_without_live_plan(rig):
    engine, _, state, _ = rig
    state.data["pending"]["failed_stop"] = {
        "failed_price": "63000",
        "message_id": 1,
    }
    result = execute(engine, "CANCEL_STOP", message_id=2)
    assert result == {"cancelled_order_id": None}
    assert state.data["pending"]["failed_stop"] is None


def test_regular_open_after_stop_uses_fresh_equity_instead_of_reentry_allowance(rig):
    engine, _, state, _ = prepare_stopped_position(rig)
    stopped_margin = state.data["stopped_position"]["total_margin_usdt"]
    result = execute(engine, "OPEN_LONG", message_id=3)
    assert result["sizing_source"] == "account_usdt_equity_1_25_percent"
    assert result["margin_usdt"] == "4"
    assert result["margin_usdt"] != stopped_margin



def test_directional_reentry_matching_side_reuses_stopped_position(rig):
    engine, api, state, config = prepare_stopped_position(rig)
    stopped_margin = D(state.data["stopped_position"]["total_margin_usdt"])
    account_calls = api.account_calls

    result = run(
        engine.execute(
            {"type": "OPEN_REENTRY", "price": None, "side": "short"},
            3,
            0,
            "숏 재진입 ㅂㅈ",
        )
    )

    expected_qty = config.floor_size(stopped_margin * live.LEVERAGE / api.mark_price)
    placed = [call for call in mutations(api) if call["method"] == "place_order"][-1]
    assert api.account_calls == account_calls
    assert placed["side"] == "sell"
    assert placed["size"] == expected_qty
    assert result["sizing_source"] == "previous_sl_margin_usdt"
    assert state.data["stopped_position"]["available_for_reentry"] is False


def test_directional_reentry_side_mismatch_fails_closed(rig):
    engine, api, state, _ = prepare_stopped_position(rig)
    before = len(mutations(api))

    with pytest.raises(BotError, match="requested side does not match"):
        run(
            engine.execute(
                {"type": "OPEN_REENTRY", "price": None, "side": "long"},
                3,
                0,
                "롱 재진입 ㅂㅈ",
            )
        )

    assert len(mutations(api)) == before
    assert state.data["stopped_position"]["available_for_reentry"] is True



def test_price_typo_pair_corrects_93860_93590_and_executes_both(rig):
    engine, api, state, config = rig
    api.mark_price = Decimal("64000")
    execute(engine, "OPEN_LONG")
    assert D(state.data["position"]["entry_price"]) == Decimal("64000")

    original = [
        {"type": "ADD", "price": 93860},
        {"type": "SET_STOP", "price": 93590},
    ]
    actions, corrections, rejected = live.preflight_and_correct_add_stop_prices(
        original, api.mark_price, state.data["position"], config
    )

    assert actions == [
        {"type": "ADD", "price": 63860},
        {"type": "SET_STOP", "price": 63590},
    ]
    assert [item["corrected_price"] for item in corrections] == [63860, 63590]
    assert rejected == []

    start = len(api.calls)
    for index, action in enumerate(actions):
        run(engine.execute(action, 14676, index, "93860물타기 93590손절 걸겠습니다!"))
    calls = mutations(api, start)
    assert [call["method"] for call in calls] == ["place_order", "place_plan"]
    assert calls[0]["price"] == Decimal("63860")
    assert calls[1]["trigger_price"] == Decimal("63590")


def test_single_bad_add_is_rejected_instead_of_guessed(rig):
    engine, api, state, config = rig
    api.mark_price = Decimal("64000")
    execute(engine, "OPEN_LONG")

    actions, corrections, rejected = live.preflight_and_correct_add_stop_prices(
        [{"type": "ADD", "price": 93860}],
        api.mark_price,
        state.data["position"],
        config,
    )

    assert actions == []
    assert corrections == []
    assert len(rejected) == 1
    assert rejected[0]["action"] == {"type": "ADD", "price": 93860}
    assert "too far from market" in rejected[0]["reason"]


def test_bad_add_does_not_block_valid_stop_in_preflight(rig):
    engine, api, state, config = rig
    api.mark_price = Decimal("64000")
    execute(engine, "OPEN_LONG")

    actions, corrections, rejected = live.preflight_and_correct_add_stop_prices(
        [
            {"type": "ADD", "price": 93860},
            {"type": "SET_STOP", "price": 63590},
        ],
        api.mark_price,
        state.data["position"],
        config,
    )

    assert actions == [{"type": "SET_STOP", "price": 63590}]
    assert corrections == []
    assert len(rejected) == 1
    validated, stop_rejected, deduplicated = live.validate_and_deduplicate_stop_actions(
        actions, api.mark_price, "long", config
    )
    assert validated == actions
    assert stop_rejected == []
    assert deduplicated == []


def test_pair_is_not_corrected_when_long_structure_is_invalid(rig):
    engine, api, state, config = rig
    api.mark_price = Decimal("64000")
    execute(engine, "OPEN_LONG")

    actions, corrections, rejected = live.preflight_and_correct_add_stop_prices(
        [
            {"type": "ADD", "price": 93590},
            {"type": "SET_STOP", "price": 93860},
        ],
        api.mark_price,
        state.data["position"],
        config,
    )

    assert corrections == []
    assert actions == [{"type": "SET_STOP", "price": 93860}]
    assert len(rejected) == 1
    validated, stop_rejected, _ = live.validate_and_deduplicate_stop_actions(
        actions, api.mark_price, "long", config
    )
    assert validated == []
    assert len(stop_rejected) == 1


def test_short_pair_uses_mirrored_structure(rig):
    engine, api, state, config = rig
    api.mark_price = Decimal("64000")
    execute(engine, "OPEN_SHORT")

    actions, corrections, rejected = live.preflight_and_correct_add_stop_prices(
        [
            {"type": "ADD", "price": 94140},
            {"type": "SET_STOP", "price": 94410},
        ],
        api.mark_price,
        state.data["position"],
        config,
    )

    assert actions == [
        {"type": "ADD", "price": 64140},
        {"type": "SET_STOP", "price": 64410},
    ]
    assert [item["corrected_price"] for item in corrections] == [64140, 64410]
    assert rejected == []
