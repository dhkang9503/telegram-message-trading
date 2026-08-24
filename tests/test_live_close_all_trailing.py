from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest


def load_scenario_support():
    name = "live_engine_scenario_support_for_close_all_trailing"
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


@pytest.fixture
def rig():
    config = live.ContractConfig(
        min_trade_num=Decimal("0.0001"),
        min_trade_usdt=Decimal("5"),
        size_step=Decimal("0.0001"),
        price_step=Decimal("0.1"),
        max_leverage=Decimal("125"),
    )
    api = FakeBitgetClient()
    state = MemoryState()
    return ScenarioEngine(api, state, config), api, state


def set_roe(api, roe_pct: str) -> None:
    assert api.position_row is not None
    margin = D(api.position_row["marginSize"])
    api.position_row["unrealizedPL"] = live.ds(
        margin * Decimal(roe_pct) / Decimal("100")
    )


def arm_trailing(engine, api, *, roe_pct: str = "20"):
    execute(engine, "OPEN_LONG")
    set_roe(api, roe_pct)
    return execute(engine, "CLOSE_ALL", message_id=2)


def test_roe_prefers_unrealized_pl_over_margin():
    roe, source = live.position_roe_percent(
        {
            "unrealizedPL": "2",
            "marginSize": "10",
            "openPriceAvg": "100",
            "markPrice": "1",
            "leverage": "98",
            "holdSide": "long",
        }
    )
    assert roe == Decimal("20")
    assert source == "unrealized_pl_over_margin"


def test_bitget_pending_plans_merges_profit_loss_and_track_plan():
    class RecordingClient(live.BitgetClient):
        def __init__(self):
            self.calls = []

        async def request(self, method, path, *, params=None, body=None, private=True):
            self.calls.append({"method": method, "path": path, "params": params})
            plan_type = params["planType"]
            row = {
                "orderId": f"{plan_type}-1",
                "planType": "moving_plan" if plan_type == "track_plan" else "pos_loss",
            }
            return {"entrustedList": [row]}

    client = RecordingClient()

    rows = run(client.pending_plans())

    assert {call["params"]["planType"] for call in client.calls} == {
        "profit_loss",
        "track_plan",
    }
    assert {row["planType"] for row in rows} == {"pos_loss", "moving_plan"}


def test_bitget_cancel_plan_uses_moving_plan_and_requires_success_confirmation():
    class RecordingClient(live.BitgetClient):
        def __init__(self):
            self.calls = []

        async def request(self, method, path, *, params=None, body=None, private=True):
            self.calls.append({"method": method, "path": path, "body": body})
            return {
                "successList": [{"orderId": "trailing-1", "clientOid": ""}],
                "failureList": [],
            }

    client = RecordingClient()

    result = run(client.cancel_plan("trailing-1", "moving_plan"))

    assert result["successList"][0]["orderId"] == "trailing-1"
    assert client.calls == [
        {
            "method": "POST",
            "path": "/api/v2/mix/order/cancel-plan-order",
            "body": {
                "orderIdList": [{"orderId": "trailing-1", "clientOid": ""}],
                "symbol": live.SYMBOL,
                "productType": live.PRODUCT_TYPE,
                "marginCoin": live.MARGIN_COIN,
                "planType": "moving_plan",
            },
        }
    ]


def test_bitget_cancel_plan_rejects_failure_list_response():
    class FailingClient(live.BitgetClient):
        def __init__(self):
            pass

        async def request(self, method, path, *, params=None, body=None, private=True):
            return {
                "successList": [],
                "failureList": [
                    {"orderId": "trailing-1", "errorMsg": "plan type mismatch"}
                ],
            }

    with pytest.raises(BotError, match="plan type mismatch"):
        run(FailingClient().cancel_plan("trailing-1", "moving_plan"))


@pytest.mark.parametrize(
    ("side", "mark", "expected"),
    [("long", "102", "196"), ("short", "98", "196")],
)
def test_roe_uses_leveraged_price_return_as_fallback(side, mark, expected):
    roe, source = live.position_roe_percent(
        {
            "openPriceAvg": "100",
            "markPrice": mark,
            "leverage": "98",
            "holdSide": side,
        }
    )
    assert roe == Decimal(expected)
    assert source == "price_return_times_leverage"


