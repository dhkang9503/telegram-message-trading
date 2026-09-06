import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest


def load_module():
    path = Path(__file__).resolve().parents[1] / "reversion_live" / "main.py"
    spec = importlib.util.spec_from_file_location("reversion_bb_cci_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rv = load_module()


def rules():
    return rv.SymbolRules(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        market_step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("100"),
        min_notional=Decimal("5"),
    )


def config(tmp_path, live=False):
    return rv.Config(
        live_trading=live,
        api_key="key" if live else "",
        api_secret="secret" if live else "",
        symbol="BTCUSDT",
        rest_url="https://example.test",
        poll_seconds=3.0,
        http_timeout_seconds=10.0,
        recv_window_ms=5000,
        log_path=tmp_path / ("live.jsonl" if live else "paper.jsonl"),
        state_path=tmp_path / ("live-state.json" if live else "paper-state.json"),
        paper_initial_equity=Decimal("10000"),
        paper_maker_fee=Decimal("0.0002"),
        paper_taker_fee=Decimal("0.0006"),
        paper_market_slippage=Decimal("0.0001"),
    )


def candle(open_time, close=100.0, high=None, low=None, open_price=None):
    return rv.Candle(
        open_time_ms=open_time,
        close_time_ms=open_time + 59_999,
        open=close if open_price is None else open_price,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=1.0,
    )


class Journal:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class Store:
    def __init__(self):
        self.saved = []

    def save(self, state):
        self.saved.append(json.loads(json.dumps(state)))


class NoPrivateAPI:
    def __getattr__(self, name):
        raise AssertionError(f"paper mode attempted API operation: {name}")


def make_bot(tmp_path, *, live=False, api=None, state=None):
    cfg = config(tmp_path, live)
    if state is None:
        state = rv.StateStore(cfg.state_path, live, cfg.paper_initial_equity).default()
    return rv.StrategyBot(
        cfg,
        NoPrivateAPI() if api is None else api,
        rules(),
        Journal(),
        Store(),
        state,
    )


def test_config_defaults_to_paper_without_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVERSION_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    cfg = rv.Config.from_env()

    assert cfg.live_trading is False
    assert cfg.api_key == ""
    assert cfg.log_path == tmp_path / "paper.jsonl"


def test_live_config_requires_both_credentials(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "1")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    with pytest.raises(ValueError, match="BINANCE_API_KEY"):
        rv.Config.from_env()


def test_live_flag_is_strict(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")

    with pytest.raises(ValueError, match="exactly 0 or 1"):
        rv.Config.from_env()


def test_indicator_formulas_use_adjust_false_population_std_and_mean_deviation():
    values = [10.0, 11.0, 12.0]
    assert rv.ema_adjust_false(values, 3) == [10.0, 10.5, 11.25]

    bars = [candle(i * 60_000, close=value) for i, value in enumerate(values)]
    z = rv.bb_z_series(bars, window=3)[-1]
    expected_z = (12.0 - 11.0) / ((2.0 / 3.0) ** 0.5)
    assert z == pytest.approx(expected_z)

    cci = rv.cci_series(bars, window=3)[-1]
    mean_deviation = (1.0 + 0.0 + 1.0) / 3.0
    assert cci == pytest.approx((12.0 - 11.0) / (0.015 * mean_deviation))


def test_signal_gates_are_strict_and_directional(monkeypatch):
    bars = [candle(i * 60_000, close=101.0) for i in range(1440)]
    monkeypatch.setattr(rv, "ema_adjust_false", lambda values, span: [100.0] * 1440)
    monkeypatch.setattr(
        rv,
        "cci_series",
        lambda values: [None] * 1438 + [-101.0, -99.0],
    )
    monkeypatch.setattr(
        rv,
        "bb_z_series",
        lambda values: [None] * 1430 + [-1.6] + [0.0] * 9,
    )

    signal = rv.calculate_signal(bars)

    assert signal is not None
    assert signal.side == "long"


def test_signal_requires_full_1440_bar_warmup():
    bars = [candle(i * 60_000, close=100.0) for i in range(1439)]

    assert rv.calculate_signal(bars) is None


def test_history_initialization_rejects_a_one_minute_data_gap(tmp_path):
    bot = make_bot(tmp_path)
    bars = [candle(i * 60_000) for i in range(1440)]
    bars[700] = candle(701 * 60_000)

    with pytest.raises(rv.SafetyHalt, match="not contiguous"):
        bot.initialize_history(bars)


def test_risk_sizing_caps_scheduled_loss_at_three_percent_before_fees():
    qty = rv.risk_quantity(Decimal("10000"), Decimal("100000"), "long", rules())

    assert qty == Decimal("0.085")
    assert qty * Decimal("100000") * rv.STOP_RATE <= Decimal("300")
    assert qty * Decimal("100000") == Decimal("8500")

    rounded_qty = rv.risk_quantity(
        Decimal("10000"), Decimal("100.07"), "long", rules()
    )
    _, rounded_stop = rv.exit_prices_for("long", Decimal("100.07"), Decimal("0.1"))
    assert rounded_qty * abs(Decimal("100.07") - rounded_stop) <= Decimal("300")


def test_directional_price_rounding():
    assert rv.entry_price_for("long", Decimal("100.07"), Decimal("0.1")) == Decimal(
        "100.0"
    )
    assert rv.entry_price_for("short", Decimal("100.07"), Decimal("0.1")) == Decimal(
        "100.2"
    )
    assert rv.exit_prices_for("long", Decimal("100"), Decimal("0.1")) == (
        Decimal("100.6"),
        Decimal("96.5"),
    )


def test_paper_limit_needs_one_bp_penetration_and_never_calls_private_api(tmp_path):
    bot = make_bot(tmp_path)
    bot.state.update(
        status="pending",
        pending={
            "side": "long",
            "qty": "1",
            "limit_price": "100",
            "signal_bar_open_time_ms": 0,
            "placed_at_ms": 59_999,
            "expires_at_ms": 20 * 60_000,
        },
    )

    bot._paper_bar(
        candle(60_000, close=100, high=100.5, low=99.99, open_price=100.1),
        allow_signal=False,
    )
    assert bot.state["status"] == "pending"

    bot._paper_bar(candle(120_000, close=100, high=100.5, low=99.98), allow_signal=False)
    assert bot.state["status"] == "position"


def test_paper_rejects_marketable_post_only_entry_at_next_bar_open(tmp_path):
    bot = make_bot(tmp_path)
    bot.state.update(
        status="pending",
        pending={
            "side": "long",
            "qty": "1",
            "limit_price": "100",
            "signal_bar_open_time_ms": 0,
            "placed_at_ms": 59_999,
            "expires_at_ms": 20 * 60_000,
        },
    )

    bot._paper_bar(
        candle(60_000, close=99, high=100, low=98, open_price=99.9),
        allow_signal=False,
    )

    assert bot.state["status"] == "flat"
    assert bot.journal.events[-1][0] == "PAPER_ENTRY_REJECTED"


def test_paper_fill_bar_blocks_target_but_allows_stop_and_stop_wins(tmp_path):
    bot = make_bot(tmp_path)
    bot.state.update(
        status="pending",
        pending={
            "side": "long",
            "qty": "1",
            "limit_price": "100",
            "signal_bar_open_time_ms": 0,
            "placed_at_ms": 59_999,
            "expires_at_ms": 20 * 60_000,
        },
    )

    bot._paper_bar(
        candle(60_000, close=100, high=101, low=96, open_price=100.1),
        allow_signal=False,
    )

    assert bot.state["status"] == "flat"
    assert bot.journal.events[-1][0] == "PAPER_EXIT"
    assert bot.journal.events[-1][1]["reason"] == "stop"


def test_paper_fill_bar_does_not_take_same_bar_target(tmp_path):
    bot = make_bot(tmp_path)
    bot.state.update(
        status="pending",
        pending={
            "side": "long",
            "qty": "1",
            "limit_price": "100",
            "signal_bar_open_time_ms": 0,
            "placed_at_ms": 59_999,
            "expires_at_ms": 20 * 60_000,
        },
    )

    bot._paper_bar(
        candle(60_000, close=100, high=101, low=99.98, open_price=100.1),
        allow_signal=False,
    )
    assert bot.state["status"] == "position"

    bot._paper_bar(candle(120_000, close=100.6, high=100.6, low=100), allow_signal=False)
    assert bot.state["status"] == "position"

    bot._paper_bar(candle(180_000, close=100.62, high=100.62, low=100), allow_signal=False)
    assert bot.state["status"] == "flat"
    assert bot.journal.events[-1][1]["reason"] == "target"


def test_paper_stop_gap_uses_worse_open_before_slippage(tmp_path):
    bot = make_bot(tmp_path)
    bot.state.update(
        status="position",
        position={
            "side": "long",
            "qty": "1",
            "entry_price": "100",
            "entry_time_ms": 0,
            "entry_bar_open_time_ms": 0,
            "target_price": "100.6",
            "stop_price": "96.5",
        },
    )

    bot._paper_bar(
        candle(60_000, close=95, high=95.5, low=94, open_price=95),
        allow_signal=False,
    )

    assert bot.journal.events[-1][1]["reason"] == "stop"
    assert Decimal(bot.journal.events[-1][1]["exit_price"]) == Decimal("94.9905")


class FakeLiveAPI:
    def __init__(self):
        self.calls = []
        self.clock = 1_000_000
        self.account_payload = {
            "totalMarginBalance": "10000",
            "availableBalance": "10000",
            "positions": [],
        }
        self.regular = []
        self.algos = []

    def now_ms(self):
        return self.clock

    def account(self):
        self.calls.append(("account", {}))
        return self.account_payload

    def position_risk(self, symbol=None):
        self.calls.append(("position_risk", {"symbol": symbol}))
        return list(self.account_payload["positions"])

    def open_orders(self, symbol=None):
        self.calls.append(("open_orders", {"symbol": symbol}))
        return list(self.regular)

    def open_algo_orders(self, symbol=None):
        self.calls.append(("open_algo_orders", {"symbol": symbol}))
        return list(self.algos)

    def new_order(self, **params):
        self.calls.append(("new_order", params))
        return {"orderId": 123, "status": "NEW"}

    def new_algo_order(self, **params):
        self.calls.append(("new_algo_order", params))
        return {"algoId": 456, "algoStatus": "NEW"}

    def cancel_order(self, symbol, order_id):
        self.calls.append(("cancel_order", {"symbol": symbol, "order_id": order_id}))
        return {}

    def cancel_algo_order(self, symbol, algo_id):
        self.calls.append(("cancel_algo", {"symbol": symbol, "algo_id": algo_id}))
        return {}

    def position_mode(self):
        return {"dualSidePosition": False}

    def multi_assets_mode(self):
        return {"multiAssetsMargin": False}

    def symbol_config(self, symbol):
        return {"symbol": symbol, "marginType": "ISOLATED", "leverage": 1}

    def set_margin_type(self, symbol):
        self.calls.append(("margin", {"symbol": symbol}))
        return {"code": 200}

    def set_leverage(self, symbol, leverage):
        self.calls.append(("leverage", {"symbol": symbol, "leverage": leverage}))
        return {"symbol": symbol, "leverage": leverage}


def test_live_entry_is_post_only_and_has_no_market_fallback(tmp_path):
    api = FakeLiveAPI()
    bot = make_bot(tmp_path, live=True, api=api)
    signal = rv.Signal("long", -2.0, -120.0, -90.0, 99.0)

    bot._place_live_entry(candle(0, close=100_000), signal)

    call = next(params for name, params in api.calls if name == "new_order")
    assert call["type"] == "LIMIT"
    assert call["timeInForce"] == "GTX"
    assert call["side"] == "BUY"
    assert Decimal(call["quantity"]) * Decimal(call["price"]) * rv.STOP_RATE <= Decimal(
        "300"
    )


def test_live_protection_places_exchange_stop_before_post_only_target(tmp_path):
    api = FakeLiveAPI()
    bot = make_bot(tmp_path, live=True, api=api)
    bot.state.update(
        status="position",
        position={
            "side": "long",
            "qty": "0.085",
            "entry_price": "100000",
            "entry_time_ms": 0,
            "target_price": "100600",
            "stop_price": "96500",
            "entry_equity": "10000",
            "target_order_id": None,
            "stop_algo_id": None,
        },
    )

    bot._ensure_live_protection([], [])

    order_calls = [(name, params) for name, params in api.calls if name.startswith("new_")]
    assert [name for name, _ in order_calls] == ["new_algo_order", "new_order"]
    stop = order_calls[0][1]
    assert stop["algoType"] == "CONDITIONAL"
    assert stop["type"] == "STOP_MARKET"
    assert stop["closePosition"] == "true"
    assert "quantity" not in stop
    assert "reduceOnly" not in stop
    target = order_calls[1][1]
    assert target["type"] == "LIMIT"
    assert target["timeInForce"] == "GTX"
    assert target["reduceOnly"] == "true"


def test_live_bootstrap_fails_closed_on_unowned_order(tmp_path):
    api = FakeLiveAPI()
    api.regular = [{"clientOrderId": "manual-order", "orderId": 1}]
    bot = make_bot(tmp_path, live=True, api=api)

    with pytest.raises(rv.SafetyHalt, match="Dedicated-account"):
        bot.bootstrap_live()


def test_live_bootstrap_configures_empty_account_to_isolated_one_x(tmp_path):
    api = FakeLiveAPI()
    bot = make_bot(tmp_path, live=True, api=api)

    bot.bootstrap_live()

    assert bot.state["status"] == "flat"
    assert ("margin", {"symbol": "BTCUSDT"}) in api.calls
    assert (
        "leverage",
        {"symbol": "BTCUSDT", "leverage": 1},
    ) in api.calls


def test_live_bootstrap_recovers_v3_position_and_places_protection(tmp_path):
    api = FakeLiveAPI()
    api.account_payload["positions"] = [
        {
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "0.085",
            "entryPrice": "100000",
            "updateTime": 900_000,
        }
    ]
    bot = make_bot(tmp_path, live=True, api=api)

    bot.bootstrap_live()

    assert bot.state["status"] == "position"
    assert bot.state["position"]["entry_price"] == "100000"
    assert [name for name, _ in api.calls if name.startswith("new_")] == [
        "new_algo_order",
        "new_order",
    ]


def test_api_wrappers_use_current_algo_endpoints(tmp_path):
    cfg = config(tmp_path, live=True)

    class RecordingClient(rv.BinanceClient):
        def __init__(self):
            super().__init__(cfg)
            self.requests = []

        def _request(self, method, path, params=None, *, signed=False):
            self.requests.append((method, path, params, signed))
            if path == "/fapi/v1/symbolConfig":
                return [{}]
            return [] if method == "GET" else {}

    api = RecordingClient()
    api.open_algo_orders("BTCUSDT")
    api.new_algo_order(symbol="BTCUSDT", type="STOP_MARKET")
    api.cancel_algo_order("BTCUSDT", 7)
    api.position_risk("BTCUSDT")
    api.symbol_config("BTCUSDT")
    api.multi_assets_mode()

    assert [(item[0], item[1]) for item in api.requests] == [
        ("GET", "/fapi/v1/openAlgoOrders"),
        ("POST", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/algoOrder"),
        ("GET", "/fapi/v3/positionRisk"),
        ("GET", "/fapi/v1/symbolConfig"),
        ("GET", "/fapi/v1/multiAssetsMargin"),
    ]
    assert all(item[3] is True for item in api.requests)


def test_state_store_uses_separate_mode_and_atomic_reload(tmp_path):
    path = tmp_path / "paper-state.json"
    store = rv.StateStore(path, False, Decimal("1234"))
    state = store.default()
    state["status"] = "pending"
    store.save(state)

    restored = store.load()

    assert restored["paper_equity"] == "1234"
    assert restored["status"] == "pending"
    assert not path.with_name(path.name + ".tmp").exists()
