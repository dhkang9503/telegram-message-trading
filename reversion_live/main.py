"""Binance USD-M full-period regime ensemble bot.

The original asymmetric snapback strategy always runs as a virtual shadow
strategy.  A causal EWM of completed shadow-trade returns selects between:

* gate ON: copy the snapback signal at the validated 1.6 risk scale;
* gate OFF: hold the strongest BTC/ETH/SOL 15m EMA(32/384) trend with
  volatility targeting and a 5x exposure ceiling.

Signals use completed candles and the account holds at most one real position.
LIVE_TRADING defaults to 0.  No order is submitted unless it is explicitly 1.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

D = lambda x, default="0": Decimal(default) if x is None or x == "" else Decimal(str(x))


@dataclass(frozen=True)
class Profile:
    notional_mult: Decimal
    tp_atr: Decimal
    sl_atr: Decimal
    max_hold_minutes: int
    hard_stop: Decimal


# Original snapback profiles.  Research modules import this name, so it remains
# the unchanged virtual-shadow definition.
PROFILES = {
    "BTCUSDT": Profile(D("5"), D("8"), D("6"), 48 * 60, D("0.20")),
    "ETHUSDT": Profile(D("9"), D("2"), D("3"), 24 * 60, D("0.20")),
    "SOLUSDT": Profile(D("3"), D("5"), D("4"), 6 * 60, D("0.15")),
}
SYMBOLS = tuple(PROFILES)  # fixed priority: BTC > ETH > SOL

# The validated live-copy layer applies the selected 1.6 risk scale to both
# notional and account-level hard loss.  This preserves each profile's price
# stop distance while scaling PnL exactly as in the final direct audit.
SNAPBACK_LIVE_PROFILES = {
    symbol: Profile(
        profile.notional_mult * D("1.6"),
        profile.tp_atr,
        profile.sl_atr,
        profile.max_hold_minutes,
        profile.hard_stop * D("1.6"),
    )
    for symbol, profile in PROFILES.items()
}

STRATEGY_ID = "full_period_regime_ensemble_v1"
STATE_VERSION = 3

# Exchange leverage controls reserved margin, not strategy exposure.  Twenty
# times is above the largest validated notional multiplier (14.4x) while
# avoiding needless dependence on symbol-specific 98x availability.
LEVERAGE = int(os.getenv("BINANCE_EXCHANGE_LEVERAGE", "20"))
if not 15 <= LEVERAGE <= 125:
    raise ValueError("BINANCE_EXCHANGE_LEVERAGE must be between 15 and 125")
BB_PERIOD = 20
EMA_PERIOD = 20
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
ATR_MEDIAN_LOOKBACK = 100
INDICATOR_FETCH_LIMIT = 500
FALLBACK_FETCH_LIMIT = 1500

GATE_SPAN = 40
GATE_THRESHOLD = D("0.0025")
GATE_ALPHA = D(2) / D(GATE_SPAN + 1)
SHADOW_COST = D("0.0005")

FALLBACK_FAST = 32
FALLBACK_SLOW = 384
FALLBACK_VOL_WINDOW = 32
FALLBACK_MIN_HOLD = 48
FALLBACK_SWITCH_MULTIPLE = D("1.25")
FALLBACK_TARGET_ANNUAL_VOL = D("1.2")
FALLBACK_MAX_LEVERAGE = D("5")
FALLBACK_BARS_PER_YEAR = D(365 * 24 * 4)
FALLBACK_TARGET_BAR_VOL = FALLBACK_TARGET_ANNUAL_VOL / FALLBACK_BARS_PER_YEAR.sqrt()

# Signal thresholds from the 2024-08-09 .. 2026-08-14 search.
BTC_BB_Z = D("2.75")
BTC_RSI_MIN = D("65")
BTC_RSI_REVERSAL = D("12")
BTC_ADX_MAX = D("35")
BTC_ATR_RATIO_MAX = D("3.0")

ETH_BB_Z = D("2.50")
ETH_RSI_MAX = D("30")
ETH_RSI_REVERSAL = D("11")
ETH_ADX_MAX = D("70")
ETH_ATR_RATIO_MAX = D("1.5")

SOL_KELTNER_ATR = D("2.25")
SOL_RSI_LONG_MAX = D("25")
SOL_RSI_SHORT_MIN = D("75")
SOL_RSI_REVERSAL = D("8")
SOL_ADX_MAX = D("35")
SOL_ATR_RATIO_MAX = D("3.0")

TAKER_FEE = D("0.0004")
SIGNAL_INTERVAL_MS = 15 * 60 * 1000
SIGNAL_MAX_AGE_MS = int(float(os.getenv("SIGNAL_MAX_AGE_SECONDS", "10")) * 1000)
BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")
LIVE = os.getenv("LIVE_TRADING", "0") == "1"
SIZING_CAP = D(os.getenv("SIZING_EQUITY_CAP_USDT", "0"))
INITIAL_EQUITY_MIN = D(os.getenv("INITIAL_EQUITY_MIN_USDT", "45"))
INITIAL_EQUITY_MAX = D(os.getenv("INITIAL_EQUITY_MAX_USDT", "55"))
if INITIAL_EQUITY_MIN <= 0 or INITIAL_EQUITY_MAX < INITIAL_EQUITY_MIN:
    raise ValueError("invalid initial equity guard range")
POLL = float(os.getenv("POLL_SECONDS", "1"))
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"
STATE_PATH = Path(os.getenv("REVERSION_STATE_PATH", str(Path(__file__).with_name("state.json"))))
INSTANCE_LOCK_PATH = Path(
    os.getenv("BOT_INSTANCE_LOCK_PATH", str(STATE_PATH.with_suffix(STATE_PATH.suffix + ".lock")))
)
RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW_MS", "5000"))
TIMEOUT = float(os.getenv("BINANCE_HTTP_TIMEOUT_SECONDS", "10"))
ALLOW_COLD_GATE = os.getenv("ALLOW_COLD_GATE", "0") == "1"
SHADOW_SEED_FILES = tuple(
    Path(x)
    for x in os.getenv(
        "SHADOW_GATE_SEED_FILES",
        os.pathsep.join(
            str(Path(__file__).with_name("results") / name)
            for name in (
                "presample_snapback_corrected_trades.csv",
                "recent_snapback_corrected_trades.csv",
            )
        ),
    ).split(os.pathsep)
    if x
)
EMBEDDED_GATE_SEED = {
    "completed_count": 292,
    "ewm": "0.03108061800816574691247015836",
    "gate_on": True,
    "seeded_through_ms": 1786369200000,
    "last_signal": {
        "BTCUSDT": 1786314599999,
        "ETHUSDT": 1784296799999,
        "SOLUSDT": 1785984299999,
    },
}

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("reversion_live")
STOP = False


class BotError(RuntimeError):
    pass


class OrderBelowMinimum(BotError):
    pass


class BinanceError(BotError):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"Binance error status={status} code={code}: {msg}")
        self.status, self.code = status, code


class InstanceLock:
    """OS-released advisory lock preventing two processes from trading one state."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b", buffering=0)
        try:
            self.handle.seek(0)
            if self.handle.read(1) == b"":
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise BotError(f"another bot process holds {self.path}") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def ds(x: Decimal) -> str:
    s = format(x.normalize(), "f")
    return "0" if s == "-0" else s


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def blank_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "strategy": STRATEGY_ID,
        "active_symbol": None,
        "active_strategy": None,
        "side": None,
        "cycle_equity": None,
        "account_equity_at_start": None,
        "entry_atr": None,
        "signal_reference": None,
        "notional_mult": None,
        "opened_at_ms": None,
        "signal_close_time": None,
        "live_account_initialized": False,
        "initial_live_equity": None,
        "last_signal": {},
        "last_strategy_bar_close": 0,
        "shadow": {
            "completed_count": 0,
            "ewm": None,
            "gate_on": False,
            "position": None,
            "seeded": False,
        },
        "fallback": {
            "desired": 0,
            "held_bars": 0,
            "score": None,
            "vol": None,
            "leverage": None,
        },
        "pending_order": None,
        "updated_at": int(time.time()),
    }


