import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path


def load_module():
    os.environ["LIVE_TRADING"] = "0"
    path = Path(__file__).resolve().parents[1] / "reversion_live" / "main.py"
    spec = importlib.util.spec_from_file_location("reversion_entry_boundary_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rv = load_module()


class StateStub:
    def __init__(self):
        self.data = rv.blank_state()
        self.data["shadow"].update(
            {"completed_count": 40, "ewm": "0.01", "gate_on": True}
        )
        self.saves = 0

    def save(self):
        self.saves += 1


class BoundaryAPI:
    def __init__(self, expected):
        self.expected = expected
        self.calls = 0

    def klines(self, symbol, interval, limit=120, start_time=None, end_time=None):
        self.calls += 1
        open_ms = self.expected - 60_000 if self.calls == 1 else self.expected
        return [
            [open_ms, "100", "101", "99", "100", "1", open_ms + 59_999]
        ]


def test_shadow_retries_previous_minute_boundary_race(monkeypatch):
    close_time = 1_800_000_899_999
    expected = close_time + 1
    api = BoundaryAPI(expected)
    state = StateStub()
    bot = rv.Bot(api, state, {})
    monkeypatch.setattr(rv.time, "time", lambda: (expected + 1_000) / 1000)
    monkeypatch.setattr(rv.time, "sleep", lambda _: None)
    signal = rv.Signal(
        "short", close_time, Decimal("2"), Decimal("100"), "test"
    )

    gate_on = bot.start_shadow("BTCUSDT", signal)

    assert gate_on is True
    assert api.calls == 2
    assert state.data["shadow"]["position"]["entry_open_ms"] == expected


def test_v4_state_is_migration_boundary(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"version":4,"live_account_initialized":true,"initial_live_equity":"49.999",'
        '"shadow":{"completed_count":292,"ewm":"0.031","gate_on":true,"seeded":true}}',
        encoding="utf-8",
    )
    state = rv.State(path)
    assert state.legacy is True
    assert state.loaded_version == 4
    state.rebuild_for_strategy()
    assert state.data["version"] == 5
    assert state.data["live_account_initialized"] is True
    assert state.data["initial_live_equity"] == "49.999"
    assert state.data["shadow"]["completed_count"] == 0
