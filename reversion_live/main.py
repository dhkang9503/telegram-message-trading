"""Binance USD-M BTC/ETH/SOL asymmetric snapback rotation bot.

Strategy:
- 15m completed-candle signals, fixed priority BTC > ETH > SOL, max one account-wide position.
- BTC: short-only Bollinger exhaustion/re-entry.
- ETH: long-only Bollinger exhaustion/re-entry.
- SOL: two-way EMA/ATR (Keltner-style) exhaustion/re-entry.
- No DCA, no rotation, no trailing. Each trade uses fixed ATR TP/SL plus a cycle loss cap
  and a maximum holding time.
"""
from __future__ import annotations

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


# Backtest-selected balanced profile. Exchange leverage remains 98x, while actual account
# notional exposure is limited by notional_mult (BTC 5x / ETH 9x / SOL 3x).
PROFILES = {
    "BTCUSDT": Profile(D("5"), D("8"), D("6"), 48 * 60, D("0.20")),
    "ETHUSDT": Profile(D("9"), D("2"), D("3"), 24 * 60, D("0.20")),
    "SOLUSDT": Profile(D("3"), D("5"), D("4"), 6 * 60, D("0.15")),
}
SYMBOLS = tuple(PROFILES)  # fixed priority: BTC > ETH > SOL

STRATEGY_ID = "asymmetric_snapback_v1"
STATE_VERSION = 2

LEVERAGE = 98
BB_PERIOD = 20
EMA_PERIOD = 20
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
ATR_MEDIAN_LOOKBACK = 100
INDICATOR_FETCH_LIMIT = 500

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
SIGNAL_MAX_AGE_MS = int(float(os.getenv("SIGNAL_MAX_AGE_SECONDS", "60")) * 1000)
BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")
LIVE = os.getenv("LIVE_TRADING", "0") == "1"
SIZING_CAP = D(os.getenv("SIZING_EQUITY_CAP_USDT", "0"))
POLL = float(os.getenv("POLL_SECONDS", "3"))
STATE_PATH = Path(os.getenv("REVERSION_STATE_PATH", str(Path(__file__).with_name("state.json"))))
RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW_MS", "5000"))
TIMEOUT = float(os.getenv("BINANCE_HTTP_TIMEOUT_SECONDS", "10"))

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
        "side": None,
        "cycle_equity": None,
        "account_equity_at_start": None,
        "entry_atr": None,
        "opened_at_ms": None,
        "signal_close_time": None,
        "last_signal": {},
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
            elif version == 1:
                # Do not silently take over a position opened by the old DCA/rotation strategy.
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
        last = dict(self.data.get("last_signal", {}))
        self.data = blank_state()
        self.data["last_signal"] = last
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

    def klines(self, symbol: str, interval: str, limit: int = 120) -> list[list[Any]]:
        return self.req(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )

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

    def configure(self, symbol: str) -> None:
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


def signal_for(api: Binance, symbol: str) -> Signal | None:
    rows = closed(api.klines(symbol, "15m", INDICATOR_FETCH_LIMIT))
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
            return Signal("short", close_time, ca, detail)
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
            return Signal("long", close_time, ca, detail)
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
            return Signal("long", close_time, ca, detail)
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
            return Signal("short", close_time, ca, detail)
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