class State:
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

    def save(self) -> None:
        self.data["updated_at"] = int(time.time())
        save_json(self.path, self.data)

    def reset_cycle(self) -> None:
        preserved = {
            "last_signal": dict(self.data.get("last_signal", {})),
            "last_strategy_bar_close": int(self.data.get("last_strategy_bar_close", 0)),
            "shadow": self.data.get("shadow", blank_state()["shadow"]),
            "fallback": self.data.get("fallback", blank_state()["fallback"]),
            "live_account_initialized": bool(
                self.data.get("live_account_initialized", False)
            ),
            "initial_live_equity": self.data.get("initial_live_equity"),
        }
        self.data = blank_state()
        self.data.update(preserved)
        self.legacy = False
        self.save()


@dataclass(frozen=True)
class Rules:
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def floor(self, qty: Decimal) -> Decimal:
        return (qty / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step


@dataclass(frozen=True)
class Position:
    symbol: str
    amount: Decimal
    entry: Decimal
    breakeven: Decimal
    mark: Decimal
    upnl: Decimal
    liq: Decimal

    @property
    def open(self) -> bool:
        return self.amount != 0

    @property
    def side(self) -> str:
        return "long" if self.amount > 0 else "short" if self.amount < 0 else "flat"

    @property
    def qty(self) -> Decimal:
        return abs(self.amount)


@dataclass(frozen=True)
class Signal:
    side: str
    close_time: int
    atr: Decimal
    reference_close: Decimal
    detail: str


class Binance:
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_API_SECRET", "")
        self.offset = 0
        self.last_sync = 0.0
        if LIVE and (not self.key or not self.secret):
            raise BotError("LIVE_TRADING=1 requires BINANCE_API_KEY and BINANCE_API_SECRET")

    def req(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        p = dict(params or {})
        if signed:
            if not self.key or not self.secret:
                raise BotError("signed call requires Binance credentials")
            self.sync_time()
            p.update(
                {
                    "timestamp": int(time.time() * 1000) + self.offset,
                    "recvWindow": RECV_WINDOW,
                }
            )
        clean = {
            k: (
                ds(v)
                if isinstance(v, Decimal)
                else str(v).lower()
                if isinstance(v, bool)
                else v
            )
            for k, v in p.items()
        }
        q = urlencode(clean)
        if signed:
            q += ("&" if q else "") + "signature=" + hmac.new(
                self.secret.encode(), q.encode(), hashlib.sha256
            ).hexdigest()
        url = BASE_URL + path + (("?" + q) if q else "")
        headers = {"Accept": "application/json", "User-Agent": "reversion-live/2.0"}
        if self.key:
            headers["X-MBX-APIKEY"] = self.key
        try:
            with urlopen(Request(url, method=method, headers=headers), timeout=TIMEOUT) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"msg": raw}
            raise BinanceError(
                e.code,
                int(payload["code"]) if "code" in payload else None,
                str(payload.get("msg", raw)),
            ) from e
        except URLError as e:
            raise BotError(f"Binance network error: {e}") from e

    def sync_time(self) -> None:
        if time.monotonic() - self.last_sync < 300:
            return
        server = self.req("GET", "/fapi/v1/time")["serverTime"]
        self.offset = int(server) - int(time.time() * 1000)
        self.last_sync = time.monotonic()

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 120,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return self.req(
            "GET", "/fapi/v1/klines", params
        )

    def funding_rates(
        self, symbol: str, start_time: int, end_time: int
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = start_time
        while cursor <= end_time:
            page = self.req(
                "GET",
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": end_time,
                    "limit": 1000,
                },
            )
            if not page:
                break
            out.extend(page)
            newest = max(int(row["fundingTime"]) for row in page)
            if newest < cursor or len(page) < 1000:
                break
            cursor = newest + 1
        return out

    def account(self) -> dict[str, Any]:
        return self.req("GET", "/fapi/v3/account", signed=True)

    def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        x = self.req(
            "GET",
            "/fapi/v3/positionRisk",
            {"symbol": symbol} if symbol else {},
            signed=True,
        )
        return x if isinstance(x, list) else [x]

    def open_orders(self) -> list[dict[str, Any]]:
        return self.req("GET", "/fapi/v1/openOrders", signed=True)

    def position_mode(self) -> bool:
        x = self.req("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"]
        return x if isinstance(x, bool) else str(x).lower() == "true"

    def set_one_way(self) -> None:
        self.req(
            "POST",
            "/fapi/v1/positionSide/dual",
            {"dualSidePosition": "false"},
            True,
        )

    def configure(self, symbol: str) -> Decimal:
        try:
            self.req(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": "CROSSED"},
                True,
            )
        except BinanceError as e:
            if e.code != -4046:
                raise
        x = self.req(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": LEVERAGE},
            True,
        )
        if int(x.get("leverage", 0)) != LEVERAGE:
            raise BotError(f"{symbol} did not accept {LEVERAGE}x")
        return D(x.get("maxNotionalValue"))

    def order(
        self, symbol: str, side: str, qty: Decimal, reduce: bool, cid: str
    ) -> dict[str, Any]:
        p: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": cid,
            "newOrderRespType": "RESULT",
        }
        if reduce:
            p["reduceOnly"] = "true"
        return self.req("POST", "/fapi/v1/order", p, True)

    def get_order(self, symbol: str, cid: str) -> dict[str, Any]:
        return self.req(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": cid},
            True,
        )


def closed(rows: list[list[Any]]) -> list[list[Any]]:
    now = int(time.time() * 1000)
    return [r for r in rows if int(r[6]) < now]


def ewm(values: list[Decimal], alpha: Decimal, min_periods: int) -> list[Decimal | None]:
    if not values:
        return []
    out: list[Decimal | None] = [None] * len(values)
    cur = values[0]
    for i, x in enumerate(values):
        if i:
            cur = (D(1) - alpha) * cur + alpha * x
        if i >= min_periods - 1:
            out[i] = cur
    return out


def atr_series(rows: list[list[Any]]) -> list[Decimal | None]:
    if not rows:
        return []
    tr: list[Decimal] = []
    for i, r in enumerate(rows):
        h, l = D(r[2]), D(r[3])
        if i == 0:
            tr.append(h - l)
        else:
            prev_close = D(rows[i - 1][4])
            tr.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    return ewm(tr, D(1) / D(ATR_PERIOD), ATR_PERIOD)


def ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    if not values:
        return []
    alpha = D(2) / D(period + 1)
    cur = values[0]
    out = [cur]
    for x in values[1:]:
        cur = (D(1) - alpha) * cur + alpha * x
        out.append(cur)
    return out


