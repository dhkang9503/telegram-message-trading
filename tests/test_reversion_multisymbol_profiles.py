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


def test_profiles_match_full_period_ensemble_contract():
    assert rv.SYMBOLS == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert rv.STRATEGY_ID == "full_period_regime_ensemble_v1"
    assert rv.STATE_VERSION == 5
    assert rv.GATE_SPAN == 40
    assert rv.GATE_THRESHOLD == Decimal("0.0025")
    assert rv.FALLBACK_FAST == 32
    assert rv.FALLBACK_SLOW == 384
    assert rv.FALLBACK_MAX_LEVERAGE == Decimal("5")

    expected = {
        "BTCUSDT": (Decimal("5"), Decimal("8"), Decimal("0.32")),
        "ETHUSDT": (Decimal("9"), Decimal("14.4"), Decimal("0.32")),
        "SOLUSDT": (Decimal("3"), Decimal("4.8"), Decimal("0.24")),
    }
    for symbol, (shadow_mult, live_mult, live_cap) in expected.items():
        assert rv.PROFILES[symbol].notional_mult == shadow_mult
        assert rv.SNAPBACK_LIVE_PROFILES[symbol].notional_mult == live_mult
        assert rv.SNAPBACK_LIVE_PROFILES[symbol].hard_stop == live_cap


def test_v3_flat_state_rebuild_discards_stale_gate_but_preserves_account_guard(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"version":3,"live_account_initialized":true,"initial_live_equity":"49.999",'
        '"shadow":{"completed_count":292,"ewm":"0.031","gate_on":true,"seeded":true}}',
        encoding="utf-8",
    )
    state = rv.State(path)
    assert state.legacy is True
    assert state.loaded_version == 3
    state.rebuild_for_strategy()
    assert state.data["version"] == 5
    assert state.data["live_account_initialized"] is True
    assert state.data["initial_live_equity"] == "49.999"
    assert state.data["shadow"]["completed_count"] == 0
    assert state.data["shadow"]["catchup_complete"] is False


def _row(open_ms: int, close_ms: int) -> list[object]:
    return [open_ms, "100", "101", "99", "100", "1", close_ms]


def test_gate_catchup_is_causal_consumes_priority_and_resamples_after_completion(monkeypatch, tmp_path):
    interval = rv.SIGNAL_INTERVAL_MS
    seed_through = interval // 2
    first_close = interval - 1
    second_close = 2 * interval - 1
    now_ms = 2 * interval + 5 * 60_000

    state = rv.State(tmp_path / "state.json")
    shadow = state.data["shadow"]
    shadow.update(
        {
            "completed_count": 40,
            "ewm": "0.01",
            "gate_on": True,
            "position": None,
            "seeded": True,
            "seeded_through_ms": seed_through,
            "catchup_complete": False,
        }
    )
    state.save()

    rows = [
        _row(-interval, -1),
        _row(0, first_close),
        _row(interval, second_close),
    ]

    monkeypatch.setattr(rv, "INDICATOR_FETCH_LIMIT", 3)
    monkeypatch.setattr(
        rv,
        "historical_klines",
        lambda api, symbol, timeframe, start, end, limit=1500: rows,
    )

    seen = []

    def fake_signal(api, symbol, supplied_rows=None):
        close_time = int(supplied_rows[-1][6])
        if close_time == first_close and symbol in ("BTCUSDT", "ETHUSDT"):
            side = "short" if symbol == "BTCUSDT" else "long"
            return rv.Signal(side, close_time, Decimal("1"), Decimal("100"), symbol)
        if close_time == second_close and symbol == "SOLUSDT":
            return rv.Signal("long", close_time, Decimal("1"), Decimal("100"), symbol)
        return None

    monkeypatch.setattr(rv, "signal_for", fake_signal)

    def fake_sim(api, symbol, signal, cutoff):
        seen.append((symbol, signal.close_time, bool(state.data["shadow"].get("gate_on"))))
        trade_return = Decimal("-0.20") if symbol == "BTCUSDT" else Decimal("0")
        return {
            "completed": True,
            "exit_close_ms": signal.close_time + 60_000,
            "trade_return": trade_return,
            "reason": "test",
            "exit_price": Decimal("100"),
        }

    monkeypatch.setattr(rv, "simulate_historical_shadow", fake_sim)
    rv.catch_up_shadow_gate(object(), state, now_ms=now_ms)

    # BTC wins first-bar priority, ETH is consumed, and the BTC loss is applied
    # before the next bar samples the gate. That pushes EWM below the threshold,
    # so the second SOL shadow starts with gate OFF.
    assert [item[0] for item in seen] == ["BTCUSDT", "SOLUSDT"]
    assert state.data["last_signal"]["BTCUSDT"] == first_close
    assert state.data["last_signal"]["ETHUSDT"] == first_close
    assert state.data["last_signal"]["SOLUSDT"] == second_close
    assert state.data["shadow"]["completed_count"] == 42
    assert Decimal(state.data["shadow"]["ewm"]) < rv.GATE_THRESHOLD
    assert state.data["shadow"]["gate_on"] is False
    assert state.data["shadow"]["catchup_complete"] is True
    assert state.data["last_strategy_bar_close"] == second_close
