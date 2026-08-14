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

    btc = rv.PROFILES["BTCUSDT"]
    assert btc.bb_std == Decimal("1.8")
    assert btc.initial_margin == Decimal("0.0125")
    assert btc.add_atrs == (Decimal("1.4"), Decimal("3.0"), Decimal("4.6"))
    assert btc.keep_ratio == Decimal("0.50")
    assert btc.atr_expansion == Decimal("1.8")

    eth = rv.PROFILES["ETHUSDT"]
    assert eth.bb_std == Decimal("1.9")
    assert eth.initial_margin == Decimal("0.005")
    assert eth.add_margins == (Decimal("0.005"), Decimal("0.0075"), Decimal("0.010"))
    assert eth.add_atrs == (Decimal("1.0"), Decimal("2.0"), Decimal("3.2"))
    assert eth.keep_ratio == Decimal("0.20")
    assert eth.atr_expansion == Decimal("1.5")

    sol = rv.PROFILES["SOLUSDT"]
    assert sol.bb_std == Decimal("1.65")
    assert sol.initial_margin == Decimal("0.003")
    assert sol.add_margins == (Decimal("0.003"), Decimal("0.0045"), Decimal("0.006"))
    assert sol.add_atrs == (Decimal("1.0"), Decimal("2.5"), Decimal("4.0"))
    assert sol.keep_ratio == Decimal("0.80")
    assert sol.atr_expansion == Decimal("1.5")


def test_scan_consumes_simultaneous_signals_and_uses_priority(monkeypatch):
    state = FakeState()
    bot = rv.Bot(object(), state, {})
    opened = []
    now_ms = 1_800_000_000_000
    close_time = now_ms - 1_000

    signals = {
        "BTCUSDT": ("long", close_time, Decimal("10")),
        "ETHUSDT": ("short", close_time, Decimal("5")),
        "SOLUSDT": None,
    }
    monkeypatch.setattr(rv, "LIVE", True)
    monkeypatch.setattr(rv.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(rv, "signal_for", lambda api, symbol: signals[symbol])
    monkeypatch.setattr(bot, "open_signal", lambda *args: opened.append(args))

    bot.scan()

    assert opened == [("BTCUSDT", "long", close_time, Decimal("10"))]
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
        "ETHUSDT": ("long", close_time, Decimal("5")),
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
