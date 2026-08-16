import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path


def load_module():
    os.environ["LIVE_TRADING"] = "0"
    path = Path(__file__).resolve().parents[1] / "reversion_live" / "main.py"
    spec = importlib.util.spec_from_file_location("reversion_live_main_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rv = load_module()


class FakeState:
    def __init__(self):
        self.data = rv.blank_state()
        self.saves = 0

    def save(self):
        self.saves += 1

    def reset_cycle(self):
        last = dict(self.data.get("last_signal", {}))
        self.data = rv.blank_state()
        self.data["last_signal"] = last
        self.save()


def test_profiles_match_validated_parameters():
    assert rv.SYMBOLS == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert rv.STRATEGY_ID == "asymmetric_snapback_v1"

    btc = rv.PROFILES["BTCUSDT"]
    assert btc.notional_mult == Decimal("5")
    assert btc.tp_atr == Decimal("8")
    assert btc.sl_atr == Decimal("6")
    assert btc.max_hold_minutes == 48 * 60
    assert btc.hard_stop == Decimal("0.20")
    assert rv.BTC_BB_Z == Decimal("2.75")
    assert rv.BTC_RSI_MIN == Decimal("65")
    assert rv.BTC_RSI_REVERSAL == Decimal("12")
    assert rv.BTC_ADX_MAX == Decimal("35")
    assert rv.BTC_ATR_RATIO_MAX == Decimal("3.0")

    eth = rv.PROFILES["ETHUSDT"]
    assert eth.notional_mult == Decimal("9")
    assert eth.tp_atr == Decimal("2")
    assert eth.sl_atr == Decimal("3")
    assert eth.max_hold_minutes == 24 * 60
    assert eth.hard_stop == Decimal("0.20")
    assert rv.ETH_BB_Z == Decimal("2.50")
    assert rv.ETH_RSI_MAX == Decimal("30")
    assert rv.ETH_RSI_REVERSAL == Decimal("11")
    assert rv.ETH_ADX_MAX == Decimal("70")
    assert rv.ETH_ATR_RATIO_MAX == Decimal("1.5")

    sol = rv.PROFILES["SOLUSDT"]
    assert sol.notional_mult == Decimal("3")
    assert sol.tp_atr == Decimal("5")
    assert sol.sl_atr == Decimal("4")
    assert sol.max_hold_minutes == 6 * 60
    assert sol.hard_stop == Decimal("0.15")
    assert rv.SOL_KELTNER_ATR == Decimal("2.25")
    assert rv.SOL_RSI_LONG_MAX == Decimal("25")
    assert rv.SOL_RSI_SHORT_MIN == Decimal("75")
    assert rv.SOL_RSI_REVERSAL == Decimal("8")
    assert rv.SOL_ADX_MAX == Decimal("35")
    assert rv.SOL_ATR_RATIO_MAX == Decimal("3.0")


def test_scan_consumes_simultaneous_signals_and_uses_priority(monkeypatch):
    state = FakeState()
    bot = rv.Bot(object(), state, {})
    opened = []
    now_ms = 1_800_000_000_000
    close_time = now_ms - 1_000

    btc_signal = rv.Signal("short", close_time, Decimal("10"), "btc")
    eth_signal = rv.Signal("long", close_time, Decimal("5"), "eth")
    signals = {
        "BTCUSDT": btc_signal,
        "ETHUSDT": eth_signal,
        "SOLUSDT": None,
    }
    monkeypatch.setattr(rv, "LIVE", True)
    monkeypatch.setattr(rv.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(rv, "signal_for", lambda api, symbol: signals[symbol])
    monkeypatch.setattr(bot, "open_signal", lambda *args: opened.append(args))

    bot.scan()

    assert opened == [("BTCUSDT", btc_signal)]
    assert state.data["last_signal"]["BTCUSDT"] == close_time
    assert state.data["last_signal"]["ETHUSDT"] == close_time


def test_scan_consumes_stale_signal_without_opening(monkeypatch):
    state = FakeState()
    bot = rv.Bot(object(), state, {})
    opened = []
    now_ms = 1_800_000_000_000
    close_time = now_ms - rv.SIGNAL_MAX_AGE_MS - 1

    signals = {
        "BTCUSDT": None,
        "ETHUSDT": rv.Signal("long", close_time, Decimal("5"), "eth"),
        "SOLUSDT": None,
    }
    monkeypatch.setattr(rv, "LIVE", True)
    monkeypatch.setattr(rv.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(rv, "signal_for", lambda api, symbol: signals[symbol])
    monkeypatch.setattr(bot, "open_signal", lambda *args: opened.append(args))

    bot.scan()

    assert opened == []
    assert state.data["last_signal"]["ETHUSDT"] == close_time


def test_occupied_bar_is_consumed_for_every_symbol(monkeypatch):
    state = FakeState()
    bot = rv.Bot(object(), state, {})
    now_ms = 1_800_000_000_000
    expected = rv.latest_completed_15m_close_time(now_ms)

    monkeypatch.setattr(rv.time, "time", lambda: now_ms / 1000)
    bot.consume_occupied_bar()

    assert state.data["last_signal"] == {
        "BTCUSDT": expected,
        "ETHUSDT": expected,
        "SOLUSDT": expected,
    }
