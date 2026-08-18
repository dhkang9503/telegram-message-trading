from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "reversion_live" / "main.py"
TEST = ROOT / "tests" / "test_reversion_multisymbol_profiles.py"


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match, found {count}: {old[:120]!r}")
    return text.replace(old, new, 1)


text = MAIN.read_text(encoding="utf-8")

text = replace_once(text, 'STATE_VERSION = 3', 'STATE_VERSION = 4')

text = replace_once(
    text,
    '''        "shadow": {
            "completed_count": 0,
            "ewm": None,
            "gate_on": False,
            "position": None,
            "seeded": False,
        },''',
    '''        "shadow": {
            "completed_count": 0,
            "ewm": None,
            "gate_on": False,
            "position": None,
            "seeded": False,
            "catchup_complete": False,
            "catchup_through_ms": None,
        },''',
)

text = replace_once(
    text,
    '''class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = blank_state()
        self.legacy = False
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            version = int(loaded.get("version", 1))
            if version == STATE_VERSION:
                self.data.update(loaded)
            elif version in (1, 2):
                # Never silently take over a position opened by an older strategy.
                self.data = loaded
                self.legacy = True
            else:
                raise BotError(f"unsupported state version={version}")
        if not self.legacy:
            self.save()
''',
    '''class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = blank_state()
        self.legacy = False
        self.loaded_version = STATE_VERSION
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            version = int(loaded.get("version", 1))
            self.loaded_version = version
            if version == STATE_VERSION:
                self.data.update(loaded)
            elif version in (1, 2, 3):
                # Never silently take over a position opened by an older strategy.
                # v3 is rebuilt from the audited gate seed because its embedded seed
                # could skip history between the seed timestamp and process startup.
                self.data = loaded
                self.legacy = True
            else:
                raise BotError(f"unsupported state version={version}")
        if not self.legacy:
            self.save()
''',
)

text = replace_once(
    text,
    '''    def reset_cycle(self) -> None:
        preserved = {
''',
    '''    def rebuild_for_strategy(self) -> None:
        # Preserve only the one-time live-account guard when rebuilding a flat v3
        # state. Gate/fallback/signal state must be reconstructed from audited
        # history rather than carried across the schema boundary.
        initialized = bool(self.data.get("live_account_initialized", False))
        initial_equity = self.data.get("initial_live_equity")
        self.data = blank_state()
        if self.loaded_version == 3:
            self.data["live_account_initialized"] = initialized
            self.data["initial_live_equity"] = initial_equity
        self.loaded_version = STATE_VERSION
        self.legacy = False
        self.save()

    def reset_cycle(self) -> None:
        preserved = {
''',
)

marker = '''\ndef rolling_return_vol(closes: list[Decimal], window: int) -> list[Decimal | None]:\n'''
if marker not in text:
    raise RuntimeError("rolling_return_vol insertion marker missing")

