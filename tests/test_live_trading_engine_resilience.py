from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest


def load_scenario_support():
    name = "live_engine_scenario_support_for_resilience"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, Path("tests/test_live_trading_engine_scenarios.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scenarios = load_scenario_support()
live = scenarios.live
D = scenarios.D
BotError = scenarios.BotError
MemoryState = scenarios.MemoryState
FakeBitgetClient = scenarios.FakeBitgetClient
ScenarioEngine = scenarios.ScenarioEngine
run = scenarios.run
execute = scenarios.execute
mutations = scenarios.mutations


def make_rig(*, equity: str = "400", mark_price: str = "63900"):
    config = live.ContractConfig(
        min_trade_num=Decimal("0.0001"),
        min_trade_usdt=Decimal("5"),
        size_step=Decimal("0.0001"),
        price_step=Decimal("0.1"),
        max_leverage=Decimal("125"),
    )
    api = FakeBitgetClient(equity=equity, mark_price=mark_price)
    state = MemoryState()
    engine = ScenarioEngine(api, state, config)
    return engine, api, state, config


def set_partial_long_position(api, qty: Decimal, price: Decimal) -> None:
    api.position_row = {
        "total": live.ds(qty),
        "holdSide": "long",
        "marginSize": live.ds(qty * price / live.LEVERAGE),
        "openPriceAvg": live.ds(price),
        "breakEvenPrice": live.ds(price),
        "cTime": "1700000000000",
    }


def prepare_verified_stopped_short(engine, api, state) -> Decimal:
    execute(engine, "OPEN_SHORT")
    execute(engine, "SET_STOP", 64500, message_id=2)
    stop_id = state.data["pending"]["stop_order"]["order_id"]
    stopped_margin = D(state.data["position"]["margin_size_usdt"])
    api.trigger_stop(stop_id)
    run(engine.reconcile())
    assert state.data["stopped_position"]["available_for_reentry"] is True
    return stopped_margin


def test_full_scale_in_stop_and_reentry_reuses_eight_unit_margin():
    engine, api, state, config = make_rig()

    execute(engine, "OPEN_LONG")
    initial_qty = D(state.data["position"]["initial_qty"])
    for message_id in (2, 3, 4):
        execute(engine, "ADD", message_id=message_id)

    assert D(state.data["position"]["total_qty"]) == initial_qty * 8
    assert D(state.data["position"]["added_qty"]) == initial_qty * 7
    full_margin = D(state.data["position"]["margin_size_usdt"])

    execute(engine, "SET_STOP", 62500, message_id=5)
    stop_id = state.data["pending"]["stop_order"]["order_id"]
    api.trigger_stop(stop_id)
    run(engine.reconcile())

    stopped = state.data["stopped_position"]
    assert D(stopped["total_margin_usdt"]) == full_margin
    assert stopped["side"] == "long"
    assert stopped["available_for_reentry"] is True

    account_calls = api.account_calls
    result = execute(engine, "OPEN_REENTRY", message_id=6)
    expected_qty = config.floor_size(full_margin * live.LEVERAGE / api.mark_price)
    placed = [call for call in mutations(api) if call["method"] == "place_order"][-1]

    assert api.account_calls == account_calls
    assert result["sizing_source"] == "previous_sl_margin_usdt"
    assert D(result["margin_usdt"]) == full_margin
    assert placed["side"] == "buy"
    assert placed["size"] == expected_qty
    assert D(state.data["position"]["total_qty"]) == expected_qty
    assert state.data["stopped_position"]["available_for_reentry"] is False


def test_close_half_after_multiple_adds_reduces_added_quantity_first():
    engine, api, state, _ = make_rig()

    execute(engine, "OPEN_LONG")
    initial_qty = D(state.data["position"]["initial_qty"])
    execute(engine, "ADD", message_id=2)
    execute(engine, "ADD", message_id=3)

    assert D(state.data["position"]["total_qty"]) == initial_qty * 4
    assert D(state.data["position"]["added_qty"]) == initial_qty * 3

    execute(engine, "CLOSE_HALF", message_id=4)

    assert D(state.data["position"]["initial_qty"]) == initial_qty
    assert D(state.data["position"]["added_qty"]) == initial_qty
    assert D(state.data["position"]["total_qty"]) == initial_qty * 2
    last_order = [call for call in mutations(api) if call["method"] == "place_order"][-1]
    assert last_order["reduce_only"] is True
    assert last_order["size"] == initial_qty * 2


def test_close_adds_after_full_scale_in_leaves_only_initial_position():
    engine, api, state, _ = make_rig()

    execute(engine, "OPEN_SHORT")
    initial_qty = D(state.data["position"]["initial_qty"])
    for message_id in (2, 3, 4):
        execute(engine, "ADD", message_id=message_id)

    assert D(state.data["position"]["total_qty"]) == initial_qty * 8
    assert D(state.data["position"]["added_qty"]) == initial_qty * 7

    execute(engine, "CLOSE_ADDS", message_id=5)

    assert state.data["position"]["side"] == "short"
    assert D(state.data["position"]["initial_qty"]) == initial_qty
    assert D(state.data["position"]["added_qty"]) == 0
    assert D(state.data["position"]["total_qty"]) == initial_qty
    last_order = [call for call in mutations(api) if call["method"] == "place_order"][-1]
    assert last_order["reduce_only"] is True
    assert last_order["size"] == initial_qty * 7


def test_partial_initial_limit_fill_can_be_closed_without_leaving_remainder():
    engine, api, state, config = make_rig()

    execute(engine, "OPEN_LONG", 63000)
    pending = state.data["pending"]["entry_order"]
    full_qty = D(pending["qty"])
    partial_qty = config.floor_size(full_qty / 2)
    assert partial_qty >= config.min_trade_num

    set_partial_long_position(api, partial_qty, Decimal("63000"))
    run(engine.reconcile())

    assert D(state.data["position"]["total_qty"]) == partial_qty
    assert state.data["pending"]["entry_order"] is not None

    with pytest.raises(BotError, match="position already exists"):
        execute(engine, "OPEN_LONG", message_id=2)
    with pytest.raises(BotError, match="initial entry limit order"):
        execute(engine, "ADD", 62500, message_id=3)

    start = len(api.calls)
    execute(engine, "CLOSE_ALL", message_id=4)
    calls = mutations(api, start)

    assert [call["method"] for call in calls] == ["cancel_order", "flash_close"]
    assert api.position_row is None
    assert api.order_rows == []
    assert D(state.data["position"]["total_qty"]) == 0
    assert state.data["pending"]["entry_order"] is None


def test_reconcile_recovers_live_position_orders_and_plans_after_state_loss():
    engine, api, state, config = make_rig()

    execute(engine, "OPEN_LONG")
    execute(engine, "ADD", 63000, message_id=2)
    execute(engine, "SET_STOP", 62500, message_id=3)
    execute(engine, "SET_TP", None, message_id=4)

    live_position_qty = D(api.position_row["total"])
    pending_add_id = state.data["pending"]["add_orders"][0]["order_id"]
    stop_id = state.data["pending"]["stop_order"]["order_id"]
    tp_id = state.data["pending"]["tp_order"]["order_id"]

    recovered_state = MemoryState()
    recovered = ScenarioEngine(api, recovered_state, config)
    run(recovered.reconcile())

    assert recovered_state.data["position"]["side"] == "long"
    assert D(recovered_state.data["position"]["total_qty"]) == live_position_qty
    assert D(recovered_state.data["position"]["initial_qty"]) == live_position_qty
    assert D(recovered_state.data["position"]["added_qty"]) == 0
    assert recovered_state.data["pending"]["entry_order"] is None
    assert [x["order_id"] for x in recovered_state.data["pending"]["add_orders"]] == [
        pending_add_id
    ]
    assert recovered_state.data["pending"]["stop_order"]["order_id"] == stop_id
    assert recovered_state.data["pending"]["tp_order"]["order_id"] == tp_id


def test_market_order_accepted_then_response_lost_is_recovered_without_second_open():
    engine, api, state, _ = make_rig()
    original_place_order = api.place_order

    async def accept_then_lose_response(**kwargs):
        await original_place_order(**kwargs)
        raise BotError("Injected response loss after accepted order")

    api.place_order = accept_then_lose_response

    with pytest.raises(BotError, match="response loss"):
        execute(engine, "OPEN_LONG")

    assert api.position_row is not None
    assert D(state.data["position"]["total_qty"]) == 0

    run(engine.reconcile())
    recovered_qty = D(state.data["position"]["total_qty"])
    assert recovered_qty > 0
    assert state.data["position"]["side"] == "long"

    before = len(mutations(api))
    with pytest.raises(BotError, match="position already exists"):
        execute(engine, "OPEN_LONG", message_id=2)
    assert len(mutations(api)) == before


def test_limit_order_accepted_then_response_lost_is_recovered_as_pending_entry():
    engine, api, state, _ = make_rig()
    original_place_order = api.place_order

    async def accept_then_lose_response(**kwargs):
        await original_place_order(**kwargs)
        raise BotError("Injected response loss after accepted limit")

    api.place_order = accept_then_lose_response

    with pytest.raises(BotError, match="response loss"):
        execute(engine, "OPEN_LONG", 63000)

    assert api.position_row is None
    assert len(api.order_rows) == 1
    assert state.data["pending"]["entry_order"] is None

    run(engine.reconcile())
    recovered = state.data["pending"]["entry_order"]
    assert recovered is not None
    assert recovered["order_id"] == api.order_rows[0]["orderId"]
    assert recovered["price"] == "63000"

    before = len(mutations(api))
    with pytest.raises(BotError, match="entry order already pending"):
        execute(engine, "OPEN_LONG", 62900, message_id=2)
    assert len(mutations(api)) == before


def test_regular_open_after_verified_stop_invalidates_old_reentry_allowance():
    engine, api, state, _ = make_rig()
    prepare_verified_stopped_short(engine, api, state)

    execute(engine, "OPEN_LONG", message_id=3)

    stopped = state.data["stopped_position"]
    assert stopped["available_for_reentry"] is False
    assert stopped["consumed_reason"] == "superseded_by_new_entry"
    assert stopped["consumed_at"] is not None
