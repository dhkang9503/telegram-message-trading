"""BTCUSDT 1-minute BB/CCI reversion strategy for Binance USD-M Futures.

LIVE_TRADING=0 is the default and is a local paper simulation. LIVE_TRADING=1
uses the configured Binance credentials and submits real USD-M orders.

The live path is intentionally fail-closed. It expects a dedicated futures
account (or sub-account), one-way mode, no unrelated positions, and no orders
that were not created by this bot. Entry and take-profit orders are post-only;
the stop and maximum-hold exits are market orders. There is no martingale or
averaging-down logic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

LOG = logging.getLogger("reversion_live")
KST = ZoneInfo("Asia/Seoul")

INTERVAL_MS = 60_000
BB_WINDOW = 90
CCI_WINDOW = 40
EMA_SPAN = 600
WARMUP_BARS = 1_440
EXTREME_LOOKBACK = 10
BB_Z_THRESHOLD = 1.5
CCI_THRESHOLD = 100.0
ENTRY_OFFSET = Decimal("0.0005")
PAPER_FILL_PENETRATION = Decimal("0.0001")
TAKE_PROFIT_RATE = Decimal("0.006")
STOP_RATE = Decimal("0.035")
ACCOUNT_RISK_RATE = Decimal("0.03")
ENTRY_TTL_MS = 20 * INTERVAL_MS
MAX_HOLD_MS = 720 * INTERVAL_MS
COOLDOWN_MS = 5 * INTERVAL_MS
HISTORY_BARS = 3_000
CLIENT_ID_PREFIX = "bbcci-"
STRATEGY_ID = "btc_1m_binance_bb90_cci40_recovery_price600_binance_passive_v1"
STATE_VERSION = 2


class SafetyHalt(RuntimeError):
    """Raised when continuing could touch an unowned order or unsafe account."""


class BinanceAPIError(RuntimeError):
    def __init__(self, status: int, code: int | None, message: str):
        self.status = status
        self.code = code
        self.api_message = message
        super().__init__(f"Binance API error status={status} code={code}: {message}")


def _env_bool(name: str, default: str = "0") -> bool:
    raw = os.getenv(name, default).strip()
    if raw not in {"0", "1"}:
        raise ValueError(f"{name} must be exactly 0 or 1")
    return raw == "1"


def _positive_float(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _api_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def load_dotenv_files() -> list[Path]:
    """Load simple KEY=VALUE files without overriding service environment vars."""
    script_dir = Path(__file__).resolve().parent
    loaded: list[Path] = []
    for path in (script_dir / ".env", script_dir.parent / ".env"):
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        loaded.append(path)
    return loaded


@dataclass(frozen=True)
class Config:
    live_trading: bool
    api_key: str
    api_secret: str
    symbol: str
    rest_url: str
    poll_seconds: float
    http_timeout_seconds: float
    recv_window_ms: int
    log_path: Path
    state_path: Path
    paper_initial_equity: Decimal
    paper_maker_fee: Decimal
    paper_taker_fee: Decimal
    paper_market_slippage: Decimal

    @classmethod
    def from_env(cls) -> "Config":
        live = _env_bool("LIVE_TRADING")
        api_key = os.getenv("BINANCE_API_KEY", "").strip()
        api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
        if live and (not api_key or not api_secret):
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required when "
                "LIVE_TRADING=1"
            )
        symbol = os.getenv("BINANCE_SYMBOL", "BTCUSDT").strip().upper()
        if symbol != "BTCUSDT":
            raise ValueError("This strategy is locked to BINANCE_SYMBOL=BTCUSDT")
        base_dir = Path(
            os.getenv(
                "REVERSION_DATA_DIR",
                str(Path(__file__).resolve().parent / "data"),
            )
        ).expanduser()
        mode = "live" if live else "paper"
        log_path = Path(
            os.getenv("REVERSION_LOG_PATH", str(base_dir / f"{mode}.jsonl"))
        ).expanduser()
        state_path = Path(
            os.getenv("REVERSION_STATE_PATH", str(base_dir / f"{mode}_state.json"))
        ).expanduser()
        recv_window = int(os.getenv("BINANCE_RECV_WINDOW_MS", "5000"))
        if not 1 <= recv_window <= 60_000:
            raise ValueError("BINANCE_RECV_WINDOW_MS must be between 1 and 60000")
        initial_equity = Decimal(os.getenv("PAPER_INITIAL_EQUITY", "10000"))
        if initial_equity <= 0:
            raise ValueError("PAPER_INITIAL_EQUITY must be positive")
        rest_url = os.getenv(
            "BINANCE_FUTURES_REST_URL", "https://fapi.binance.com"
        ).strip().rstrip("/")
        if not rest_url or (live and not rest_url.startswith("https://")):
            raise ValueError("Live Binance REST requests require an HTTPS URL")
        maker_fee = Decimal(os.getenv("PAPER_MAKER_FEE", "0.0002"))
        taker_fee = Decimal(os.getenv("PAPER_TAKER_FEE", "0.0006"))
        market_slippage = Decimal(os.getenv("PAPER_MARKET_SLIPPAGE", "0.0001"))
        for name, value in (
            ("PAPER_MAKER_FEE", maker_fee),
            ("PAPER_TAKER_FEE", taker_fee),
            ("PAPER_MARKET_SLIPPAGE", market_slippage),
        ):
            if not value.is_finite() or value < 0 or value >= 1:
                raise ValueError(f"{name} must be finite and between 0 and 1")
        return cls(
            live_trading=live,
            api_key=api_key,
            api_secret=api_secret,
            symbol=symbol,
            rest_url=rest_url,
            poll_seconds=_positive_float("REVERSION_POLL_SECONDS", "3"),
            http_timeout_seconds=_positive_float("BINANCE_HTTP_TIMEOUT_SECONDS", "10"),
            recv_window_ms=recv_window,
            log_path=log_path,
            state_path=state_path,
            paper_initial_equity=initial_equity,
            paper_maker_fee=maker_fee,
            paper_taker_fee=taker_fee,
            paper_market_slippage=market_slippage,
        )


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_binance(cls, row: list[Any]) -> "Candle":
        return cls(
            open_time_ms=int(row[0]),
            close_time_ms=int(row[6]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )


@dataclass(frozen=True)
class Signal:
    side: str
    z_extreme: float
    cci_previous: float
    cci_current: float
    ema: float


@dataclass(frozen=True)
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    market_step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, payload: Mapping[str, Any], symbol: str) -> "SymbolRules":
        symbol_info = next(
            (item for item in payload.get("symbols", []) if item.get("symbol") == symbol),
            None,
        )
        if symbol_info is None:
            raise ValueError(f"Symbol not found in exchangeInfo: {symbol}")
        filters = {item["filterType"]: item for item in symbol_info["filters"]}
        price_filter = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        market_lot = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
        return cls(
            tick_size=Decimal(price_filter["tickSize"]),
            step_size=Decimal(lot["stepSize"]),
            market_step_size=Decimal(market_lot["stepSize"]),
            min_qty=Decimal(lot["minQty"]),
            max_qty=Decimal(lot["maxQty"]),
            min_notional=Decimal(
                str(notional.get("notional", notional.get("minNotional", "0")))
            ),
        )


def _fmt_decimal(value: Decimal) -> str:
    return format(value, "f")


def round_step(value: Decimal, step: Decimal, rounding: str = ROUND_FLOOR) -> Decimal:
    if value < 0 or step <= 0:
        raise ValueError("value must be non-negative and step must be positive")
    return (value / step).to_integral_value(rounding=rounding) * step


def entry_price_for(side: str, close: Decimal, tick: Decimal) -> Decimal:
    if side == "long":
        return round_step(close * (Decimal("1") - ENTRY_OFFSET), tick, ROUND_FLOOR)
    return round_step(close * (Decimal("1") + ENTRY_OFFSET), tick, ROUND_CEILING)


def exit_prices_for(side: str, entry: Decimal, tick: Decimal) -> tuple[Decimal, Decimal]:
    if side == "long":
        target = round_step(entry * (Decimal("1") + TAKE_PROFIT_RATE), tick, ROUND_CEILING)
        stop = round_step(entry * (Decimal("1") - STOP_RATE), tick, ROUND_FLOOR)
    else:
        target = round_step(entry * (Decimal("1") - TAKE_PROFIT_RATE), tick, ROUND_FLOOR)
        stop = round_step(entry * (Decimal("1") + STOP_RATE), tick, ROUND_CEILING)
    return target, stop


def risk_quantity(
    equity: Decimal, entry: Decimal, side: str, rules: SymbolRules
) -> Decimal | None:
    """Size so the scheduled 3.5% stop loses at most 3% before fees."""
    if equity <= 0 or entry <= 0:
        return None
    _, rounded_stop = exit_prices_for(side, entry, rules.tick_size)
    loss_per_unit = abs(entry - rounded_stop)
    if loss_per_unit <= 0:
        return None
    raw = equity * ACCOUNT_RISK_RATE / loss_per_unit
    qty = round_step(raw, rules.step_size, ROUND_FLOOR)
    qty = min(qty, rules.max_qty)
    if qty < rules.min_qty or qty * entry < rules.min_notional:
        return None
    return qty


def ema_adjust_false(values: Iterable[float], span: int) -> list[float]:
    items = list(values)
    if not items:
        return []
    alpha = 2.0 / (span + 1.0)
    result = [items[0]]
    for value in items[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def cci_series(candles: list[Candle], window: int = CCI_WINDOW) -> list[float | None]:
    typical = [(bar.high + bar.low + bar.close) / 3.0 for bar in candles]
    result: list[float | None] = [None] * len(candles)
    for index in range(window - 1, len(typical)):
        sample = typical[index - window + 1 : index + 1]
        mean = sum(sample) / window
        deviation = sum(abs(value - mean) for value in sample) / window
        result[index] = (
            0.0 if deviation == 0 else (typical[index] - mean) / (0.015 * deviation)
        )
    return result


def bb_z_series(candles: list[Candle], window: int = BB_WINDOW) -> list[float | None]:
    closes = [bar.close for bar in candles]
    result: list[float | None] = [None] * len(candles)
    for index in range(window - 1, len(closes)):
        sample = closes[index - window + 1 : index + 1]
        mean = sum(sample) / window
        std = statistics.pstdev(sample)
        result[index] = 0.0 if std == 0 else (closes[index] - mean) / std
    return result


def calculate_signal(candles: list[Candle]) -> Signal | None:
    if len(candles) < WARMUP_BARS:
        return None
    closes = [bar.close for bar in candles]
    ema = ema_adjust_false(closes, EMA_SPAN)[-1]
    cci = cci_series(candles)
    z_values = bb_z_series(candles)
    current_cci = cci[-1]
    previous_cci = cci[-2]
    recent_z = z_values[-EXTREME_LOOKBACK:]
    if current_cci is None or previous_cci is None or any(v is None for v in recent_z):
        return None
    z_numbers = [float(value) for value in recent_z if value is not None]
    close = closes[-1]
    if (
        min(z_numbers) < -BB_Z_THRESHOLD
        and previous_cci < -CCI_THRESHOLD
        and current_cci >= -CCI_THRESHOLD
        and close > ema
    ):
        return Signal("long", min(z_numbers), previous_cci, current_cci, ema)
    if (
        max(z_numbers) > BB_Z_THRESHOLD
        and previous_cci > CCI_THRESHOLD
        and current_cci <= CCI_THRESHOLD
        and close < ema
    ):
        return Signal("short", max(z_numbers), previous_cci, current_cci, ema)
    return None


def utc_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def kst_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, KST).isoformat()


class EventJournal:
    def __init__(self, path: Path, live: bool):
        self.path = path
        self.live = live
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, timestamp_ms: int | None = None, **fields: Any) -> None:
        timestamp_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        record = {
            "timestamp_utc": utc_iso(timestamp_ms),
            "timestamp_kst": kst_iso(timestamp_ms),
            "mode": "live" if self.live else "paper",
            "strategy_id": STRATEGY_ID,
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        LOG.info("%s %s", event, json.dumps(fields, ensure_ascii=False, default=str))


class StateStore:
    def __init__(self, path: Path, live: bool, initial_equity: Decimal):
        self.path = path
        self.live = live
        self.initial_equity = initial_equity
        path.parent.mkdir(parents=True, exist_ok=True)

    def default(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "mode": "live" if self.live else "paper",
            "status": "flat",
            "last_bar_open_time_ms": None,
            "cooldown_until_ms": 0,
            "paper_equity": _fmt_decimal(self.initial_equity),
            "pending": None,
            "position": None,
        }

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.default()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        expected_mode = "live" if self.live else "paper"
        if state.get("version") != STATE_VERSION or state.get("mode") != expected_mode:
            raise SafetyHalt(f"State file is incompatible with this bot/mode: {self.path}")
        if state.get("status") not in {"flat", "pending", "position"}:
            raise SafetyHalt("State file has an invalid status")
        return state

    def save(self, state: Mapping[str, Any]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class BinanceClient:
    def __init__(self, config: Config):
        self.base_url = config.rest_url
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.timeout = config.http_timeout_seconds
        self.recv_window = config.recv_window_ms
        self.time_offset_ms = 0

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        signed: bool = False,
    ) -> Any:
        values = {key: value for key, value in (params or {}).items() if value is not None}
        headers = {"User-Agent": "telegram-message-trading/reversion-live"}
        if signed:
            if not self.api_key or not self.api_secret:
                raise SafetyHalt("A signed Binance request was attempted without credentials")
            values["timestamp"] = self.now_ms()
            values["recvWindow"] = self.recv_window
            encoded = urlencode(values)
            values["signature"] = hmac.new(
                self.api_secret.encode("utf-8"),
                encoded.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key
        encoded = urlencode(values).encode("ascii")
        if method in {"GET", "DELETE"}:
            url = self.base_url + path
            if encoded:
                url += "?" + encoded.decode("ascii")
            request = Request(url, method=method, headers=headers)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request = Request(
                self.base_url + path, data=encoded, method=method, headers=headers
            )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                code = int(payload["code"]) if "code" in payload else None
                message = str(payload.get("msg", "request rejected"))[:500]
            except (ValueError, TypeError, json.JSONDecodeError):
                code = None
                message = "request rejected"
            raise BinanceAPIError(status, code, message) from exc
        except URLError as exc:
            raise RuntimeError(f"Binance network error: {exc.reason}") from exc

    def sync_time(self) -> int:
        started = int(time.time() * 1000)
        payload = self._request("GET", "/fapi/v1/time")
        finished = int(time.time() * 1000)
        midpoint = (started + finished) // 2
        self.time_offset_ms = int(payload["serverTime"]) - midpoint
        return self.time_offset_ms

    def exchange_info(self) -> Any:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def klines(
        self,
        symbol: str,
        *,
        limit: int = 1500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        return self._request(
            "GET",
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": "1m",
                "limit": limit,
                "startTime": start_time,
                "endTime": end_time,
            },
        )

    def account(self) -> Mapping[str, Any]:
        return self._request("GET", "/fapi/v3/account", signed=True)

    def position_risk(self, symbol: str | None = None) -> list[Mapping[str, Any]]:
        return self._request(
            "GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True
        )

    def symbol_config(self, symbol: str) -> Mapping[str, Any]:
        rows = self._request(
            "GET", "/fapi/v1/symbolConfig", {"symbol": symbol}, signed=True
        )
        if not rows:
            raise SafetyHalt(f"No Binance account configuration found for {symbol}")
        return rows[0]

    def multi_assets_mode(self) -> Mapping[str, Any]:
        return self._request("GET", "/fapi/v1/multiAssetsMargin", signed=True)

    def open_orders(self, symbol: str | None = None) -> list[Mapping[str, Any]]:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    def open_algo_orders(self, symbol: str | None = None) -> list[Mapping[str, Any]]:
        return self._request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            {"symbol": symbol, "algoType": "CONDITIONAL"},
            signed=True,
        )

    def position_mode(self) -> Mapping[str, Any]:
        return self._request("GET", "/fapi/v1/positionSide/dual", signed=True)

    def set_one_way_mode(self) -> Any:
        return self._request(
            "POST",
            "/fapi/v1/positionSide/dual",
            {"dualSidePosition": "false"},
            signed=True,
        )

    def set_margin_type(self, symbol: str) -> Any:
        return self._request(
            "POST",
            "/fapi/v1/marginType",
            {"symbol": symbol, "marginType": "ISOLATED"},
            signed=True,
        )

    def set_leverage(self, symbol: str, leverage: int = 1) -> Any:
        return self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    def new_order(self, **params: Any) -> Mapping[str, Any]:
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def query_order(self, symbol: str, order_id: int) -> Mapping[str, Any]:
        return self._request(
            "GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    def cancel_order(self, symbol: str, order_id: int) -> Mapping[str, Any]:
        return self._request(
            "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True
        )

    def new_algo_order(self, **params: Any) -> Mapping[str, Any]:
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def cancel_algo_order(self, symbol: str, algo_id: int) -> Mapping[str, Any]:
        return self._request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {"symbol": symbol, "algoId": algo_id},
            signed=True,
        )


def fetch_recent_completed(
    api: BinanceClient, symbol: str, count: int = HISTORY_BARS
) -> list[Candle]:
    now_ms = api.now_ms()
    end_time = (now_ms // INTERVAL_MS) * INTERVAL_MS - 1
    rows: list[list[Any]] = []
    while len(rows) < count:
        requested = min(1500, count - len(rows))
        batch = api.klines(symbol, limit=requested, end_time=end_time)
        if not batch:
            break
        rows = batch + rows
        end_time = int(batch[0][0]) - 1
        if len(batch) < requested:
            break
    candles = [Candle.from_binance(row) for row in rows]
    unique = {bar.open_time_ms: bar for bar in candles if bar.close_time_ms <= now_ms}
    return [unique[key] for key in sorted(unique)][-count:]


def fetch_completed_after(
    api: BinanceClient, symbol: str, after_open_time_ms: int
) -> list[Candle]:
    now_ms = api.now_ms()
    cursor = after_open_time_ms + INTERVAL_MS
    result: list[Candle] = []
    while cursor <= now_ms:
        rows = api.klines(symbol, limit=1500, start_time=cursor, end_time=now_ms)
        if not rows:
            break
        batch = [Candle.from_binance(row) for row in rows]
        result.extend(bar for bar in batch if bar.close_time_ms <= now_ms)
        next_cursor = int(rows[-1][0]) + INTERVAL_MS
        if next_cursor <= cursor or len(rows) < 1500:
            break
        cursor = next_cursor
    unique = {bar.open_time_ms: bar for bar in result if bar.open_time_ms > after_open_time_ms}
    return [unique[key] for key in sorted(unique)]


def _owned_regular(order: Mapping[str, Any]) -> bool:
    return str(order.get("clientOrderId", "")).startswith(CLIENT_ID_PREFIX)


def _owned_algo(order: Mapping[str, Any]) -> bool:
    return str(order.get("clientAlgoId", "")).startswith(CLIENT_ID_PREFIX)


def _nonzero_position_rows(
    positions: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        position
        for position in positions
        if Decimal(str(position.get("positionAmt", "0"))) != 0
    ]


def _client_id(kind: str) -> str:
    return f"{CLIENT_ID_PREFIX}{kind}-{int(time.time())}-{secrets.token_hex(3)}"[:36]


class StrategyBot:
    def __init__(
        self,
        config: Config,
        api: BinanceClient,
        rules: SymbolRules,
        journal: EventJournal,
        store: StateStore,
        state: dict[str, Any],
    ):
        self.config = config
        self.api = api
        self.rules = rules
        self.journal = journal
        self.store = store
        self.state = state
        self.history: list[Candle] = []

    def _save(self) -> None:
        self.store.save(self.state)

    def _assert_live_symbol_config(self) -> None:
        symbol_config = self.api.symbol_config(self.config.symbol)
        if (
            str(symbol_config.get("marginType", "")).upper() != "ISOLATED"
            or int(symbol_config.get("leverage", 0)) != 1
        ):
            raise SafetyHalt("BTCUSDT must be configured as ISOLATED with 1x leverage")

    def initialize_history(self, candles: list[Candle]) -> None:
        if len(candles) < WARMUP_BARS:
            raise RuntimeError(f"Only {len(candles)} completed candles were available")
        if any(
            current.open_time_ms - previous.open_time_ms != INTERVAL_MS
            for previous, current in zip(candles, candles[1:])
        ):
            raise SafetyHalt("Binance warm-up candles are not contiguous 1-minute bars")
        self.history = candles[-HISTORY_BARS:]
        latest = self.history[-1]
        previous = self.state.get("last_bar_open_time_ms")
        if previous is None:
            self.state["last_bar_open_time_ms"] = latest.open_time_ms
        elif int(previous) > latest.open_time_ms:
            raise SafetyHalt("State file points to a future Binance candle")
        elif int(previous) < latest.open_time_ms:
            missed = [bar for bar in self.history if bar.open_time_ms > int(previous)]
            if not self.config.live_trading and self.state["status"] != "flat":
                for bar in missed:
                    self._paper_bar(bar, allow_signal=False)
            self.state["last_bar_open_time_ms"] = latest.open_time_ms
            self.journal.emit(
                "STARTUP_GAP_SKIPPED",
                timestamp_ms=latest.close_time_ms,
                bars=len(missed),
                active_state=self.state["status"],
            )
        self._save()

    def process_candle(self, candle: Candle) -> None:
        if self.history and candle.open_time_ms <= self.history[-1].open_time_ms:
            return
        if self.history and candle.open_time_ms - self.history[-1].open_time_ms != INTERVAL_MS:
            raise SafetyHalt("A gap was detected in Binance 1-minute candles")
        self.history.append(candle)
        self.history = self.history[-HISTORY_BARS:]
        if self.config.live_trading:
            if self.state["status"] == "flat" and candle.close_time_ms >= int(
                self.state.get("cooldown_until_ms", 0)
            ):
                signal = calculate_signal(self.history)
                if signal:
                    self._record_signal(candle, signal)
                    self._place_live_entry(candle, signal)
        else:
            self._paper_bar(candle, allow_signal=True)
        self.state["last_bar_open_time_ms"] = candle.open_time_ms
        self._save()

    def _record_signal(self, candle: Candle, signal: Signal) -> None:
        self.journal.emit(
            "SIGNAL",
            timestamp_ms=candle.close_time_ms,
            symbol=self.config.symbol,
            side=signal.side,
            close=candle.close,
            z_extreme=signal.z_extreme,
            cci_previous=signal.cci_previous,
            cci_current=signal.cci_current,
            ema600=signal.ema,
            signal_bar_open_kst=kst_iso(candle.open_time_ms),
        )

    def _paper_bar(self, candle: Candle, *, allow_signal: bool) -> None:
        if self.state["status"] == "position":
            self._paper_position_bar(candle, allow_target=True)
            return
        if self.state["status"] == "pending":
            pending = self.state["pending"]
            if candle.open_time_ms >= int(pending["expires_at_ms"]):
                self.journal.emit(
                    "PAPER_ENTRY_EXPIRED",
                    timestamp_ms=candle.open_time_ms,
                    side=pending["side"],
                    limit_price=pending["limit_price"],
                )
                self.state.update(status="flat", pending=None)
            elif candle.open_time_ms > int(pending["signal_bar_open_time_ms"]):
                limit_price = Decimal(pending["limit_price"])
                first_bar = (
                    candle.open_time_ms
                    == int(pending["signal_bar_open_time_ms"]) + INTERVAL_MS
                )
                marketable_at_submission = first_bar and (
                    (pending["side"] == "long" and Decimal(str(candle.open)) <= limit_price)
                    or (
                        pending["side"] == "short"
                        and Decimal(str(candle.open)) >= limit_price
                    )
                )
                if marketable_at_submission:
                    self.journal.emit(
                        "PAPER_ENTRY_REJECTED",
                        timestamp_ms=candle.open_time_ms,
                        reason="post_only_marketable_at_next_bar_open",
                        side=pending["side"],
                        limit_price=pending["limit_price"],
                        next_bar_open=candle.open,
                    )
                    self.state.update(status="flat", pending=None)
                else:
                    threshold = (
                        limit_price * (Decimal("1") - PAPER_FILL_PENETRATION)
                        if pending["side"] == "long"
                        else limit_price * (Decimal("1") + PAPER_FILL_PENETRATION)
                    )
                    filled = (
                        Decimal(str(candle.low)) < threshold
                        if pending["side"] == "long"
                        else Decimal(str(candle.high)) > threshold
                    )
                    if filled:
                        target, stop = exit_prices_for(
                            pending["side"], limit_price, self.rules.tick_size
                        )
                        self.state["status"] = "position"
                        self.state["position"] = {
                            "side": pending["side"],
                            "qty": pending["qty"],
                            "entry_price": pending["limit_price"],
                            "entry_time_ms": candle.open_time_ms,
                            "entry_bar_open_time_ms": candle.open_time_ms,
                            "target_price": _fmt_decimal(target),
                            "stop_price": _fmt_decimal(stop),
                        }
                        self.state["pending"] = None
                        self.journal.emit(
                            "PAPER_ENTRY_FILLED",
                            timestamp_ms=candle.open_time_ms,
                            **self.state["position"],
                        )
                        self._paper_position_bar(candle, allow_target=False)
                        return
            if self.state["status"] != "flat":
                return
        if not allow_signal or candle.close_time_ms < int(
            self.state.get("cooldown_until_ms", 0)
        ):
            return
        signal = calculate_signal(self.history)
        if signal is None:
            return
        self._record_signal(candle, signal)
        close = Decimal(str(candle.close))
        limit_price = entry_price_for(signal.side, close, self.rules.tick_size)
        equity = Decimal(self.state["paper_equity"])
        qty = risk_quantity(equity, limit_price, signal.side, self.rules)
        if qty is None:
            self.journal.emit(
                "PAPER_ENTRY_SKIPPED",
                timestamp_ms=candle.close_time_ms,
                reason="quantity_below_exchange_minimum",
                equity=_fmt_decimal(equity),
                limit_price=_fmt_decimal(limit_price),
            )
            return
        self.state["status"] = "pending"
        self.state["pending"] = {
            "side": signal.side,
            "qty": _fmt_decimal(qty),
            "limit_price": _fmt_decimal(limit_price),
            "signal_bar_open_time_ms": candle.open_time_ms,
            "placed_at_ms": candle.close_time_ms,
            "expires_at_ms": candle.close_time_ms + ENTRY_TTL_MS,
        }
        _, rounded_stop = exit_prices_for(signal.side, limit_price, self.rules.tick_size)
        self.journal.emit(
            "PAPER_ENTRY_PLACED",
            timestamp_ms=candle.close_time_ms,
            **self.state["pending"],
            equity=_fmt_decimal(equity),
            risk_before_fees=_fmt_decimal(qty * abs(limit_price - rounded_stop)),
        )

    def _paper_position_bar(self, candle: Candle, *, allow_target: bool) -> None:
        position = self.state["position"]
        side = position["side"]
        entry = Decimal(position["entry_price"])
        target = Decimal(position["target_price"])
        stop = Decimal(position["stop_price"])
        low = Decimal(str(candle.low))
        high = Decimal(str(candle.high))
        bar_open = Decimal(str(candle.open))
        reason: str | None = None
        exit_price: Decimal | None = None
        maker_exit = False
        stop_hit = low <= stop if side == "long" else high >= stop
        target_hit = (
            high > target * (Decimal("1") + PAPER_FILL_PENETRATION)
            if side == "long"
            else low < target * (Decimal("1") - PAPER_FILL_PENETRATION)
        )
        if stop_hit:
            reason = "stop"
            raw_stop = stop
            if allow_target:
                raw_stop = min(bar_open, stop) if side == "long" else max(bar_open, stop)
            exit_price = raw_stop * (
                Decimal("1") - self.config.paper_market_slippage
                if side == "long"
                else Decimal("1") + self.config.paper_market_slippage
            )
        elif allow_target and target_hit:
            reason = "target"
            exit_price = target
            maker_exit = True
        elif candle.close_time_ms - int(position["entry_time_ms"]) >= MAX_HOLD_MS:
            reason = "max_hold"
            close = Decimal(str(candle.close))
            exit_price = close * (
                Decimal("1") - self.config.paper_market_slippage
                if side == "long"
                else Decimal("1") + self.config.paper_market_slippage
            )
        if reason is None or exit_price is None:
            return
        qty = Decimal(position["qty"])
        direction = Decimal("1") if side == "long" else Decimal("-1")
        gross = direction * qty * (exit_price - entry)
        entry_fee = qty * entry * self.config.paper_maker_fee
        exit_fee_rate = (
            self.config.paper_maker_fee if maker_exit else self.config.paper_taker_fee
        )
        exit_fee = qty * exit_price * exit_fee_rate
        equity_before = Decimal(self.state["paper_equity"])
        equity_after = equity_before + gross - entry_fee - exit_fee
        self.state["paper_equity"] = _fmt_decimal(equity_after)
        self.state["status"] = "flat"
        self.state["position"] = None
        self.state["cooldown_until_ms"] = candle.close_time_ms + COOLDOWN_MS
        self.journal.emit(
            "PAPER_EXIT",
            timestamp_ms=candle.close_time_ms,
            reason=reason,
            side=side,
            qty=_fmt_decimal(qty),
            entry_price=_fmt_decimal(entry),
            exit_price=_fmt_decimal(exit_price),
            gross_pnl=_fmt_decimal(gross),
            fees=_fmt_decimal(entry_fee + exit_fee),
            net_pnl=_fmt_decimal(gross - entry_fee - exit_fee),
            equity_before=_fmt_decimal(equity_before),
            equity_after=_fmt_decimal(equity_after),
            cooldown_until_kst=kst_iso(self.state["cooldown_until_ms"]),
        )

    def bootstrap_live(self) -> None:
        account = self.api.account()
        positions = _nonzero_position_rows(self.api.position_risk())
        regular = self.api.open_orders()
        algo = self.api.open_algo_orders()
        foreign_regular = [order for order in regular if not _owned_regular(order)]
        foreign_algo = [order for order in algo if not _owned_algo(order)]
        other_positions = [p for p in positions if p.get("symbol") != self.config.symbol]
        if foreign_regular or foreign_algo or other_positions:
            raise SafetyHalt(
                "Dedicated-account check failed: unrelated position or unowned open order exists"
            )
        if len(positions) > 1:
            raise SafetyHalt("More than one non-zero futures position exists")
        if _api_bool(self.api.multi_assets_mode().get("multiAssetsMargin")):
            raise SafetyHalt("Multi-Assets Mode is unsupported; use Single-Asset Mode")
        hedge_mode = _api_bool(self.api.position_mode().get("dualSidePosition"))
        if hedge_mode:
            if positions or regular or algo:
                raise SafetyHalt("Hedge mode cannot be changed while positions/orders exist")
            self.api.set_one_way_mode()
            self.journal.emit("LIVE_POSITION_MODE_SET", position_mode="ONE_WAY")
        if not positions and not regular and not algo:
            try:
                self.api.set_margin_type(self.config.symbol)
            except BinanceAPIError as exc:
                if exc.code != -4046:
                    raise
            leverage = self.api.set_leverage(self.config.symbol, 1)
            if int(leverage.get("leverage", 0)) != 1:
                raise SafetyHalt("Binance did not confirm 1x leverage")
            self.journal.emit(
                "LIVE_ACCOUNT_CONFIGURED", margin_type="ISOLATED", leverage=1
            )
        self._assert_live_symbol_config()
        if positions:
            position = positions[0]
            if position.get("symbol") != self.config.symbol:
                raise SafetyHalt("Unexpected symbol position exists")
            for order in regular:
                if not _api_bool(order.get("reduceOnly")):
                    self._cancel_regular(int(order["orderId"]))
            self._adopt_live_position(position, account, regular, algo, recovered=True)
        elif regular:
            entries = [order for order in regular if not _api_bool(order.get("reduceOnly"))]
            exits = [order for order in regular if _api_bool(order.get("reduceOnly"))]
            if len(entries) != 1 or exits or algo:
                raise SafetyHalt("Managed orders do not describe one recoverable pending entry")
            order = entries[0]
            self.state["status"] = "pending"
            self.state["position"] = None
            placed_at = int(order.get("time", self.api.now_ms()))
            self.state["pending"] = {
                "side": "long" if order["side"] == "BUY" else "short",
                "qty": str(order["origQty"]),
                "limit_price": str(order["price"]),
                "order_id": int(order["orderId"]),
                "client_order_id": str(order["clientOrderId"]),
                "placed_at_ms": placed_at,
                "expires_at_ms": placed_at + ENTRY_TTL_MS,
            }
            self.journal.emit("LIVE_PENDING_RECOVERED", **self.state["pending"])
        elif algo:
            for order in algo:
                self._cancel_algo(int(order["algoId"]))
            self.state.update(status="flat", pending=None, position=None)
            self.journal.emit("LIVE_ORPHAN_PROTECTION_REMOVED", count=len(algo))
        else:
            self.state.update(status="flat", pending=None, position=None)
        self._save()

    def _adopt_live_position(
        self,
        position: Mapping[str, Any],
        account: Mapping[str, Any],
        regular: list[Mapping[str, Any]],
        algo: list[Mapping[str, Any]],
        *,
        recovered: bool,
    ) -> None:
        amount = Decimal(str(position["positionAmt"]))
        side = "long" if amount > 0 else "short"
        qty = abs(amount)
        entry = Decimal(str(position["entryPrice"]))
        if entry <= 0:
            raise SafetyHalt("Binance returned a non-positive live entry price")
        target, stop = exit_prices_for(side, entry, self.rules.tick_size)
        old = self.state.get("position") or {}
        entry_time = int(
            old.get("entry_time_ms") or position.get("updateTime") or self.api.now_ms()
        )
        pending = self.state.get("pending") or {}
        equity = str(
            old.get("entry_equity")
            or pending.get("entry_equity")
            or account.get("totalMarginBalance", "0")
        )
        self.state["status"] = "position"
        self.state["pending"] = None
        self.state["position"] = {
            "side": side,
            "qty": _fmt_decimal(qty),
            "entry_price": _fmt_decimal(entry),
            "entry_time_ms": entry_time,
            "target_price": _fmt_decimal(target),
            "stop_price": _fmt_decimal(stop),
            "entry_equity": equity,
            "target_order_id": old.get("target_order_id"),
            "stop_algo_id": old.get("stop_algo_id"),
        }
        if recovered:
            self.journal.emit("LIVE_POSITION_RECOVERED", **self.state["position"])
        self._ensure_live_protection(regular, algo)
        self._save()

    def _place_live_entry(self, candle: Candle, signal: Signal) -> None:
        self._assert_live_symbol_config()
        account = self.api.account()
        positions = _nonzero_position_rows(self.api.position_risk())
        regular = self.api.open_orders()
        algo = self.api.open_algo_orders()
        if positions or regular or algo:
            raise SafetyHalt("Account changed before entry; refusing to submit a new order")
        equity = Decimal(str(account.get("totalMarginBalance", "0")))
        available = Decimal(str(account.get("availableBalance", "0")))
        limit_price = entry_price_for(
            signal.side, Decimal(str(candle.close)), self.rules.tick_size
        )
        qty = risk_quantity(equity, limit_price, signal.side, self.rules)
        if qty is None:
            self.journal.emit(
                "LIVE_ENTRY_SKIPPED",
                timestamp_ms=candle.close_time_ms,
                reason="quantity_below_exchange_minimum",
                equity=_fmt_decimal(equity),
            )
            return
        notional = qty * limit_price
        if available < notional:
            self.journal.emit(
                "LIVE_ENTRY_SKIPPED",
                timestamp_ms=candle.close_time_ms,
                reason="insufficient_available_balance_at_1x",
                available_balance=_fmt_decimal(available),
                required_notional=_fmt_decimal(notional),
            )
            return
        client_id = _client_id("entry")
        side = "BUY" if signal.side == "long" else "SELL"
        try:
            order = self.api.new_order(
                symbol=self.config.symbol,
                side=side,
                positionSide="BOTH",
                type="LIMIT",
                timeInForce="GTX",
                quantity=_fmt_decimal(qty),
                price=_fmt_decimal(limit_price),
                newClientOrderId=client_id,
                newOrderRespType="RESULT",
            )
        except BinanceAPIError as exc:
            self.journal.emit(
                "LIVE_ENTRY_REJECTED",
                timestamp_ms=candle.close_time_ms,
                code=exc.code,
                message=exc.api_message,
                side=signal.side,
                limit_price=_fmt_decimal(limit_price),
            )
            return
        now_ms = self.api.now_ms()
        self.state["status"] = "pending"
        self.state["pending"] = {
            "side": signal.side,
            "qty": _fmt_decimal(qty),
            "limit_price": _fmt_decimal(limit_price),
            "order_id": int(order["orderId"]),
            "client_order_id": client_id,
            "placed_at_ms": now_ms,
            "expires_at_ms": now_ms + ENTRY_TTL_MS,
            "entry_equity": _fmt_decimal(equity),
        }
        _, rounded_stop = exit_prices_for(signal.side, limit_price, self.rules.tick_size)
        self.journal.emit(
            "LIVE_ENTRY_PLACED",
            timestamp_ms=candle.close_time_ms,
            **self.state["pending"],
            time_in_force="GTX",
            risk_before_fees=_fmt_decimal(qty * abs(limit_price - rounded_stop)),
        )
        self._save()

    def reconcile_live(self) -> None:
        if not self.config.live_trading:
            return
        account = self.api.account()
        positions = _nonzero_position_rows(self.api.position_risk())
        other = [p for p in positions if p.get("symbol") != self.config.symbol]
        if other or len(positions) > 1:
            raise SafetyHalt("Unexpected futures position detected while bot was running")
        regular = self.api.open_orders(self.config.symbol)
        algo = self.api.open_algo_orders(self.config.symbol)
        if any(not _owned_regular(order) for order in regular) or any(
            not _owned_algo(order) for order in algo
        ):
            raise SafetyHalt("Unowned BTCUSDT order detected while bot was running")
        if positions:
            amount = Decimal(str(positions[0]["positionAmt"]))
            live_side = "long" if amount > 0 else "short"
            live_qty = abs(amount)
            if self.state["status"] == "position":
                expected = self.state["position"]
                if live_side != expected["side"] or live_qty > (
                    Decimal(expected["qty"]) + self.rules.step_size
                ):
                    raise SafetyHalt("Live position direction or quantity increased unexpectedly")
            if self.state["status"] == "pending" and live_qty > (
                Decimal(self.state["pending"]["qty"]) + self.rules.step_size
            ):
                raise SafetyHalt("Entry fill exceeds the bot's requested quantity")
            if self.state["status"] == "pending":
                pending = self.state["pending"]
                self._cancel_regular(int(pending["order_id"]))
                order = self._query_regular_tolerant(int(pending["order_id"]))
                position = positions[0]
                if order and Decimal(str(order.get("avgPrice", "0"))) > 0:
                    position = dict(position)
                    position["entryPrice"] = order["avgPrice"]
                    position["updateTime"] = order.get("updateTime", self.api.now_ms())
                self._adopt_live_position(position, account, regular, algo, recovered=False)
                self.journal.emit("LIVE_ENTRY_FILLED", **self.state["position"])
            else:
                self._adopt_live_position(positions[0], account, regular, algo, recovered=False)
            position_state = self.state["position"]
            if self.api.now_ms() - int(position_state["entry_time_ms"]) >= MAX_HOLD_MS:
                self._market_close("max_hold")
            return
        if self.state["status"] == "pending":
            pending = self.state["pending"]
            order = self._query_regular_tolerant(int(pending["order_id"]))
            if order is None or order.get("status") in {"CANCELED", "EXPIRED", "REJECTED"}:
                self.journal.emit(
                    "LIVE_ENTRY_ENDED",
                    reason="exchange_status",
                    status=None if order is None else order.get("status"),
                )
                self.state.update(status="flat", pending=None)
                self._save()
            elif self.api.now_ms() >= int(pending["expires_at_ms"]):
                self._cancel_regular(int(pending["order_id"]))
                order = self._query_regular_tolerant(int(pending["order_id"]))
                if order and Decimal(str(order.get("executedQty", "0"))) > 0:
                    self.state["cooldown_until_ms"] = self.api.now_ms() + COOLDOWN_MS
                    self.journal.emit(
                        "LIVE_ENTRY_FILLED_BUT_FLAT",
                        note="Position endpoint is flat; any later position is recovered next poll",
                    )
                self.state.update(status="flat", pending=None)
                self.journal.emit("LIVE_ENTRY_EXPIRED", order_id=pending["order_id"])
                self._save()
            return
        if self.state["status"] == "position":
            self._cancel_owned_orders(regular, algo)
            self.state.update(status="flat", pending=None, position=None)
            self.state["cooldown_until_ms"] = self.api.now_ms() + COOLDOWN_MS
            self.journal.emit(
                "LIVE_POSITION_CLOSED",
                cooldown_until_kst=kst_iso(self.state["cooldown_until_ms"]),
            )
            self._save()
        elif regular or algo:
            self._cancel_owned_orders(regular, algo)
            self.journal.emit("LIVE_ORPHAN_ORDERS_REMOVED")

    def _ensure_live_protection(
        self,
        regular: list[Mapping[str, Any]] | None = None,
        algo: list[Mapping[str, Any]] | None = None,
    ) -> None:
        position = self.state["position"]
        regular = regular if regular is not None else self.api.open_orders(self.config.symbol)
        algo = algo if algo is not None else self.api.open_algo_orders(self.config.symbol)
        targets = [order for order in regular if _api_bool(order.get("reduceOnly"))]
        stops = [
            order
            for order in algo
            if str(order.get("orderType", order.get("type", ""))) == "STOP_MARKET"
            and _api_bool(order.get("closePosition"))
        ]
        for order in regular:
            if not _api_bool(order.get("reduceOnly")):
                self._cancel_regular(int(order["orderId"]))
        for extra in targets[1:]:
            self._cancel_regular(int(extra["orderId"]))
        for extra in stops[1:]:
            self._cancel_algo(int(extra["algoId"]))
        stop_ids = {int(order["algoId"]) for order in stops}
        for order in algo:
            if int(order["algoId"]) not in stop_ids:
                self._cancel_algo(int(order["algoId"]))
        expected_stop_side = "SELL" if position["side"] == "long" else "BUY"
        if stops:
            current_stop = stops[0]
            stop_valid = (
                str(current_stop.get("side")) == expected_stop_side
                and Decimal(str(current_stop.get("triggerPrice", "0")))
                == Decimal(position["stop_price"])
                and str(current_stop.get("workingType", "CONTRACT_PRICE"))
                == "CONTRACT_PRICE"
            )
            if not stop_valid:
                self._cancel_algo(int(current_stop["algoId"]))
                stops = []
        if not stops:
            try:
                response = self.api.new_algo_order(
                    algoType="CONDITIONAL",
                    symbol=self.config.symbol,
                    side=expected_stop_side,
                    positionSide="BOTH",
                    type="STOP_MARKET",
                    triggerPrice=position["stop_price"],
                    workingType="CONTRACT_PRICE",
                    closePosition="true",
                    priceProtect="false",
                    clientAlgoId=_client_id("stop"),
                    newOrderRespType="RESULT",
                )
                position["stop_algo_id"] = int(response["algoId"])
                self.journal.emit(
                    "LIVE_STOP_PLACED",
                    algo_id=position["stop_algo_id"],
                    trigger_price=position["stop_price"],
                    close_position=True,
                )
            except Exception:
                LOG.exception("Stop placement failed; issuing emergency reduce-only close")
                self._market_close("stop_placement_failure")
                raise
        else:
            position["stop_algo_id"] = int(stops[0]["algoId"])
        expected_qty = Decimal(position["qty"])
        expected_target_side = "SELL" if position["side"] == "long" else "BUY"
        if targets:
            current_target = targets[0]
            current_qty = Decimal(str(current_target.get("origQty", "0")))
            target_valid = (
                abs(current_qty - expected_qty) < self.rules.step_size
                and str(current_target.get("side")) == expected_target_side
                and Decimal(str(current_target.get("price", "0")))
                == Decimal(position["target_price"])
                and str(current_target.get("timeInForce")) == "GTX"
            )
            if not target_valid:
                self._cancel_regular(int(current_target["orderId"]))
                targets = []
        if not targets:
            try:
                response = self.api.new_order(
                    symbol=self.config.symbol,
                    side=expected_target_side,
                    positionSide="BOTH",
                    type="LIMIT",
                    timeInForce="GTX",
                    quantity=position["qty"],
                    price=position["target_price"],
                    reduceOnly="true",
                    newClientOrderId=_client_id("target"),
                    newOrderRespType="RESULT",
                )
                position["target_order_id"] = int(response["orderId"])
                self.journal.emit(
                    "LIVE_TARGET_PLACED",
                    order_id=position["target_order_id"],
                    price=position["target_price"],
                    qty=position["qty"],
                    time_in_force="GTX",
                )
            except BinanceAPIError as exc:
                position["target_order_id"] = None
                self.journal.emit(
                    "LIVE_TARGET_NOT_POSTED",
                    code=exc.code,
                    message=exc.api_message,
                    note="No market fallback; the stop remains active and retry occurs next poll",
                )
        else:
            position["target_order_id"] = int(targets[0]["orderId"])
        self._save()

    def _market_close(self, reason: str) -> None:
        position = self.state.get("position")
        if not position:
            return
        regular = self.api.open_orders(self.config.symbol)
        for order in regular:
            if _owned_regular(order):
                self._cancel_regular(int(order["orderId"]))
        live_positions = _nonzero_position_rows(
            self.api.position_risk(self.config.symbol)
        )
        if not live_positions:
            self.journal.emit("LIVE_MARKET_EXIT_NOT_NEEDED", reason=reason)
            return
        if len(live_positions) != 1:
            raise SafetyHalt("Expected exactly one BTCUSDT position before market exit")
        amount = Decimal(str(live_positions[0]["positionAmt"]))
        live_side = "long" if amount > 0 else "short"
        if live_side != position["side"]:
            raise SafetyHalt("Position direction changed before market exit")
        qty = round_step(abs(amount), self.rules.market_step_size, ROUND_FLOOR)
        if qty < self.rules.min_qty:
            raise SafetyHalt("Position quantity is below the market-close minimum")
        response = self.api.new_order(
            symbol=self.config.symbol,
            side="SELL" if position["side"] == "long" else "BUY",
            positionSide="BOTH",
            type="MARKET",
            quantity=_fmt_decimal(qty),
            reduceOnly="true",
            newClientOrderId=_client_id("exit"),
            newOrderRespType="RESULT",
        )
        self.journal.emit(
            "LIVE_MARKET_EXIT_SUBMITTED",
            reason=reason,
            order_id=response.get("orderId"),
            qty=_fmt_decimal(qty),
        )

    def _query_regular_tolerant(self, order_id: int) -> Mapping[str, Any] | None:
        try:
            return self.api.query_order(self.config.symbol, order_id)
        except BinanceAPIError as exc:
            if exc.code in {-2011, -2013}:
                return None
            raise

    def _cancel_regular(self, order_id: int) -> None:
        try:
            self.api.cancel_order(self.config.symbol, order_id)
        except BinanceAPIError as exc:
            if exc.code not in {-2011, -2013}:
                raise

    def _cancel_algo(self, algo_id: int) -> None:
        try:
            self.api.cancel_algo_order(self.config.symbol, algo_id)
        except BinanceAPIError as exc:
            if exc.code not in {-2011, -2013}:
                raise

    def _cancel_owned_orders(
        self,
        regular: list[Mapping[str, Any]],
        algo: list[Mapping[str, Any]],
    ) -> None:
        for order in regular:
            if _owned_regular(order):
                self._cancel_regular(int(order["orderId"]))
        for order in algo:
            if _owned_algo(order):
                self._cancel_algo(int(order["algoId"]))


def build_bot(config: Config) -> StrategyBot:
    api = BinanceClient(config)
    api.sync_time()
    rules = SymbolRules.from_exchange_info(api.exchange_info(), config.symbol)
    journal = EventJournal(config.log_path, config.live_trading)
    store = StateStore(config.state_path, config.live_trading, config.paper_initial_equity)
    state = store.load()
    bot = StrategyBot(config, api, rules, journal, store, state)
    if config.live_trading:
        bot.bootstrap_live()
    candles = fetch_recent_completed(api, config.symbol)
    bot.initialize_history(candles)
    journal.emit(
        "BOT_STARTED",
        symbol=config.symbol,
        interval="1m",
        execution_venue="BINANCE_USDM",
        live_trading=config.live_trading,
        last_completed_bar_kst=kst_iso(candles[-1].open_time_ms),
        paper_equity=None if config.live_trading else state["paper_equity"],
    )
    return bot


def run_forever(bot: StrategyBot) -> None:
    delay = bot.config.poll_seconds
    while True:
        try:
            if bot.config.live_trading:
                bot.reconcile_live()
            last_open = int(bot.state["last_bar_open_time_ms"])
            for candle in fetch_completed_after(bot.api, bot.config.symbol, last_open):
                bot.process_candle(candle)
            delay = bot.config.poll_seconds
            time.sleep(delay)
        except SafetyHalt:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            LOG.exception("Transient bot loop error")
            if isinstance(exc, BinanceAPIError) and exc.code == -1021:
                bot.api.sync_time()
            bot.journal.emit(
                "LOOP_ERROR",
                error_type=type(exc).__name__,
                message=str(exc)[:500],
                retry_seconds=delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    loaded = load_dotenv_files()
    if loaded:
        LOG.info("Loaded environment file(s): %s", ", ".join(str(path) for path in loaded))
    try:
        config = Config.from_env()
        LOG.warning(
            "Starting BTC strategy mode=%s (fees are excluded from the 3%% stop-risk budget)",
            "LIVE" if config.live_trading else "PAPER",
        )
        bot = build_bot(config)
        run_forever(bot)
    except KeyboardInterrupt:
        LOG.info("Stopped by operator")
        return 0
    except Exception:
        LOG.exception("Bot stopped")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