class Bot:
    def __init__(self, api: Binance, state: State, rules: dict[str, Rules]):
        self.api, self.state, self.rules = api, state, rules
        self.last_signal_scan = 0.0

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
            return

        pos = open_positions(self.api)

        if self.state.legacy:
            if pos:
                raise BotError(
                    "legacy v1 DCA/rotation position/state detected. "
                    "Do not let the new snapback strategy take it over; "
                    "finish/close the legacy position with the old bot first."
                )
            LOG.warning("migrating flat legacy v1 state to %s", STRATEGY_ID)
            self.state.reset_cycle()

        if self.reconcile_pending():
            raise BotError("recovered FILLED pending order; manual state reconciliation required")

        if len(pos) > 1:
            raise BotError("more than one futures position is open")

        if pos:
            p = pos[0]
            required = (
                "active_symbol",
                "side",
                "cycle_equity",
                "entry_atr",
                "opened_at_ms",
            )
            if (
                self.state.data.get("strategy") != STRATEGY_ID
                or p.symbol not in SYMBOLS
                or self.state.data.get("active_symbol") != p.symbol
                or self.state.data.get("side") != p.side
                or any(self.state.data.get(k) is None for k in required)
            ):
                raise BotError(f"unknown/incompatible live position: {p}")
            return

        if self.state.data.get("active_symbol"):
            self.state.reset_cycle()

        if self.api.open_orders():
            raise BotError("existing USD-M open orders block this bot")
        if self.api.position_mode():
            self.api.set_one_way()

    def open_signal(self, symbol: str, s: Signal) -> None:
        if open_positions(self.api) or self.api.open_orders():
            return
        if self.api.position_mode():
            raise BotError("one-way mode required")

        profile = PROFILES[symbol]
        self.api.configure(symbol)
        account = self.api.account()
        actual = D(account.get("totalMarginBalance") or account.get("totalWalletBalance"))
        if actual <= 0:
            raise BotError("invalid futures equity")
        equity = min(actual, SIZING_CAP) if SIZING_CAP > 0 else actual

        trade, candle = latest_trade(self.api, symbol)
        notional = equity * profile.notional_mult
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
            "OPEN %s %s qty=%s trade=%s sizing_equity=%s notional_mult=%s "
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
                "side": s.side,
                "cycle_equity": ds(equity),
                "account_equity_at_start": ds(actual),
                "entry_atr": ds(s.atr),
                "opened_at_ms": int(time.time() * 1000),
                "signal_close_time": s.close_time,
            }
        )
        self.state.data["last_signal"][symbol] = s.close_time
        self.state.save()
        self.clear_pending()

        tp, sl, cap = self.exit_prices(p)
        LOG.info(
            "ENTRY_STATE %s candle=%s entry=%s atr=%s tp=%s atr_sl=%s hard_cap=%s",
            symbol,
            candle,
            ds(p.entry),
            ds(s.atr),
            ds(tp),
            ds(sl),
            ds(cap),
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
        profile = PROFILES[p.symbol]
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
        profile = PROFILES[p.symbol]
        a = D(self.state.data["entry_atr"])
        if a <= 0:
            raise BotError("invalid entry ATR in state")
        if p.side == "long":
            tp = p.entry + profile.tp_atr * a
            atr_sl = p.entry - profile.sl_atr * a
        else:
            tp = p.entry - profile.tp_atr * a
            atr_sl = p.entry + profile.sl_atr * a
        return tp, atr_sl, self.hard_stop_price(p)

    def manage(self) -> None:
        symbol = self.state.data["active_symbol"]
        profile = PROFILES[symbol]
        live_positions = open_positions(self.api)
        if len(live_positions) != 1 or live_positions[0].symbol != symbol:
            if not live_positions:
                self.state.reset_cycle()
                return
            raise BotError(f"max-one-position invariant broken: {live_positions}")

        p = live_positions[0]
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

    def consume_occupied_bar(self) -> None:
        """Do not enter a signal later if it happened while another symbol was occupied."""
        close_time = latest_completed_15m_close_time()
        changed = False
        for symbol in SYMBOLS:
            previous = int(self.state.data["last_signal"].get(symbol, 0))
            if close_time > previous:
                self.state.data["last_signal"][symbol] = close_time
                changed = True
        if changed:
            self.state.save()

    def scan(self) -> None:
        if time.monotonic() - self.last_signal_scan < 15:
            return
        self.last_signal_scan = time.monotonic()
        now_ms = int(time.time() * 1000)
        candidates: list[tuple[str, Signal, int]] = []

        # Evaluate all symbols first; lower-priority simultaneous signals are consumed too.
        for symbol in SYMBOLS:
            s = signal_for(self.api, symbol)
            if not s:
                continue
            if s.close_time <= int(self.state.data["last_signal"].get(symbol, 0)):
                continue
            age_ms = max(0, now_ms - s.close_time)
            candidates.append((symbol, s, age_ms))

        if not candidates:
            return

        for symbol, s, _ in candidates:
            self.state.data["last_signal"][symbol] = s.close_time
        self.state.save()

        fresh = [x for x in candidates if x[2] <= SIGNAL_MAX_AGE_MS]
        for symbol, s, age_ms in candidates:
            LOG.info(
                "SIGNAL %s %s close_time=%s atr=%s age_ms=%s fresh=%s detail=[%s]",
                symbol,
                s.side,
                s.close_time,
                ds(s.atr),
                age_ms,
                age_ms <= SIGNAL_MAX_AGE_MS,
                s.detail,
            )

        if not fresh:
            LOG.info(
                "SKIP_STALE signals=%s max_age_ms=%s",
                ",".join(f"{symbol}:{s.close_time}" for symbol, s, _ in candidates),
                SIGNAL_MAX_AGE_MS,
            )
            return

        selected = fresh[0]
        if len(fresh) > 1:
            LOG.info(
                "SIGNAL_PRIORITY selected=%s consumed=%s",
                selected[0],
                ",".join(x[0] for x in fresh[1:]),
            )

        if LIVE:
            symbol, s, _ = selected
            self.open_signal(symbol, s)

    def run(self) -> None:
        self.bootstrap()
        profile_summary = "; ".join(
            f"{s}:notional={ds(p.notional_mult)}x,tp={ds(p.tp_atr)}ATR,"
            f"sl={ds(p.sl_atr)}ATR,max_hold={p.max_hold_minutes}m,"
            f"hard_cap={ds(p.hard_stop * D(100))}%"
            for s, p in PROFILES.items()
        )
        LOG.info(
            "started strategy=%s live=%s symbols=%s priority=%s leverage=%sx cross "
            "max_one_position no_dca fresh_signal=%sms profiles=[%s]",
            STRATEGY_ID,
            LIVE,
            SYMBOLS,
            ">".join(SYMBOLS),
            LEVERAGE,
            SIGNAL_MAX_AGE_MS,
            profile_summary,
        )
        while not STOP:
            if LIVE and self.state.data.get("pending_order") and self.reconcile_pending():
                raise BotError("FILLED pending order with incomplete local transition")
            if LIVE and self.state.data.get("active_symbol"):
                self.consume_occupied_bar()
                self.manage()
            else:
                self.scan()
            time.sleep(POLL)


def stop_handler(*_: Any) -> None:
    global STOP
    STOP = True


def main() -> None:
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
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