@pytest.mark.parametrize("roe_pct", ["19.9999", "-5"])
def test_close_all_below_threshold_market_closes(rig, roe_pct):
    engine, api, state = rig
    execute(engine, "OPEN_LONG")
    set_roe(api, roe_pct)
    start = len(api.calls)

    result = execute(engine, "CLOSE_ALL", message_id=2)

    assert result == {"result": {"closed": True}}
    assert [call["method"] for call in mutations(api, start)] == ["flash_close"]
    assert api.position_row is None
    assert state.data["pending"]["trailing_order"] is None


def test_close_half_dust_conversion_does_not_arm_close_all_trailing(rig):
    engine, api, state = rig
    execute(engine, "OPEN_LONG")
    api.position_row["total"] = "0.0001"
    api.position_row["marginSize"] = live.ds(
        Decimal("0.0001") * api.mark_price / live.LEVERAGE
    )
    set_roe(api, "50")
    run(engine.reconcile())
    start = len(api.calls)

    execute(engine, "CLOSE_HALF", message_id=2)

    assert [call["method"] for call in mutations(api, start)] == ["flash_close"]
    assert api.position_row is None
    assert state.data["pending"]["trailing_order"] is None


def test_close_all_at_threshold_arms_full_size_trailing(rig):
    engine, api, state = rig
    execute(engine, "OPEN_LONG")
    set_roe(api, "20")
    expected_qty = D(api.position_row["total"])
    start = len(api.calls)

    result = execute(engine, "CLOSE_ALL", message_id=2)

    calls = mutations(api, start)
    assert [call["method"] for call in calls] == [
        "place_plan",
        "place_trailing_plan",
    ]
    guard, trailing = calls
    assert guard["plan_type"] == "pos_loss"
    assert guard["trigger_price"] == Decimal("63836.1")
    assert trailing["size"] == expected_qty
    assert trailing["trigger_price"] == api.mark_price
    assert trailing["range_rate"] == Decimal("0.10")
    assert result["trailing"] is True
    assert result["activated"] is True
    assert result["guard_trigger_price"] == "63836.1"
    assert result["roe_pct"] == "20"
    assert api.position_row is not None
    assert state.data["pending"]["stop_order"]["source"] == "close_all_trailing_guard"
    assert state.data["pending"]["trailing_order"]["plan_type"] == "moving_plan"


def test_short_trailing_uses_valid_nearest_trigger_and_guard_above_market(rig):
    engine, api, _ = rig
    api.mark_price = Decimal("63900.05")
    execute(engine, "OPEN_SHORT")
    set_roe(api, "20")

    execute(engine, "CLOSE_ALL", message_id=2)

    trailing = [
        call for call in mutations(api) if call["method"] == "place_trailing_plan"
    ][0]
    guard = [call for call in mutations(api) if call["method"] == "place_plan"][-1]
    assert trailing["trigger_price"] == Decimal("63900.0")
    assert guard["trigger_price"] == Decimal("63964.0")


def test_long_trailing_uses_valid_nearest_trigger_and_guard_below_market(rig):
    engine, api, _ = rig
    api.mark_price = Decimal("63900.05")
    execute(engine, "OPEN_LONG")
    set_roe(api, "20")

    execute(engine, "CLOSE_ALL", message_id=2)

    trailing = [
        call for call in mutations(api) if call["method"] == "place_trailing_plan"
    ][0]
    guard = [call for call in mutations(api) if call["method"] == "place_plan"][-1]
    assert trailing["trigger_price"] == Decimal("63900.1")
    assert guard["trigger_price"] == Decimal("63836.1")


def test_close_all_replaces_stop_with_guard_and_cancels_tp(rig):
    engine, api, state = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "SET_STOP", 63000, message_id=2)
    execute(engine, "SET_TP", None, message_id=3)
    set_roe(api, "25")
    start = len(api.calls)

    execute(engine, "CLOSE_ALL", message_id=4)

    assert [call["method"] for call in mutations(api, start)] == [
        "cancel_plan",
        "place_plan",
        "place_trailing_plan",
        "cancel_plan",
    ]
    placed = mutations(api, start)[2]
    assert set(placed["existing_plan_types"]) == {"pos_loss", "pos_profit"}
    assert {row["planType"] for row in api.plan_rows} == {"pos_loss", "moving_plan"}
    assert state.data["pending"]["stop_order"]["source"] == "close_all_trailing_guard"
    assert state.data["pending"]["tp_order"] is None