def rsi_series(closes: list[Decimal]) -> list[Decimal | None]:
    if len(closes) < 2:
        return [None] * len(closes)
    gains = [D(0)]
    losses = [D(0)]
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, D(0)))
        losses.append(max(-delta, D(0)))
    avg_gain = ewm(gains, D(1) / D(RSI_PERIOD), RSI_PERIOD)
    avg_loss = ewm(losses, D(1) / D(RSI_PERIOD), RSI_PERIOD)
    out: list[Decimal | None] = [None] * len(closes)
    for i, (g, l) in enumerate(zip(avg_gain, avg_loss)):
        if g is None or l is None:
            continue
        if l == 0:
            out[i] = D(100) if g > 0 else D(50)
        else:
            rs = g / l
            out[i] = D(100) - D(100) / (D(1) + rs)
    return out


def adx_series(rows: list[list[Any]]) -> list[Decimal | None]:
    if not rows:
        return []
    tr = [D(0)]
    plus_dm = [D(0)]
    minus_dm = [D(0)]
    for i in range(1, len(rows)):
        h, l = D(rows[i][2]), D(rows[i][3])
        ph, pl, pc = D(rows[i - 1][2]), D(rows[i - 1][3]), D(rows[i - 1][4])
        up = h - ph
        down = pl - l
        plus_dm.append(up if up > down and up > 0 else D(0))
        minus_dm.append(down if down > up and down > 0 else D(0))
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))

    alpha = D(1) / D(ADX_PERIOD)
    sm_tr = ewm(tr, alpha, ADX_PERIOD)
    sm_plus = ewm(plus_dm, alpha, ADX_PERIOD)
    sm_minus = ewm(minus_dm, alpha, ADX_PERIOD)
    dx_values: list[Decimal] = []
    dx_indexes: list[int] = []
    for i in range(len(rows)):
        if sm_tr[i] is None or sm_tr[i] == 0:
            continue
        pdi = D(100) * sm_plus[i] / sm_tr[i]
        mdi = D(100) * sm_minus[i] / sm_tr[i]
        denom = pdi + mdi
        dx = D(0) if denom == 0 else D(100) * abs(pdi - mdi) / denom
        dx_values.append(dx)
        dx_indexes.append(i)

    out: list[Decimal | None] = [None] * len(rows)
    if not dx_values:
        return out
    adx_vals = ewm(dx_values, alpha, ADX_PERIOD)
    for idx, value in zip(dx_indexes, adx_vals):
        if value is not None:
            out[idx] = value
    return out


def rolling_z(closes: list[Decimal], i: int, period: int = BB_PERIOD) -> Decimal | None:
    if i < period - 1:
        return None
    w = closes[i - period + 1 : i + 1]
    mean = sum(w, D(0)) / D(period)
    var = sum((x - mean) ** 2 for x in w) / D(period)
    sd = var.sqrt()
    return D(0) if sd == 0 else (closes[i] - mean) / sd


def atr_ratio(atrs: list[Decimal | None], i: int) -> Decimal | None:
    cur = atrs[i]
    if cur is None or cur <= 0:
        return None
    start = max(0, i - ATR_MEDIAN_LOOKBACK + 1)
    hist = [x for x in atrs[start : i + 1] if x is not None and x > 0]
    if len(hist) < ATR_MEDIAN_LOOKBACK:
        return None
    med = D(statistics.median(hist))
    return None if med <= 0 else cur / med


def signal_for(
    api: Binance, symbol: str, supplied_rows: list[list[Any]] | None = None
) -> Signal | None:
    rows = supplied_rows if supplied_rows is not None else closed(
        api.klines(symbol, "15m", INDICATOR_FETCH_LIMIT)
    )
    needed = max(
        BB_PERIOD,
        RSI_PERIOD,
        ATR_PERIOD,
        ADX_PERIOD * 2,
        ATR_MEDIAN_LOOKBACK + ATR_PERIOD,
    ) + 2
    if len(rows) < needed:
        return None

    closes = [D(r[4]) for r in rows]
    atrs = atr_series(rows)
    rsis = rsi_series(closes)
    adxs = adx_series(rows)
    emas = ema_series(closes, EMA_PERIOD)
    p, c = len(rows) - 2, len(rows) - 1

    pa, ca = atrs[p], atrs[c]
    prsi, crsi = rsis[p], rsis[c]
    cadx = adxs[c]
    ratio = atr_ratio(atrs, c)
    if None in (pa, ca, prsi, crsi, cadx, ratio):
        return None
    assert pa is not None and ca is not None
    assert prsi is not None and crsi is not None
    assert cadx is not None and ratio is not None

    close_time = int(rows[c][6])

    if symbol == "BTCUSDT":
        pz = rolling_z(closes, p)
        cz = rolling_z(closes, c)
        if pz is None or cz is None:
            return None
        if (
            pz > BTC_BB_Z
            and prsi > BTC_RSI_MIN
            and cz < BTC_BB_Z
            and prsi - crsi >= BTC_RSI_REVERSAL
            and cadx <= BTC_ADX_MAX
            and ratio <= BTC_ATR_RATIO_MAX
        ):
            detail = (
                f"z={ds(pz)}->{ds(cz)} rsi={ds(prsi)}->{ds(crsi)} "
                f"adx={ds(cadx)} atr_ratio={ds(ratio)}"
            )
            return Signal("short", close_time, ca, closes[c], detail)
        return None

    if symbol == "ETHUSDT":
        pz = rolling_z(closes, p)
        cz = rolling_z(closes, c)
        if pz is None or cz is None:
            return None
        if (
            pz < -ETH_BB_Z
            and prsi < ETH_RSI_MAX
            and cz > -ETH_BB_Z
            and crsi - prsi >= ETH_RSI_REVERSAL
            and cadx <= ETH_ADX_MAX
            and ratio <= ETH_ATR_RATIO_MAX
        ):
            detail = (
                f"z={ds(pz)}->{ds(cz)} rsi={ds(prsi)}->{ds(crsi)} "
                f"adx={ds(cadx)} atr_ratio={ds(ratio)}"
            )
            return Signal("long", close_time, ca, closes[c], detail)
        return None

    if symbol == "SOLUSDT":
        upper_p = emas[p] + SOL_KELTNER_ATR * pa
        lower_p = emas[p] - SOL_KELTNER_ATR * pa
        upper_c = emas[c] + SOL_KELTNER_ATR * ca
        lower_c = emas[c] - SOL_KELTNER_ATR * ca
        common = cadx <= SOL_ADX_MAX and ratio <= SOL_ATR_RATIO_MAX
        if (
            common
            and closes[p] < lower_p
            and prsi < SOL_RSI_LONG_MAX
            and closes[c] > lower_c
            and crsi - prsi >= SOL_RSI_REVERSAL
        ):
            detail = (
                f"lower={ds(lower_p)}->{ds(lower_c)} close={ds(closes[p])}->{ds(closes[c])} "
                f"rsi={ds(prsi)}->{ds(crsi)} adx={ds(cadx)} atr_ratio={ds(ratio)}"
            )
            return Signal("long", close_time, ca, closes[c], detail)
        if (
            common
            and closes[p] > upper_p
            and prsi > SOL_RSI_SHORT_MIN
            and closes[c] < upper_c
            and prsi - crsi >= SOL_RSI_REVERSAL
        ):
            detail = (
                f"upper={ds(upper_p)}->{ds(upper_c)} close={ds(closes[p])}->{ds(closes[c])} "
                f"rsi={ds(prsi)}->{ds(crsi)} adx={ds(cadx)} atr_ratio={ds(ratio)}"
            )
            return Signal("short", close_time, ca, closes[c], detail)
        return None

    raise BotError(f"unsupported symbol {symbol}")


