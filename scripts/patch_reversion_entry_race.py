from pathlib import Path

MAIN = Path("reversion_live/main.py")
text = MAIN.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match, found {count}: {old[:100]!r}")
    text = text.replace(old, new, 1)


replace_once("STATE_VERSION = 4", "STATE_VERSION = 5")
replace_once("elif version in (1, 2, 3):", "elif version in (1, 2, 3, 4):")
replace_once("if self.loaded_version == 3:", "if self.loaded_version in (3, 4):")

marker = "def latest_trade(api: Binance, symbol: str) -> tuple[Decimal, int]:\n"
helper = '''def exact_entry_kline(
    api: Binance,
    symbol: str,
    interval: str,
    expected_open_ms: int,
    signal_close_time: int,
) -> list[Any]:
    """Fetch the exact entry candle, tolerating exchange boundary propagation lag.

    Binance can briefly expose the previous kline immediately after a timeframe
    boundary. Poll the exact requested candle until the signal freshness deadline.
    A stale/recovered shadow signal is allowed to fetch its historical entry candle
    once so shadow accounting remains continuous without opening a stale live trade.
    """
    step_by_interval = {"1m": 60_000, "5m": 5 * 60_000}
    if interval not in step_by_interval:
        raise BotError(f"unsupported entry interval={interval}")
    expected_open_ms = int(expected_open_ms)
    deadline_ms = int(signal_close_time) + SIGNAL_MAX_AGE_MS
    last_open: int | None = None
    while True:
        rows = api.klines(
            symbol,
            interval,
            2,
            start_time=expected_open_ms,
            end_time=expected_open_ms + step_by_interval[interval] - 1,
        )
        for row in rows:
            open_ms = int(row[0])
            last_open = open_ms
            if open_ms == expected_open_ms:
                return row

        now_ms = int(time.time() * 1000)
        if now_ms >= deadline_ms:
            raise BotError(
                f"entry candle unavailable: {symbol} {interval} "
                f"expected={expected_open_ms} last={last_open} now={now_ms}"
            )
        time.sleep(max(0.01, min(0.25, (deadline_ms - now_ms) / 1000)))


'''
replace_once(marker, helper + marker)

replace_once(
'''        rows = self.api.klines(symbol, "1m", 1)
        if not rows:
            raise BotError(f"current 1m entry candle unavailable for shadow {symbol}")
        row = rows[-1]
        entry_open_ms = int(row[0])
        if entry_open_ms != s.close_time + 1:
            raise BotError(
                f"shadow execution candle mismatch: expected={s.close_time + 1} "
                f"got={entry_open_ms}"
            )
        entry = D(row[1])
''',
'''        row = exact_entry_kline(
            self.api, symbol, "1m", s.close_time + 1, s.close_time
        )
        entry_open_ms = int(row[0])
        entry = D(row[1])
''',
)

replace_once(
'''        trade, candle = latest_trade(self.api, symbol)
        if candle != s.close_time + 1:
            raise BotError(
                f"snapback execution candle mismatch: expected={s.close_time + 1} got={candle}"
            )
''',
'''        entry_row = exact_entry_kline(
            self.api, symbol, "5m", s.close_time + 1, s.close_time
        )
        trade, candle = D(entry_row[4]), int(entry_row[0])
''',
)

replace_once(
'''        trade, candle = latest_trade(self.api, symbol)
        if candle != close_time + 1:
            raise BotError(
                f"fallback execution candle mismatch: expected={close_time + 1} got={candle}"
            )
''',
'''        entry_row = exact_entry_kline(
            self.api, symbol, "5m", close_time + 1, close_time
        )
        trade, candle = D(entry_row[4]), int(entry_row[0])
''',
)

replace_once(
'''            if current and current.close_time > int(
                self.state.data["last_signal"].get(symbol, 0)
            ):
                candidates.append((symbol, current))
                self.state.data["last_signal"][symbol] = current.close_time

        self.state.data["last_strategy_bar_close"] = close_time
        self.state.save()
''',
'''            if current and current.close_time > int(
                self.state.data["last_signal"].get(symbol, 0)
            ):
                candidates.append((symbol, current))

''',
)

replace_once(
'''        if candidates and fresh_bar:
            if self.state.data["shadow"].get("position"):
                LOG.info("SKIP_SHADOW_SIGNAL occupied=true candidates=%s", len(candidates))
            else:
                symbol, selected = candidates[0]
                gate_on = self.start_shadow(symbol, selected)
                if LIVE and gate_on:
                    live_positions = open_positions(self.api)
                    if live_positions and self.state.data.get("active_strategy") == "fallback":
                        self.close_all(live_positions[0], "gate_on_snapback")
                        live_positions = []
                    if not live_positions:
                        self.open_signal(symbol, selected)
                if len(candidates) > 1:
                    LOG.info(
                        "SNAPBACK_PRIORITY selected=%s consumed=%s",
                        symbol,
                        ",".join(item[0] for item in candidates[1:]),
                    )
        elif candidates:
            LOG.info("SKIP_STALE_SNAPBACK count=%s age_ms=%s", len(candidates), age_ms)

        self.apply_fallback(snapshot, fresh_bar)
''',
'''        if candidates:
            if self.state.data["shadow"].get("position"):
                LOG.info("SKIP_SHADOW_SIGNAL occupied=true candidates=%s", len(candidates))
            else:
                symbol, selected = candidates[0]
                gate_on = self.start_shadow(symbol, selected)
                if LIVE and fresh_bar and gate_on:
                    live_positions = open_positions(self.api)
                    if live_positions and self.state.data.get("active_strategy") == "fallback":
                        self.close_all(live_positions[0], "gate_on_snapback")
                        live_positions = []
                    if not live_positions:
                        self.open_signal(symbol, selected)
                elif LIVE and not fresh_bar:
                    LOG.info(
                        "SKIP_STALE_LIVE_SNAPBACK %s close_time=%s age_ms=%s",
                        symbol,
                        selected.close_time,
                        age_ms,
                    )
                if len(candidates) > 1:
                    LOG.info(
                        "SNAPBACK_PRIORITY selected=%s consumed=%s",
                        symbol,
                        ",".join(item[0] for item in candidates[1:]),
                    )

        self.apply_fallback(snapshot, fresh_bar)
        for symbol, current in candidates:
            self.state.data["last_signal"][symbol] = current.close_time
        self.state.data["last_strategy_bar_close"] = close_time
        self.state.save()
''',
)

MAIN.write_text(text, encoding="utf-8")

test_path = Path("tests/test_reversion_multisymbol_profiles.py")
tests = test_path.read_text(encoding="utf-8")
tests = tests.replace("assert rv.STATE_VERSION == 4", "assert rv.STATE_VERSION == 5")
tests = tests.replace('assert state.data["version"] == 4', 'assert state.data["version"] == 5')
test_path.write_text(tests, encoding="utf-8")

extra = Path("tests/test_reversion_entry_boundary.py")
extra.write_text('''import importlib.util
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
''', encoding="utf-8")