def test_adverse_move_before_activation_leaves_guard_and_trailing_registered():
    class AdverseMoveClient(FakeBitgetClient):
        async def place_trailing_plan(self, **kwargs):
            result = await super().place_trailing_plan(**kwargs)
            self.mark_price -= Decimal("0.2")
            return result

    config = live.ContractConfig(
        min_trade_num=Decimal("0.0001"),
        min_trade_usdt=Decimal("5"),
        size_step=Decimal("0.0001"),
        price_step=Decimal("0.1"),
        max_leverage=Decimal("125"),
    )
    api = AdverseMoveClient()
    state = MemoryState()
    engine = ScenarioEngine(api, state, config)
    execute(engine, "OPEN_LONG")
    execute(engine, "SET_STOP", 63000, message_id=2)
    execute(engine, "SET_TP", None, message_id=3)
    set_roe(api, "25")
    start = len(api.calls)

    result = execute(engine, "CLOSE_ALL", message_id=4)

    calls = mutations(api, start)
    assert result["trailing"] is True
    assert result["activated"] is False
    assert [call["method"] for call in calls] == [
        "cancel_plan",
        "place_plan",
        "place_trailing_plan",
        "cancel_plan",
    ]
    assert set(calls[2]["existing_plan_types"]) == {"pos_loss", "pos_profit"}
    assert api.position_row is not None
    assert {row["planType"] for row in api.plan_rows} == {"pos_loss", "moving_plan"}


def test_trailing_registration_failure_falls_back_to_market_close(rig):
    engine, api, state = rig
    execute(engine, "OPEN_LONG")
    set_roe(api, "30")
    api.fail_next_place_trailing = BotError("injected trailing failure")

    result = execute(engine, "CLOSE_ALL", message_id=2)

    assert result == {"result": {"closed": True}}
    assert [call["method"] for call in mutations(api, 1)] == [
        "place_plan",
        "place_trailing_plan",
        "cancel_plan",
        "flash_close",
    ]
    assert api.position_row is None
    assert state.data["pending"]["trailing_order"] is None
    assert any(
        event["event"] == "CLOSE_ALL_TRAILING_FAILED_MARKET_FALLBACK"
        for event in engine.events
    )


def test_trailing_failure_keeps_existing_protection_until_market_fallback(rig):
    engine, api, _ = rig
    execute(engine, "OPEN_LONG")
    execute(engine, "SET_STOP", 63000, message_id=2)
    execute(engine, "SET_TP", None, message_id=3)
    set_roe(api, "30")
    api.fail_next_place_trailing = BotError("injected trailing failure")
    start = len(api.calls)

    execute(engine, "CLOSE_ALL", message_id=4)

    calls = mutations(api, start)
    assert [call["method"] for call in calls] == [
        "cancel_plan",
        "place_plan",
        "place_trailing_plan",
        "cancel_plan",
        "cancel_plan",
        "flash_close",
    ]
    assert set(calls[2]["existing_plan_types"]) == {"pos_loss", "pos_profit"}
    assert api.position_row is None
    assert api.plan_rows == []


def test_unconfirmed_trailing_is_cancelled_before_market_fallback(rig):
    engine, api, state = rig

    class UnconfirmedTrailingEngine(ScenarioEngine):
        async def wait_for_trailing_plan(self, order_id):
            raise BotError(f"injected missing confirmation: {order_id}")

    engine = UnconfirmedTrailingEngine(api, state, engine.config)
    execute(engine, "OPEN_LONG")
    set_roe(api, "20")
    start = len(api.calls)

    execute(engine, "CLOSE_ALL", message_id=2)

    assert [call["method"] for call in mutations(api, start)] == [
        "place_plan",
        "place_trailing_plan",
        "cancel_plan",
        "cancel_plan",
        "flash_close",
    ]
    assert api.position_row is None
    assert api.plan_rows == []
    assert state.data["pending"]["trailing_order"] is None


def test_duplicate_close_all_is_ignored_while_trailing(rig):
    engine, api, _ = rig
    first = arm_trailing(engine, api)
    start = len(api.calls)

    result = execute(engine, "CLOSE_ALL", message_id=3)

    assert result == {
        "skipped": "trailing_close_active",
        "action_type": "CLOSE_ALL",
        "trailing_order_id": first["order_id"],
    }
    assert mutations(api, start) == []
    assert api.position_row is not None