def latest_trade(api: Binance, symbol: str) -> tuple[Decimal, int]:
    rows = api.klines(symbol, "5m", 2)
    if not rows:
        raise BotError(f"trade price unavailable for {symbol}")
    # Binance's current kline close updates with the latest trade while the candle is open.
    return D(rows[-1][4]), int(rows[-1][0])


def latest_completed_15m_close_time(now_ms: int | None = None) -> int:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return (now_ms // SIGNAL_INTERVAL_MS) * SIGNAL_INTERVAL_MS - 1


def parse_pos(x: dict[str, Any]) -> Position:
    return Position(
        str(x["symbol"]),
        D(x.get("positionAmt")),
        D(x.get("entryPrice")),
        D(x.get("breakEvenPrice") or x.get("entryPrice")),
        D(x.get("markPrice")),
        D(x.get("unRealizedProfit")),
        D(x.get("liquidationPrice")),
    )


def open_positions(api: Binance) -> list[Position]:
    return [p for p in map(parse_pos, api.positions()) if p.open]


def one_position(api: Binance, symbol: str) -> Position:
    rows = [
        parse_pos(x)
        for x in api.positions(symbol)
        if str(x.get("symbol")) == symbol
    ]
    opens = [p for p in rows if p.open]
    if len(opens) > 1:
        raise BotError(f"multiple live rows for {symbol}")
    return (
        opens[0]
        if opens
        else rows[0]
        if rows
        else Position(symbol, D(0), D(0), D(0), D(0), D(0), D(0))
    )


def load_rules(api: Binance) -> dict[str, Rules]:
    info = api.req("GET", "/fapi/v1/exchangeInfo")
    out: dict[str, Rules] = {}
    for s in info.get("symbols", []):
        if s.get("symbol") not in SYMBOLS:
            continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("MARKET_LOT_SIZE") or fs["LOT_SIZE"]
        if D(lot.get("stepSize")) <= 0:
            lot = fs["LOT_SIZE"]
        nf = fs.get("MIN_NOTIONAL") or fs.get("NOTIONAL") or {}
        out[s["symbol"]] = Rules(
            D(lot["stepSize"]),
            D(lot["minQty"]),
            D(nf.get("notional") or nf.get("minNotional")),
        )
    if set(out) != set(SYMBOLS):
        raise BotError("missing symbol filters")
    return out


def timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def seed_shadow_gate(state: State) -> None:
    """Initialize the causal gate from completed audited shadow trades.

    A fresh live state cannot reproduce a 40-trade gate from a short REST
    window.  The checked audit ledgers are therefore the explicit seed.  Once
    seeded, every later shadow trade is simulated and persisted locally.
    """
    shadow = state.data["shadow"]
    if shadow.get("seeded") or int(shadow.get("completed_count", 0)) > 0:
        return
    now_ms = int(time.time() * 1000)
    events: list[tuple[int, int, str, Decimal]] = []
    missing: list[str] = []
    for path in SHADOW_SEED_FILES:
        if not path.exists():
            missing.append(str(path))
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                entry_ms = timestamp_ms(row["entry_time"])
                exit_ms = timestamp_ms(row["exit_time"])
                if exit_ms <= now_ms:
                    events.append((entry_ms, exit_ms, row["symbol"], D(row["net_return"])))
    if missing or len(events) < GATE_SPAN:
        if now_ms >= int(EMBEDDED_GATE_SEED["seeded_through_ms"]):
            shadow.update(
                {
                    "completed_count": EMBEDDED_GATE_SEED["completed_count"],
                    "ewm": EMBEDDED_GATE_SEED["ewm"],
                    "gate_on": EMBEDDED_GATE_SEED["gate_on"],
                    "position": None,
                    "seeded": True,
                    "seeded_through_ms": EMBEDDED_GATE_SEED["seeded_through_ms"],
                }
            )
            state.data["last_signal"].update(EMBEDDED_GATE_SEED["last_signal"])
            state.save()
            LOG.warning(
                "GATE_SEEDED_EMBEDDED completed=%s ewm=%s sampled_gate=%s through=%s",
                shadow["completed_count"],
                shadow["ewm"],
                shadow["gate_on"],
                shadow["seeded_through_ms"],
            )
            return
        message = (
            "shadow gate seed unavailable; missing=" + ",".join(missing)
            if missing
            else f"shadow gate seed has {len(events)} trades; {GATE_SPAN} required"
        )
        if LIVE and not ALLOW_COLD_GATE:
            raise BotError(message + "; set ALLOW_COLD_GATE=1 only to start a new 40-trade warmup")
        LOG.warning("%s; gate starts cold", message)
        shadow["seeded"] = True
        state.save()
        return

    count = 0
    current: Decimal | None = None
    sampled_gate = False
    latest_exit = 0
    last_signal = dict(state.data.get("last_signal", {}))
    for entry_ms, exit_ms, symbol, trade_return in sorted(events):
        if trade_return <= -D(1):
            raise BotError(f"invalid seeded shadow return={trade_return} at {entry_ms}")
        sampled_gate = count >= GATE_SPAN and current is not None and current > GATE_THRESHOLD
        current = (
            trade_return
            if current is None
            else (D(1) - GATE_ALPHA) * current + GATE_ALPHA * trade_return
        )
        count += 1
        latest_exit = max(latest_exit, exit_ms)
        last_signal[symbol] = max(int(last_signal.get(symbol, 0)), entry_ms - 1)

    shadow.update(
        {
            "completed_count": count,
            "ewm": ds(current) if current is not None else None,
            "gate_on": sampled_gate,
            "position": None,
            "seeded": True,
            "seeded_through_ms": latest_exit,
        }
    )
    state.data["last_signal"] = last_signal
    state.save()
    LOG.warning(
        "GATE_SEEDED completed=%s ewm=%s sampled_gate=%s through=%s",
        count,
        ds(current) if current is not None else "n/a",
        sampled_gate,
        latest_exit,
    )


def rolling_return_vol(closes: list[Decimal], window: int) -> list[Decimal | None]:
    returns: list[Decimal | None] = [None]
    returns.extend(closes[i] / closes[i - 1] - D(1) for i in range(1, len(closes)))
    out: list[Decimal | None] = [None] * len(closes)
    for i in range(window, len(closes)):
        sample = [x for x in returns[i - window + 1 : i + 1] if x is not None]
        if len(sample) != window:
            continue
        mean = sum(sample, D(0)) / D(window)
        variance = sum((x - mean) ** 2 for x in sample) / D(window)
        out[i] = variance.sqrt()
    return out


def fallback_snapshot(rows_by_symbol: dict[str, list[list[Any]]]) -> dict[str, Any]:
    """Reconstruct the locked EMA selector from aligned completed 15m rows."""
    maps = {
        symbol: {int(row[6]): row for row in rows}
        for symbol, rows in rows_by_symbol.items()
    }
    common = sorted(set.intersection(*(set(rows) for rows in maps.values())))
    if len(common) < FALLBACK_SLOW + FALLBACK_VOL_WINDOW:
        raise BotError(f"fallback warmup unavailable: only {len(common)} common candles")
    closes = {
        symbol: [D(maps[symbol][close_time][4]) for close_time in common]
        for symbol in SYMBOLS
    }
    fast = {symbol: ema_series(values, FALLBACK_FAST) for symbol, values in closes.items()}
    slow = {symbol: ema_series(values, FALLBACK_SLOW) for symbol, values in closes.items()}
    vols = {
        symbol: rolling_return_vol(values, FALLBACK_VOL_WINDOW)
        for symbol, values in closes.items()
    }
    scale = D(FALLBACK_SLOW - FALLBACK_FAST).sqrt()
    score_rows: list[dict[str, Decimal | None]] = []
    for i in range(len(common)):
        scores: dict[str, Decimal | None] = {}
        for symbol in SYMBOLS:
            vol = vols[symbol][i]
            scores[symbol] = (
                None
                if vol is None or vol <= 0
                else (fast[symbol][i] / slow[symbol][i] - D(1)) / (vol * scale)
            )
        score_rows.append(scores)

    current_symbol: str | None = None
    current_dir = 0
    held = 0
    for scores in score_rows:
        best_symbol: str | None = None
        best_strength = D("-1")
        best_dir = 0
        for symbol in SYMBOLS:
            value = scores[symbol]
            if value is not None and abs(value) >= 0 and abs(value) > best_strength:
                best_symbol = symbol
                best_strength = abs(value)
                best_dir = 1 if value > 0 else -1
        keep = False
        if current_symbol is not None:
            value = scores[current_symbol]
            if value is not None and (1 if value > 0 else -1) == current_dir:
                keep = True
                if (
                    held >= FALLBACK_MIN_HOLD
                    and best_symbol is not None
                    and best_symbol != current_symbol
                    and best_strength > abs(value) * FALLBACK_SWITCH_MULTIPLE
                ):
                    keep = False
        if keep:
            held += 1
        elif best_symbol is not None:
            current_symbol = best_symbol
            current_dir = best_dir
            held = 1
        else:
            current_symbol = None
            current_dir = 0
            held = 0

    if current_symbol is None:
        return {
            "close_time": common[-1], "desired": 0, "held_bars": 0,
            "score": None, "vol": None, "leverage": None,
        }
    latest_score = score_rows[-1][current_symbol]
    latest_vol = vols[current_symbol][-1]
    assert latest_score is not None and latest_vol is not None
    leverage = min(FALLBACK_MAX_LEVERAGE, FALLBACK_TARGET_BAR_VOL / latest_vol)
    desired = (SYMBOLS.index(current_symbol) + 1) * current_dir
    return {
        "close_time": common[-1],
        "desired": desired,
        "held_bars": held,
        "score": latest_score,
        "vol": latest_vol,
        "leverage": leverage,
    }


class Bot:
    def __init__(self, api: Binance, state: State, rules: dict[str, Rules]):
        self.api, self.state, self.rules = api, state, rules
        self.last_signal_scan = 0.0
        self.last_shadow_sync = 0.0

    def qty(self, symbol: str, notional: Decimal, price: Decimal) -> Decimal:
        r = self.rules[symbol]
        q = r.floor(notional / price)
        if (
            q < r.min_qty
            or q <= 0
            or (r.min_notional > 0 and q * price < r.min_notional)
        ):
            raise OrderBelowMinimum(
                f"{symbol} order below exchange minimum; bot will not auto-upsize"
            )
        return q

    def reconcile_pending(self) -> bool:
        x = self.state.data.get("pending_order")
        if not x:
            return False
        try:
            order = self.api.get_order(x["symbol"], x["cid"])
        except BinanceError as e:
            if e.code == -2013:
                self.state.data["pending_order"] = None
                self.state.save()
                return False
            raise BotError(f"cannot reconcile pending order: {e}") from e
        if order.get("status") != "FILLED":
            raise BotError(f"pending order status={order.get('status')}; manual review")
        return True

    def market(
        self, symbol: str, side: str, qty: Decimal, reduce: bool, action: str
    ) -> None:
        if self.state.data.get("pending_order") and self.reconcile_pending():
            raise BotError("previous market order filled but local transition is incomplete")
        cid = (
            f"rv{action}{int(time.time() * 1000) % 10**10}{secrets.token_hex(3)}"
        )[:36]
        self.state.data["pending_order"] = {
            "symbol": symbol,
            "cid": cid,
            "action": action,
            "qty": ds(qty),
        }
        self.state.save()
        try:
            order = self.api.order(symbol, side, qty, reduce, cid)
        except (BinanceError, BotError) as e:
            if self.reconcile_pending():
                return
            raise BotError(f"market order not confirmed: {e}") from e
        if order.get("status") not in (None, "FILLED"):
            raise BotError(f"unexpected market status={order.get('status')}")

    def clear_pending(self) -> None:
        self.state.data["pending_order"] = None
        self.state.save()

    def bootstrap(self) -> None:
        if not LIVE:
            LOG.warning("LIVE_TRADING=0: signal-only mode")
            if self.state.legacy:
                LOG.warning("legacy state is not migrated in signal-only mode")
                return
            seed_shadow_gate(self.state)
            return

        pos = open_positions(self.api)

        if self.state.legacy:
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
            raise BotError("recovered FILLED pending order; manual state reconciliation required")

        if len(pos) > 1:
            raise BotError("more than one futures position is open")
        if self.api.open_orders():
            raise BotError("existing USD-M open orders block this bot")

        if pos:
            p = pos[0]
            active_strategy = self.state.data.get("active_strategy")
            required = ["active_symbol", "side", "cycle_equity", "opened_at_ms"]
            if active_strategy == "snapback":
                required.extend(("entry_atr", "signal_reference"))
            if (
                self.state.data.get("strategy") != STRATEGY_ID
                or active_strategy not in ("snapback", "fallback")
                or p.symbol not in SYMBOLS
                or self.state.data.get("active_symbol") != p.symbol
                or self.state.data.get("side") != p.side
                or not self.state.data.get("live_account_initialized")
                or any(self.state.data.get(k) is None for k in required)
            ):
                raise BotError(f"unknown/incompatible live position: {p}")
            return

        if self.state.data.get("active_symbol"):
            self.state.reset_cycle()

        if not self.state.data.get("live_account_initialized"):
            account = self.api.account()
            actual = D(
                account.get("totalMarginBalance") or account.get("totalWalletBalance")
            )
            if not INITIAL_EQUITY_MIN <= actual <= INITIAL_EQUITY_MAX:
                raise BotError(
                    f"initial futures equity {ds(actual)} is outside the configured "
                    f"{ds(INITIAL_EQUITY_MIN)}..{ds(INITIAL_EQUITY_MAX)} USDT guard"
                )
            self.state.data["live_account_initialized"] = True
            self.state.data["initial_live_equity"] = ds(actual)
            self.state.save()
            LOG.warning(
                "LIVE_ACCOUNT_INITIALIZED equity=%s guard=%s..%s sizing_cap=%s",
                ds(actual),
                ds(INITIAL_EQUITY_MIN),
                ds(INITIAL_EQUITY_MAX),
                ds(SIZING_CAP),
            )

        if self.api.position_mode():
            self.api.set_one_way()

    def open_signal(self, symbol: str, s: Signal) -> None:
        if open_positions(self.api) or self.api.open_orders():
            return
        if self.api.position_mode():
            raise BotError("one-way mode required")

        profile = SNAPBACK_LIVE_PROFILES[symbol]
        max_notional = self.api.configure(symbol)
        account = self.api.account()
        actual = D(account.get("totalMarginBalance") or account.get("totalWalletBalance"))
        if actual <= 0:
            raise BotError("invalid futures equity")
        equity = min(actual, SIZING_CAP) if SIZING_CAP > 0 else actual

        trade, candle = latest_trade(self.api, symbol)
        if candle != s.close_time + 1:
            raise BotError(
                f"snapback execution candle mismatch: expected={s.close_time + 1} got={candle}"
            )
        notional = equity * profile.notional_mult
        if max_notional > 0 and notional > max_notional:
            raise BotError(
                f"{symbol} desired notional {ds(notional)} exceeds leverage-bracket "
                f"maximum {ds(max_notional)}; set SIZING_EQUITY_CAP_USDT explicitly"
            )
        try:
            q = self.qty(symbol, notional, trade)
        except OrderBelowMinimum as e:
            self.state.data["last_signal"][symbol] = s.close_time
            self.state.save()
            LOG.warning(
                "SKIP_ENTRY %s %s close_time=%s trade=%s sizing_equity=%s "
                "notional_mult=%s reason=%s",
                symbol,
                s.side,
                s.close_time,
                ds(trade),
                ds(equity),
                ds(profile.notional_mult),
                e,
            )
            return

        LOG.warning(
            "OPEN_SNAPBACK %s %s qty=%s trade=%s sizing_equity=%s notional_mult=%s "
            "signal_atr=%s detail=[%s]",
            symbol,
            s.side,
            ds(q),
            ds(trade),
            ds(equity),
            ds(profile.notional_mult),
            ds(s.atr),
            s.detail,
        )
        self.market(symbol, "BUY" if s.side == "long" else "SELL", q, False, "open")
        time.sleep(0.4)
        p = one_position(self.api, symbol)
        if not p.open or p.side != s.side:
            raise BotError("entry reconciliation failed")

        self.state.data.update(
            {
                "version": STATE_VERSION,
                "strategy": STRATEGY_ID,
                "active_symbol": symbol,
                "active_strategy": "snapback",
                "side": s.side,
                "cycle_equity": ds(equity),
                "account_equity_at_start": ds(actual),
                "entry_atr": ds(s.atr),
                "signal_reference": ds(s.reference_close),
                "notional_mult": ds(profile.notional_mult),
                "opened_at_ms": int(time.time() * 1000),
                "signal_close_time": s.close_time,
            }
        )
        self.state.data["last_signal"][symbol] = s.close_time
        self.state.save()
        self.clear_pending()

        tp, sl, cap = self.exit_prices(p)
        LOG.info(
            "ENTRY_STATE strategy=snapback %s candle=%s entry=%s reference=%s atr=%s "
            "tp=%s atr_sl=%s hard_cap=%s",
            symbol,
            candle,
            ds(p.entry),
            ds(s.reference_close),
            ds(s.atr),
            ds(tp),
            ds(sl),
            ds(cap),
        )

    def open_fallback(self, desired: int, leverage: Decimal, close_time: int) -> None:
        if desired == 0 or open_positions(self.api) or self.api.open_orders():
            return
        asset = abs(desired) - 1
        if asset < 0 or asset >= len(SYMBOLS):
            raise BotError(f"invalid fallback desired={desired}")
        symbol = SYMBOLS[asset]
        side = "long" if desired > 0 else "short"
        if leverage <= 0 or leverage > FALLBACK_MAX_LEVERAGE:
            raise BotError(f"invalid fallback leverage={leverage}")
        if self.api.position_mode():
            raise BotError("one-way mode required")
        max_notional = self.api.configure(symbol)
        account = self.api.account()
        actual = D(account.get("totalMarginBalance") or account.get("totalWalletBalance"))
        if actual <= 0:
            raise BotError("invalid futures equity")
        equity = min(actual, SIZING_CAP) if SIZING_CAP > 0 else actual
        trade, candle = latest_trade(self.api, symbol)
        if candle != close_time + 1:
            raise BotError(
                f"fallback execution candle mismatch: expected={close_time + 1} got={candle}"
            )
        notional = equity * leverage
        if max_notional > 0 and notional > max_notional:
            raise BotError(
                f"{symbol} desired notional {ds(notional)} exceeds leverage-bracket "
                f"maximum {ds(max_notional)}; set SIZING_EQUITY_CAP_USDT explicitly"
            )
        try:
            quantity = self.qty(symbol, notional, trade)
        except OrderBelowMinimum as exc:
            LOG.warning(
                "SKIP_FALLBACK_ENTRY %s %s close_time=%s leverage=%s reason=%s",
                symbol, side, close_time, ds(leverage), exc,
            )
            return
        LOG.warning(
            "OPEN_FALLBACK %s %s qty=%s trade=%s sizing_equity=%s leverage=%s",
            symbol, side, ds(quantity), ds(trade), ds(equity), ds(leverage),
        )
        self.market(symbol, "BUY" if side == "long" else "SELL", quantity, False, "fbopen")
        time.sleep(0.4)
        position = one_position(self.api, symbol)
        if not position.open or position.side != side:
            raise BotError("fallback entry reconciliation failed")
        self.state.data.update(
            {
                "version": STATE_VERSION,
                "strategy": STRATEGY_ID,
                "active_symbol": symbol,
                "active_strategy": "fallback",
                "side": side,
                "cycle_equity": ds(equity),
                "account_equity_at_start": ds(actual),
                "entry_atr": None,
                "signal_reference": None,
                "notional_mult": ds(leverage),
                "opened_at_ms": int(time.time() * 1000),
                "signal_close_time": close_time,
            }
        )
        self.state.save()
        self.clear_pending()
        LOG.info(
            "ENTRY_STATE strategy=fallback %s candle=%s entry=%s leverage=%s",
            symbol, candle, ds(position.entry), ds(leverage),
        )

    def close_all(self, p: Position, reason: str, trade: Decimal | None = None) -> None:
        q = self.rules[p.symbol].floor(p.qty)
        LOG.warning(
            "CLOSE_ALL reason=%s %s qty=%s entry=%s trade=%s mark=%s upnl=%s",
            reason,
            p.symbol,
            ds(q),
            ds(p.entry),
            ds(trade) if trade is not None else "n/a",
            ds(p.mark),
            ds(p.upnl),
        )
        self.market(
            p.symbol,
            "SELL" if p.side == "long" else "BUY",
            q,
            True,
            "close",
        )
        time.sleep(0.4)
        after = one_position(self.api, p.symbol)
        self.clear_pending()
        if after.open:
            raise BotError(f"residual position after close: {after.qty}")
        self.state.reset_cycle()

    def hard_stop_price(self, p: Position) -> Decimal:
        profile = SNAPBACK_LIVE_PROFILES[p.symbol]
        equity = D(self.state.data["cycle_equity"])
        # Solve net PnL including one entry taker fee and one expected exit taker fee.
        if p.side == "long":
            return (
                p.entry * (D(1) + TAKER_FEE)
                - equity * profile.hard_stop / p.qty
            ) / (D(1) - TAKER_FEE)
        return (
            p.entry * (D(1) - TAKER_FEE)
            + equity * profile.hard_stop / p.qty
        ) / (D(1) + TAKER_FEE)

    def exit_prices(self, p: Position) -> tuple[Decimal, Decimal, Decimal]:
        profile = SNAPBACK_LIVE_PROFILES[p.symbol]
        a = D(self.state.data["entry_atr"])
        reference = D(self.state.data["signal_reference"])
        if a <= 0:
            raise BotError("invalid entry ATR in state")
        if reference <= 0:
            raise BotError("invalid signal reference in state")
        if p.side == "long":
            tp = reference + profile.tp_atr * a
            atr_sl = reference - profile.sl_atr * a
        else:
            tp = reference - profile.tp_atr * a
            atr_sl = reference + profile.sl_atr * a
        return tp, atr_sl, self.hard_stop_price(p)

    def manage(self) -> None:
        symbol = self.state.data["active_symbol"]
        active_strategy = self.state.data.get("active_strategy")
        live_positions = open_positions(self.api)
        if len(live_positions) != 1 or live_positions[0].symbol != symbol:
            if not live_positions:
                self.state.reset_cycle()
                return
            raise BotError(f"max-one-position invariant broken: {live_positions}")

        p = live_positions[0]
        if active_strategy == "fallback":
            return
        if active_strategy != "snapback":
            raise BotError(f"unknown active_strategy={active_strategy}")
        profile = SNAPBACK_LIVE_PROFILES[symbol]
        trade, _ = latest_trade(self.api, symbol)
        tp, atr_sl, hard_cap = self.exit_prices(p)

        # Use the tighter adverse stop; the hard cap is an account-loss safety ceiling.
        if p.side == "long":
            effective_sl = max(atr_sl, hard_cap)
            if trade <= effective_sl:
                reason = "cycle_loss_cap" if hard_cap >= atr_sl else "atr_stop"
                LOG.warning(
                    "STOP %s side=%s reason=%s trade=%s atr_sl=%s hard_cap=%s",
                    symbol,
                    p.side,
                    reason,
                    ds(trade),
                    ds(atr_sl),
                    ds(hard_cap),
                )
                self.close_all(p, reason, trade)
                return
            if trade >= tp:
                LOG.warning(
                    "TAKE_PROFIT %s side=%s trade=%s target=%s tp_atr=%s",
                    symbol,
                    p.side,
                    ds(trade),
                    ds(tp),
                    ds(profile.tp_atr),
                )
                self.close_all(p, "atr_take_profit", trade)
                return
        else:
            effective_sl = min(atr_sl, hard_cap)
            if trade >= effective_sl:
                reason = "cycle_loss_cap" if hard_cap <= atr_sl else "atr_stop"
                LOG.warning(
                    "STOP %s side=%s reason=%s trade=%s atr_sl=%s hard_cap=%s",
                    symbol,
                    p.side,
                    reason,
                    ds(trade),
                    ds(atr_sl),
                    ds(hard_cap),
                )
                self.close_all(p, reason, trade)
                return
            if trade <= tp:
                LOG.warning(
                    "TAKE_PROFIT %s side=%s trade=%s target=%s tp_atr=%s",
                    symbol,
                    p.side,
                    ds(trade),
                    ds(tp),
                    ds(profile.tp_atr),
                )
                self.close_all(p, "atr_take_profit", trade)
                return

        opened = int(self.state.data["opened_at_ms"])
        held_ms = int(time.time() * 1000) - opened
        if held_ms >= profile.max_hold_minutes * 60 * 1000:
            LOG.warning(
                "TIME_EXIT %s side=%s held_minutes=%.1f max_minutes=%s trade=%s",
                symbol,
                p.side,
                held_ms / 60000,
                profile.max_hold_minutes,
                ds(trade),
            )
            self.close_all(p, "max_hold_time", trade)

    def complete_shadow(self, trade_return: Decimal, reason: str, exit_price: Decimal) -> None:
        shadow = self.state.data["shadow"]
        count = int(shadow.get("completed_count", 0))
        prior = D(shadow.get("ewm")) if shadow.get("ewm") is not None else None
        updated = (
            trade_return
            if prior is None
            else (D(1) - GATE_ALPHA) * prior + GATE_ALPHA * trade_return
        )
        shadow.update(
            {
                "completed_count": count + 1,
                "ewm": ds(updated),
                "position": None,
            }
        )
        self.state.save()
        LOG.warning(
            "SHADOW_CLOSE reason=%s exit=%s return=%s completed=%s ewm=%s "
            "sampled_gate_remains=%s",
            reason,
            ds(exit_price),
            ds(trade_return),
            count + 1,
            ds(updated),
            shadow.get("gate_on"),
        )

    def start_shadow(self, symbol: str, s: Signal) -> bool:
        shadow = self.state.data["shadow"]
        if shadow.get("position"):
            return bool(shadow.get("gate_on"))
        count = int(shadow.get("completed_count", 0))
        current = D(shadow.get("ewm")) if shadow.get("ewm") is not None else None
        gate_on = count >= GATE_SPAN and current is not None and current > GATE_THRESHOLD
        shadow["gate_on"] = gate_on

        rows = self.api.klines(symbol, "1m", 1)
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
        shadow["position"] = {
            "symbol": symbol,
            "side": s.side,
            "entry": ds(entry),
            "qty": ds(qty),
            "wallet": ds(wallet),
            "take_profit": ds(take_profit),
            "stop": ds(stop),
            "entry_open_ms": entry_open_ms,
            "last_processed_open_ms": entry_open_ms - 60_000,
            "last_funding_time_ms": entry_open_ms - 1,
            "funding_checked_through_ms": entry_open_ms - 1,
            "max_exit_ms": entry_open_ms + profile.max_hold_minutes * 60_000,
        }
        self.state.save()
        LOG.warning(
            "SHADOW_OPEN %s %s entry=%s atr=%s tp=%s stop=%s sampled_gate=%s "
            "completed=%s ewm=%s",
            symbol,
            s.side,
            ds(entry),
            ds(s.atr),
            ds(take_profit),
            ds(stop),
            gate_on,
            count,
            ds(current) if current is not None else "n/a",
        )
        return gate_on

    def sync_shadow(self) -> None:
        if time.monotonic() - self.last_shadow_sync < 2:
            return
        self.last_shadow_sync = time.monotonic()
        shadow = self.state.data.get("shadow", {})
        raw = shadow.get("position")
        if not raw:
            return
        now_ms = int(time.time() * 1000)
        closed_end = (now_ms // 60_000) * 60_000
        cursor = int(raw["last_processed_open_ms"]) + 60_000
        if cursor >= closed_end:
            return
        symbol = str(raw["symbol"])
        pages: list[list[Any]] = []
        while cursor < closed_end:
            page = self.api.klines(
                symbol, "1m", 1500, start_time=cursor, end_time=closed_end - 1
            )
            page = [row for row in page if int(row[0]) >= cursor and int(row[6]) < now_ms]
            if not page:
                break
            pages.extend(page)
            newest = max(int(row[0]) for row in page)
            if newest < cursor:
                break
            cursor = newest + 60_000

        if not pages:
            return
        funding_rows = self.api.funding_rates(
            symbol,
            int(raw.get("funding_checked_through_ms", raw["entry_open_ms"] - 1)) + 1,
            int(pages[-1][6]),
        )
        funding_by_minute: dict[int, list[dict[str, Any]]] = {}
        for funding in funding_rows:
            minute = int(funding["fundingTime"]) // 60_000 * 60_000
            funding_by_minute.setdefault(minute, []).append(funding)

        entry = D(raw["entry"])
        qty = D(raw["qty"])
        qty_abs = abs(qty)
        wallet = D(raw["wallet"])
        take_profit = D(raw["take_profit"])
        stop = D(raw["stop"])
        side_sign = D(1) if qty > 0 else D(-1)
        entry_open_ms = int(raw["entry_open_ms"])
        last_funding_time = int(raw.get("last_funding_time_ms", entry_open_ms - 1))

        for row in pages:
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
            if open_equity <= D("0.005") * qty_abs * open_price:
                self.complete_shadow(D("-1"), "liquidation_gap", open_price)
                return
            stop_hit = (qty > 0 and low <= stop) or (qty < 0 and high >= stop)
            target_hit = (qty > 0 and high >= take_profit) or (
                qty < 0 and low <= take_profit
            )
            reason: str | None = None
            exit_price = D(0)
            if stop_hit:
                exit_price = min(stop, open_price) if qty > 0 else max(stop, open_price)
                reason = "stop"
            elif target_hit:
                exit_price = max(take_profit, open_price) if qty > 0 else min(
                    take_profit, open_price
                )
                reason = "take_profit"
            elif open_ms >= int(raw["max_exit_ms"]):
                exit_price = open_price
                reason = "max_hold"

            raw["last_processed_open_ms"] = open_ms
            raw["last_funding_time_ms"] = last_funding_time
            raw["funding_checked_through_ms"] = int(row[6])
            raw["wallet"] = ds(wallet)
            if reason is not None:
                wallet += qty * (exit_price - entry) - qty_abs * exit_price * SHADOW_COST
                self.complete_shadow(wallet - D(1), reason, exit_price)
                return
        self.state.save()

    def apply_fallback(self, snapshot: dict[str, Any], fresh: bool) -> None:
        desired = int(snapshot["desired"])
        fallback = self.state.data["fallback"]
        fallback.update(
            {
                "desired": desired,
                "held_bars": int(snapshot["held_bars"]),
                "score": ds(snapshot["score"]) if snapshot["score"] is not None else None,
                "vol": ds(snapshot["vol"]) if snapshot["vol"] is not None else None,
                "leverage": ds(snapshot["leverage"])
                if snapshot["leverage"] is not None
                else None,
            }
        )
        self.state.save()
        if not LIVE or not fresh:
            return
        if bool(self.state.data["shadow"].get("gate_on")):
            live_positions = open_positions(self.api)
            if live_positions and self.state.data.get("active_strategy") == "fallback":
                self.close_all(live_positions[0], "gate_on_disables_fallback")
            return
        live_positions = open_positions(self.api)
        if live_positions:
            if self.state.data.get("active_strategy") != "fallback":
                return
            position = live_positions[0]
            desired_symbol = SYMBOLS[abs(desired) - 1] if desired else None
            desired_side = "long" if desired > 0 else "short"
            if desired and position.symbol == desired_symbol and position.side == desired_side:
                return
            self.close_all(position, "fallback_switch")
        if desired:
            leverage = snapshot["leverage"]
            if leverage is None:
                return
            self.open_fallback(desired, leverage, int(snapshot["close_time"]))

    def scan(self) -> None:
        if time.monotonic() - self.last_signal_scan < 5:
            return
        self.last_signal_scan = time.monotonic()
        expected_close = latest_completed_15m_close_time()
        if expected_close <= int(self.state.data.get("last_strategy_bar_close", 0)):
            return

        rows_by_symbol = {
            symbol: closed(self.api.klines(symbol, "15m", FALLBACK_FETCH_LIMIT))
            for symbol in SYMBOLS
        }
        snapshot = fallback_snapshot(rows_by_symbol)
        close_time = int(snapshot["close_time"])
        if close_time <= int(self.state.data.get("last_strategy_bar_close", 0)):
            return
        now_ms = int(time.time() * 1000)
        age_ms = max(0, now_ms - close_time)
        fresh_bar = age_ms <= SIGNAL_MAX_AGE_MS
        candidates: list[tuple[str, Signal]] = []
        for symbol in SYMBOLS:
            current = signal_for(self.api, symbol, rows_by_symbol[symbol])
            if current and current.close_time > int(
                self.state.data["last_signal"].get(symbol, 0)
            ):
                candidates.append((symbol, current))
                self.state.data["last_signal"][symbol] = current.close_time

        self.state.data["last_strategy_bar_close"] = close_time
        self.state.save()
        LOG.info(
            "FALLBACK_SIGNAL close_time=%s desired=%s held=%s score=%s vol=%s "
            "leverage=%s gate_on=%s age_ms=%s fresh=%s",
            close_time,
            snapshot["desired"],
            snapshot["held_bars"],
            ds(snapshot["score"]) if snapshot["score"] is not None else "n/a",
            ds(snapshot["vol"]) if snapshot["vol"] is not None else "n/a",
            ds(snapshot["leverage"]) if snapshot["leverage"] is not None else "n/a",
            self.state.data["shadow"].get("gate_on"),
            age_ms,
            fresh_bar,
        )
        for symbol, current in candidates:
            LOG.info(
                "SNAPBACK_SIGNAL %s %s close_time=%s reference=%s atr=%s fresh=%s "
                "detail=[%s]",
                symbol,
                current.side,
                current.close_time,
                ds(current.reference_close),
                ds(current.atr),
                fresh_bar,
                current.detail,
            )

        if candidates and fresh_bar:
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

    def run(self) -> None:
        self.bootstrap()
        profile_summary = "; ".join(
            f"{s}:notional={ds(p.notional_mult)}x,tp={ds(p.tp_atr)}ATR,"
            f"sl={ds(p.sl_atr)}ATR,max_hold={p.max_hold_minutes}m,"
            f"hard_cap={ds(p.hard_stop * D(100))}%"
            for s, p in SNAPBACK_LIVE_PROFILES.items()
        )
        LOG.info(
            "started strategy=%s live=%s symbols=%s priority=%s leverage=%sx cross "
            "max_one_position gate=ewm(%s)>%s fallback=ema(%s,%s) target_vol=%s "
            "max_fallback_leverage=%s fresh_signal=%sms snapback_profiles=[%s]",
            STRATEGY_ID,
            LIVE,
            SYMBOLS,
            ">".join(SYMBOLS),
            LEVERAGE,
            GATE_SPAN,
            ds(GATE_THRESHOLD),
            FALLBACK_FAST,
            FALLBACK_SLOW,
            ds(FALLBACK_TARGET_ANNUAL_VOL),
            ds(FALLBACK_MAX_LEVERAGE),
            SIGNAL_MAX_AGE_MS,
            profile_summary,
        )
        while not STOP:
            if LIVE and self.state.data.get("pending_order") and self.reconcile_pending():
                raise BotError("FILLED pending order with incomplete local transition")
            self.sync_shadow()
            if LIVE and self.state.data.get("active_symbol"):
                self.manage()
            self.scan()
            if RUN_ONCE:
                break
            time.sleep(POLL)


def stop_handler(*_: Any) -> None:
    global STOP
    STOP = True


def main() -> None:
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    with InstanceLock(INSTANCE_LOCK_PATH):
        api = Binance()
        state = State(STATE_PATH)
        rules = load_rules(api)
        Bot(api, state, rules).run()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        LOG.exception("bot stopped: %s", e)
        raise SystemExit(1)