catchup_code = r'''

def historical_klines(
    api: Binance,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    limit: int = 1500,
) -> list[list[Any]]:
    """Fetch an exact historical kline interval, failing on gaps/duplicates.

    Startup gate reconstruction is risk state, so incomplete exchange history is
    treated as fatal instead of silently approximated.
    """
    step_by_interval = {"1m": 60_000, "15m": SIGNAL_INTERVAL_MS}
    if interval not in step_by_interval:
        raise BotError(f"unsupported historical interval={interval}")
    step = step_by_interval[interval]
    if end_time < start_time:
        return []
    cursor = start_time
    rows: list[list[Any]] = []
    while cursor <= end_time:
        page = api.klines(
            symbol,
            interval,
            limit,
            start_time=cursor,
            end_time=end_time,
        )
        page = sorted(
            (row for row in page if cursor <= int(row[0]) <= end_time),
            key=lambda row: int(row[0]),
        )
        if not page:
            break
        rows.extend(page)
        newest = int(page[-1][0])
        if newest < cursor:
            raise BotError(f"{symbol} {interval} history cursor did not advance")
        cursor = newest + step
        if len(page) < limit:
            break

    dedup: dict[int, list[Any]] = {}
    for row in rows:
        open_ms = int(row[0])
        if open_ms in dedup:
            raise BotError(f"duplicate {symbol} {interval} candle at {open_ms}")
        dedup[open_ms] = row
    ordered = [dedup[key] for key in sorted(dedup)]
    expected = list(range(start_time, end_time + 1, step))
    actual = [int(row[0]) for row in ordered]
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise BotError(
            f"incomplete {symbol} {interval} history start={start_time} end={end_time} "
            f"expected={len(expected)} actual={len(actual)} "
            f"missing_head={missing[:3]} extra_head={extra[:3]}"
        )
    return ordered


def _shadow_trade_levels(symbol: str, s: Signal, entry: Decimal) -> dict[str, Any]:
    profile = PROFILES[symbol]
    side_sign = D(1) if s.side == "long" else D(-1)
    qty_abs = profile.notional_mult / entry
    qty = side_sign * qty_abs
    wallet = D(1) - profile.notional_mult * SHADOW_COST
    if s.side == "long":
        take_profit = entry + profile.tp_atr * s.atr
        atr_stop = entry - profile.sl_atr * s.atr
        hard_stop = (
            entry * (D(1) + SHADOW_COST) - profile.hard_stop / qty_abs
        ) / (D(1) - SHADOW_COST)
        stop = max(atr_stop, hard_stop)
    else:
        take_profit = entry - profile.tp_atr * s.atr
        atr_stop = entry + profile.sl_atr * s.atr
        hard_stop = (
            entry * (D(1) - SHADOW_COST) + profile.hard_stop / qty_abs
        ) / (D(1) + SHADOW_COST)
        stop = min(atr_stop, hard_stop)
    return {
        "profile": profile,
        "qty": qty,
        "qty_abs": qty_abs,
        "wallet": wallet,
        "take_profit": take_profit,
        "stop": stop,
    }


def simulate_historical_shadow(
    api: Binance,
    symbol: str,
    s: Signal,
    cutoff_open_ms: int,
) -> dict[str, Any]:
    """Replay one shadow trade with the same minute/funding semantics as live."""
    entry_open_ms = s.close_time + 1
    if entry_open_ms > cutoff_open_ms:
        raise BotError(
            f"shadow entry {entry_open_ms} is newer than closed-minute cutoff {cutoff_open_ms}"
        )
    profile = PROFILES[symbol]
    max_exit_ms = entry_open_ms + profile.max_hold_minutes * 60_000
    end_open_ms = min(max_exit_ms, cutoff_open_ms)
    rows = historical_klines(api, symbol, "1m", entry_open_ms, end_open_ms)
    if not rows:
        raise BotError(f"historical shadow entry candle unavailable for {symbol}")

    entry = D(rows[0][1])
    levels = _shadow_trade_levels(symbol, s, entry)
    qty = D(levels["qty"])
    qty_abs = D(levels["qty_abs"])
    wallet = D(levels["wallet"])
    take_profit = D(levels["take_profit"])
    stop = D(levels["stop"])

    funding_rows = api.funding_rates(symbol, entry_open_ms, int(rows[-1][6]))
    funding_by_minute: dict[int, list[dict[str, Any]]] = {}
    for funding in funding_rows:
        funding_time = int(funding["fundingTime"])
        minute = (funding_time // 60_000) * 60_000
        funding_by_minute.setdefault(minute, []).append(funding)
    last_funding_time = entry_open_ms - 1

    for row in rows:
        open_ms = int(row[0])
        open_price, high, low = D(row[1]), D(row[2]), D(row[3])
        if open_ms > entry_open_ms:
            for funding in funding_by_minute.get(open_ms, []):
                funding_time = int(funding["fundingTime"])
                if funding_time <= last_funding_time:
                    continue
                mark = D(funding.get("markPrice"), row[1])
                wallet -= qty * mark * D(funding["fundingRate"])
                last_funding_time = funding_time

        open_equity = wallet + qty * (open_price - entry)
        reason: str | None = None
        exit_price = D(0)
        if open_equity <= D("0.005") * qty_abs * open_price:
            reason = "liquidation_gap"
            exit_price = open_price
        else:
            stop_hit = (qty > 0 and low <= stop) or (qty < 0 and high >= stop)
            target_hit = (qty > 0 and high >= take_profit) or (
                qty < 0 and low <= take_profit
            )
            if stop_hit:
                exit_price = min(stop, open_price) if qty > 0 else max(stop, open_price)
                reason = "stop"
            elif target_hit:
                exit_price = max(take_profit, open_price) if qty > 0 else min(
                    take_profit, open_price
                )
                reason = "take_profit"
            elif open_ms >= max_exit_ms:
                exit_price = open_price
                reason = "max_hold"

        if reason is not None:
            if reason == "liquidation_gap":
                trade_return = D("-1")
            else:
                wallet += qty * (exit_price - entry) - qty_abs * exit_price * SHADOW_COST
                trade_return = wallet - D(1)
            return {
                "completed": True,
                "exit_close_ms": int(row[6]),
                "trade_return": trade_return,
                "reason": reason,
                "exit_price": exit_price,
            }

    raw = {
        "symbol": symbol,
        "side": s.side,
        "entry": ds(entry),
        "qty": ds(qty),
        "wallet": ds(wallet),
        "take_profit": ds(take_profit),
        "stop": ds(stop),
        "entry_open_ms": entry_open_ms,
        "last_processed_open_ms": int(rows[-1][0]),
        "last_funding_time_ms": last_funding_time,
        "funding_checked_through_ms": int(rows[-1][6]),
        "max_exit_ms": max_exit_ms,
    }
    return {"completed": False, "position": raw}


def _apply_catchup_completion(shadow: dict[str, Any], result: dict[str, Any]) -> None:
    trade_return = D(result["trade_return"])
    count = int(shadow.get("completed_count", 0))
    prior = D(shadow.get("ewm")) if shadow.get("ewm") is not None else None
    updated = (
        trade_return
        if prior is None
        else (D(1) - GATE_ALPHA) * prior + GATE_ALPHA * trade_return
    )
    shadow["completed_count"] = count + 1
    shadow["ewm"] = ds(updated)
    shadow["position"] = None


def catch_up_shadow_gate(api: Binance, state: State, now_ms: int | None = None) -> None:
    """Causally reconstruct seed-to-startup shadow history before live orders.

    This intentionally preserves the research gate sampling timing: a gate value
    is sampled when a new shadow trade starts; a completed trade updates EWM,
    while gate_on is resampled only at the next eligible shadow entry.
    """
    if state.legacy:
        raise BotError("cannot catch up a legacy state")
    shadow = state.data["shadow"]
    if shadow.get("catchup_complete"):
        return
    if not shadow.get("seeded"):
        raise BotError("shadow gate must be seeded before catch-up")
    if shadow.get("position"):
        raise BotError("startup catch-up requires a flat seeded shadow state")

    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    seed_through = int(shadow.get("seeded_through_ms") or 0)
    target_close = latest_completed_15m_close_time(now_ms)
    latest_closed_1m_open = (now_ms // 60_000) * 60_000 - 60_000
    if seed_through <= 0:
        raise BotError("invalid shadow seeded_through_ms")
    if latest_closed_1m_open < 0:
        raise BotError("invalid closed-minute cutoff")

    first_close = ((seed_through // SIGNAL_INTERVAL_MS) + 1) * SIGNAL_INTERVAL_MS - 1
    before_count = int(shadow.get("completed_count", 0))
    LOG.warning(
        "GATE_CATCHUP_START from=%s to=%s completed=%s ewm=%s sampled_gate=%s",
        seed_through,
        target_close,
        before_count,
        shadow.get("ewm"),
        shadow.get("gate_on"),
    )

    if first_close > target_close:
        shadow["catchup_complete"] = True
        shadow["catchup_through_ms"] = target_close
        state.data["last_strategy_bar_close"] = target_close
        state.save()
        LOG.warning(
            "GATE_CATCHUP_COMPLETE from=%s to=%s added_trades=0 completed=%s "
            "ewm=%s gate_on=%s",
            seed_through,
            target_close,
            before_count,
            shadow.get("ewm"),
            shadow.get("gate_on"),
        )
        return

    first_open = first_close - SIGNAL_INTERVAL_MS + 1
    warmup_open = max(
        0, first_open - (INDICATOR_FETCH_LIMIT - 1) * SIGNAL_INTERVAL_MS
    )
    target_open = target_close - SIGNAL_INTERVAL_MS + 1
    rows_by_symbol = {
        symbol: historical_klines(
            api, symbol, "15m", warmup_open, target_open, INDICATOR_FETCH_LIMIT
        )
        for symbol in SYMBOLS
    }
    index_by_close = {
        symbol: {int(row[6]): i for i, row in enumerate(rows)}
        for symbol, rows in rows_by_symbol.items()
    }
    replay_closes = list(range(first_close, target_close + 1, SIGNAL_INTERVAL_MS))
    for close_time in replay_closes:
        missing = [symbol for symbol in SYMBOLS if close_time not in index_by_close[symbol]]
        if missing:
            raise BotError(
                f"historical 15m alignment missing close_time={close_time} symbols={missing}"
            )

    # Work on a detached JSON copy so an exception cannot persist a partially
    # reconstructed gate. seed_shadow_gate itself is the only pre-catchup write.
    working = json.loads(json.dumps(state.data))
    shadow = working["shadow"]
    scheduled: dict[str, Any] | None = None

    for close_time in replay_closes:
        if (
            scheduled is not None
            and scheduled.get("completed")
            and int(scheduled["exit_close_ms"]) <= close_time
        ):
            _apply_catchup_completion(shadow, scheduled)
            scheduled = None

        candidates: list[tuple[str, Signal]] = []
        for symbol in SYMBOLS:
            rows = rows_by_symbol[symbol]
            idx = index_by_close[symbol][close_time]
            start = max(0, idx - INDICATOR_FETCH_LIMIT + 1)
            current = signal_for(api, symbol, rows[start : idx + 1])
            if current and current.close_time > int(
                working["last_signal"].get(symbol, 0)
            ):
                candidates.append((symbol, current))
                working["last_signal"][symbol] = current.close_time

        if candidates and scheduled is None:
            count = int(shadow.get("completed_count", 0))
            current_ewm = D(shadow.get("ewm")) if shadow.get("ewm") is not None else None
            gate_on = (
                count >= GATE_SPAN
                and current_ewm is not None
                and current_ewm > GATE_THRESHOLD
            )
            shadow["gate_on"] = gate_on
            symbol, selected = candidates[0]
            scheduled = simulate_historical_shadow(
                api, symbol, selected, latest_closed_1m_open
            )
            if not scheduled.get("completed"):
                shadow["position"] = scheduled["position"]

    if scheduled is not None and scheduled.get("completed"):
        if int(scheduled["exit_close_ms"]) > latest_closed_1m_open + 59_999:
            raise BotError("historical shadow completion exceeds closed-minute cutoff")
        _apply_catchup_completion(shadow, scheduled)
        scheduled = None

    shadow["catchup_complete"] = True
    shadow["catchup_through_ms"] = target_close
    working["last_strategy_bar_close"] = target_close
    state.data = working
    state.save()
    added = int(shadow.get("completed_count", 0)) - before_count
    LOG.warning(
        "GATE_CATCHUP_COMPLETE from=%s to=%s added_trades=%s completed=%s ewm=%s "
        "gate_on=%s shadow_open=%s",
        seed_through,
        target_close,
        added,
        shadow.get("completed_count"),
        shadow.get("ewm"),
        shadow.get("gate_on"),
        bool(shadow.get("position")),
    )
'''