@pytest.mark.parametrize(
    "action_type",
    [
        "ADD",
        "SET_STOP",
        "SET_TP",
        "CLOSE_HALF",
        "CLOSE_ADDS",
        "CANCEL_ADD",
        "CANCEL_STOP",
    ],
)
def test_non_directional_actions_are_ignored_while_trailing(rig, action_type):
    engine, api, _ = rig
    arm_trailing(engine, api)
    start = len(api.calls)

    result = execute(engine, action_type, message_id=3)

    assert result["skipped"] == "trailing_close_active"
    assert result["action_type"] == action_type
    assert mutations(api, start) == []
    assert api.position_row is not None


def test_open_reentry_is_explicitly_rejected_during_profitable_trailing(rig):
    engine, api, _ = rig
    arm_trailing(engine, api)
    start = len(api.calls)

    result = execute(engine, "OPEN_REENTRY", message_id=3)

    assert result["skipped"] == "trailing_reentry_requires_verified_stop"
    assert mutations(api, start) == []
    assert api.position_row is not None
    assert any(
        event["event"] == "OPEN_REENTRY_REJECTED_DURING_TRAILING"
        for event in engine.events
    )


@pytest.mark.parametrize(
    ("initial_action", "replacement_action", "expected_side"),
    [
        ("OPEN_LONG", "OPEN_SHORT", "short"),
        ("OPEN_SHORT", "OPEN_LONG", "long"),
    ],
)
def test_directional_open_closes_trailing_position_then_opens_new_one(
    rig, initial_action, replacement_action, expected_side
):
    engine, api, state = rig
    execute(engine, initial_action)
    set_roe(api, "20")
    execute(engine, "CLOSE_ALL", message_id=2)
    trailing_id = state.data["pending"]["trailing_order"]["order_id"]
    start = len(api.calls)

    result = execute(engine, replacement_action, message_id=3)

    calls = mutations(api, start)
    assert [call["method"] for call in calls] == [
        "cancel_plan",
        "cancel_plan",
        "flash_close",
        "place_order",
    ]
    assert calls[1]["order_id"] == trailing_id
    assert calls[1]["plan_type"] == "moving_plan"
    assert calls[3]["reduce_only"] is False
    assert result["transition"] == "trailing_replaced_by_open"
    assert api.position_row["holdSide"] == expected_side
    assert state.data["position"]["side"] == expected_side
    assert state.data["pending"]["trailing_order"] is None


@pytest.mark.parametrize(
    ("initial_action", "replacement_action", "stop_price", "expected_side"),
    [
        ("OPEN_LONG", "OPEN_SHORT", 64500, "short"),
        ("OPEN_SHORT", "OPEN_LONG", 63300, "long"),
    ],
)
def test_replacement_batch_validates_and_sets_stop_for_new_position_side(
    rig, initial_action, replacement_action, stop_price, expected_side
):
    engine, api, state = rig
    execute(engine, initial_action)
    set_roe(api, "20")
    execute(engine, "CLOSE_ALL", message_id=2)
    actions = [
        {"type": "SET_STOP", "price": stop_price},
        {"type": replacement_action, "price": None},
    ]

    replacement_side = live.trailing_replacement_side(actions, True)
    validated, rejected, _ = live.validate_and_deduplicate_stop_actions(
        actions, api.mark_price, replacement_side, engine.config
    )
    ordered = live.prioritize_cancel_actions(
        live.prioritize_trailing_replacement_open(validated, True)
    )
    for index, action in enumerate(ordered):
        run(engine.execute(action, 3, index, "replacement with stop"))

    assert rejected == []
    assert [action["type"] for action in ordered] == [
        replacement_action,
        "SET_STOP",
    ]
    assert state.data["position"]["side"] == expected_side
    assert state.data["pending"]["stop_order"]["price"] == str(stop_price)
    assert state.data["pending"]["trailing_order"] is None


def test_priced_replacement_is_rejected_before_touching_active_trailing(rig):
    engine, api, state = rig
    arm_trailing(engine, api)
    trailing_id = state.data["pending"]["trailing_order"]["order_id"]
    actions = [
        {"type": "SET_STOP", "price": 64500},
        {"type": "OPEN_SHORT", "price": 63800},
    ]
    start = len(api.calls)

    assert live.trailing_replacement_side(actions, True) is None
    assert live.prioritize_trailing_replacement_open(actions, True) == actions
    with pytest.raises(BotError, match="Priced OPEN is not supported"):
        run(engine.execute(actions[1], 3, 1, "priced replacement"))

    assert mutations(api, start) == []
    assert api.position_row is not None
    assert state.data["pending"]["trailing_order"]["order_id"] == trailing_id