text = text.replace(marker, catchup_code + marker, 1)

text = replace_once(
    text,
    '''        if self.state.legacy:
            if pos:
                raise BotError(
                    "legacy v1 DCA/rotation position/state detected. "
                    "Do not let the new snapback strategy take it over; "
                    "finish/close the legacy position with the old bot first."
                )
            LOG.warning("migrating flat legacy state to %s", STRATEGY_ID)
            self.state.reset_cycle()

        seed_shadow_gate(self.state)

        if self.reconcile_pending():
''',
    '''        if self.state.legacy:
            if pos:
                raise BotError(
                    "legacy strategy position/state detected. Do not let the new "
                    "gate-catchup strategy take over an older live position; "
                    "finish/close it with the matching old bot first."
                )
            LOG.warning(
                "migrating flat state v%s to %s with full gate reconstruction",
                self.state.loaded_version,
                STRATEGY_ID,
            )
            self.state.rebuild_for_strategy()

        seed_shadow_gate(self.state)
        catch_up_shadow_gate(self.api, self.state)

        if self.reconcile_pending():
''',
)

# Signal-only startup should also reconstruct public historical state when the
# state already matches the current schema. This keeps dry-run diagnostics equal
# to live without requiring account credentials.
text = replace_once(
    text,
    '''            seed_shadow_gate(self.state)
            return

        pos = open_positions(self.api)
''',
    '''            seed_shadow_gate(self.state)
            catch_up_shadow_gate(self.api, self.state)
            return

        pos = open_positions(self.api)
''',
)

MAIN.write_text(text, encoding="utf-8")

TEST.write_text(
    r'''import importlib.util
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
    assert rv.STATE_VERSION == 4
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
    assert state.data["version"] == 4
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
''',
    encoding="utf-8",
)

print("patched reversion gate catch-up and tests")