def test_missing_exchange_trailing_plan_forces_market_close(rig):
    engine, api, state = rig
    arm_trailing(engine, api)
    api.plan_rows = []
    start = len(api.calls)

    run(engine.reconcile())

    assert [call["method"] for call in mutations(api, start)] == ["flash_close"]
    assert api.position_row is None
    assert state.data["pending"]["trailing_order"] is None
    assert any(
        event["event"] == "TRAILING_STATE_MISSING_MARKET_CLOSED"
        for event in engine.events
    )


def test_trailing_fill_does_not_create_stop_reentry_eligibility(rig):
    engine, api, state = rig
    arm_trailing(engine, api)
    api.position_row = None
    api.plan_rows = []

    run(engine.reconcile())

    assert state.data["stopped_position"] is None
    assert D(state.data["position"]["total_qty"]) == 0
    assert state.data["pending"]["trailing_order"] is None


def test_unconfirmed_guard_is_cancelled_before_market_fallback(rig):
    engine, api, state = rig

    class UnconfirmedGuardEngine(ScenarioEngine):
        async def wait_for_stop_plan(self, order_id):
            raise BotError(f"injected missing guard confirmation: {order_id}")

    engine = UnconfirmedGuardEngine(api, state, engine.config)
    execute(engine, "OPEN_LONG")
    set_roe(api, "20")
    start = len(api.calls)

    result = execute(engine, "CLOSE_ALL", message_id=2)

    assert result == {"result": {"closed": True}}
    assert [call["method"] for call in mutations(api, start)] == [
        "place_plan",
        "cancel_plan",
        "flash_close",
    ]
    assert api.position_row is None
    assert api.plan_rows == []
    assert state.data["pending"]["stop_order"] is None


def test_guard_fill_cancels_remaining_trailing_without_reentry_eligibility(rig):
    engine, api, state = rig
    arm_trailing(engine, api)
    guard_id = state.data["pending"]["stop_order"]["order_id"]
    trailing_id = state.data["pending"]["trailing_order"]["order_id"]
    api.trigger_stop(guard_id)
    start = len(api.calls)

    run(engine.reconcile())

    calls = mutations(api, start)
    assert [call["method"] for call in calls] == ["cancel_plan"]
    assert calls[0]["order_id"] == trailing_id
    assert calls[0]["plan_type"] == "moving_plan"
    assert api.plan_rows == []
    assert state.data["stopped_position"] is None
    assert D(state.data["position"]["total_qty"]) == 0


def test_trailing_fill_cancels_remaining_guard_without_reentry_eligibility(rig):
    engine, api, state = rig
    arm_trailing(engine, api)
    guard_id = state.data["pending"]["stop_order"]["order_id"]
    trailing_id = state.data["pending"]["trailing_order"]["order_id"]
    api.plan_rows = [
        row for row in api.plan_rows if str(row["orderId"]) != trailing_id
    ]
    api.position_row = None
    start = len(api.calls)

    run(engine.reconcile())

    calls = mutations(api, start)
    assert [call["method"] for call in calls] == ["cancel_plan"]
    assert calls[0]["order_id"] == guard_id
    assert calls[0]["plan_type"] == "pos_loss"
    assert api.plan_rows == []
    assert state.data["stopped_position"] is None


def test_reconcile_recovers_exchange_trailing_plan_after_local_state_loss(rig):
    engine, api, _ = rig
    result = arm_trailing(engine, api)
    recovered_state = MemoryState()
    recovered = ScenarioEngine(api, recovered_state, engine.config)

    run(recovered.reconcile())

    trailing = recovered_state.data["pending"]["trailing_order"]
    guard = recovered_state.data["pending"]["stop_order"]
    assert trailing["order_id"] == result["order_id"]
    assert trailing["plan_type"] == "moving_plan"
    assert trailing["callback_rate_pct"] == "0.1"
    assert guard["order_id"] == result["guard_stop_order_id"]
    assert guard["source"] == "close_all_trailing_guard"
