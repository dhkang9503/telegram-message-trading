"""Binance USD-M BTCUSDT BB+CCI high-frequency reversion bot."""
from __future__ import annotations

import hashlib, hmac, json, logging, os, secrets, signal, statistics, time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

D = lambda x, default="0": Decimal(default) if x is None or x == "" else Decimal(str(x))

# BTC-only high-frequency profile validated on the recovered reference engine.
SYMBOLS = ("BTCUSDT",)
LEVERAGE = 98
BB_PERIOD, BB_STD = 20, D("1.8")
CCI_PERIOD, CCI_EXTREME = 20, D("210")
ATR_PERIOD, ATR_FETCH_LIMIT = 14, 500
INITIAL_MARGIN = D("0.0125")
ADD_MARGINS = (D("0.0125"), D("0.01875"), D("0.025"))
ADD_ATRS = (D("1.4"), D("3.0"), D("4.6"))
KEEP_RATIO = D("0.50")
TAKER_FEE = D("0.0004")
BE_PRICE_RATIO = D(2) * TAKER_FEE / (D(1) - TAKER_FEE)
TP_ACTIVATION_PRICE_RATIO = D("0.50") / D(LEVERAGE)
TRAIL_ROI_GIVEBACK = D("0.05")
TRAIL_PRICE_RATIO = TRAIL_ROI_GIVEBACK / D(LEVERAGE)
HARD_STOP = D("0.08")
STRUCTURE_LOOKBACK = 96  # 96 x 15m = 24h
ATR_MEDIAN_LOOKBACK, ATR_EXPANSION = 100, D("1.8")
BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")
LIVE = os.getenv("LIVE_TRADING", "0") == "1"
SIZING_CAP = D(os.getenv("SIZING_EQUITY_CAP_USDT", "0"))
POLL = float(os.getenv("POLL_SECONDS", "3"))
STATE_PATH = Path(os.getenv("REVERSION_STATE_PATH", str(Path(__file__).with_name("state.json"))))
RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW_MS", "5000"))
TIMEOUT = float(os.getenv("BINANCE_HTTP_TIMEOUT_SECONDS", "10"))

logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("reversion_live")
STOP = False


class BotError(RuntimeError): pass


class OrderBelowMinimum(BotError): pass


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
        "version": 1, "active_symbol": None, "side": None,
        "cycle_equity": None, "cycle_cash": None, "account_equity_at_start": None,
        "round_anchor": None, "round_atr": None, "add_stage": 0,
        "had_add": False, "rotations": 0, "last_add_candle": None,
        "last_rotation_candle": None, "trail_active": False, "trail_extreme": None,
        "trail_activation_candle": None, "last_signal": {}, "pending_order": None,
        "updated_at": int(time.time()),
    }


class State:
    def __init__(self, path: Path):
        self.path = path; self.data = blank_state()
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("version") != 1: raise BotError("unsupported state version")
            self.data.update(loaded)
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = int(time.time()); save_json(self.path, self.data)

    def reset_cycle(self) -> None:
        last = dict(self.data.get("last_signal", {})); self.data = blank_state()
        self.data["last_signal"] = last; self.save()


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
    def open(self) -> bool: return self.amount != 0
    @property
    def side(self) -> str: return "long" if self.amount > 0 else "short" if self.amount < 0 else "flat"
    @property
    def qty(self) -> Decimal: return abs(self.amount)


class Binance:
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", ""); self.secret = os.getenv("BINANCE_API_SECRET", "")
        self.offset = 0; self.last_sync = 0.0
        if LIVE and (not self.key or not self.secret):
            raise BotError("LIVE_TRADING=1 requires BINANCE_API_KEY and BINANCE_API_SECRET")

    def req(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        p = dict(params or {})
        if signed:
            if not self.key or not self.secret: raise BotError("signed call requires Binance credentials")
            self.sync_time(); p.update({"timestamp": int(time.time()*1000)+self.offset, "recvWindow": RECV_WINDOW})
        clean = {k: (ds(v) if isinstance(v, Decimal) else str(v).lower() if isinstance(v, bool) else v) for k,v in p.items()}
        q = urlencode(clean)
        if signed:
            q += ("&" if q else "") + "signature=" + hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = BASE_URL + path + (("?" + q) if q else "")
        headers = {"Accept":"application/json", "User-Agent":"reversion-live/1.0"}
        if self.key: headers["X-MBX-APIKEY"] = self.key
        try:
            with urlopen(Request(url, method=method, headers=headers), timeout=TIMEOUT) as r:
                body = r.read().decode(); return json.loads(body) if body else {}
        except HTTPError as e:
            raw = e.read().decode(errors="replace")
            try: payload = json.loads(raw)
            except json.JSONDecodeError: payload = {"msg": raw}
            raise BinanceError(e.code, int(payload["code"]) if "code" in payload else None, str(payload.get("msg",raw))) from e
        except URLError as e: raise BotError(f"Binance network error: {e}") from e

    def sync_time(self) -> None:
        if time.monotonic()-self.last_sync < 300: return
        server = self.req("GET", "/fapi/v1/time")["serverTime"]
        self.offset = int(server)-int(time.time()*1000); self.last_sync = time.monotonic()

    def klines(self, symbol: str, interval: str, limit: int = 120) -> list[list[Any]]:
        return self.req("GET", "/fapi/v1/klines", {"symbol":symbol,"interval":interval,"limit":limit})
    def account(self) -> dict[str,Any]: return self.req("GET","/fapi/v3/account",signed=True)
    def positions(self, symbol: str | None = None) -> list[dict[str,Any]]:
        x = self.req("GET","/fapi/v3/positionRisk",{"symbol":symbol} if symbol else {},signed=True)
        return x if isinstance(x,list) else [x]
    def open_orders(self) -> list[dict[str,Any]]: return self.req("GET","/fapi/v1/openOrders",signed=True)
    def position_mode(self) -> bool:
        x = self.req("GET","/fapi/v1/positionSide/dual",signed=True)["dualSidePosition"]
        return x if isinstance(x,bool) else str(x).lower()=="true"
    def set_one_way(self) -> None: self.req("POST","/fapi/v1/positionSide/dual",{"dualSidePosition":"false"},True)
    def configure(self, symbol: str) -> None:
        try: self.req("POST","/fapi/v1/marginType",{"symbol":symbol,"marginType":"CROSSED"},True)
        except BinanceError as e:
            if e.code != -4046: raise
        x = self.req("POST","/fapi/v1/leverage",{"symbol":symbol,"leverage":LEVERAGE},True)
        if int(x.get("leverage",0)) != LEVERAGE: raise BotError(f"{symbol} did not accept {LEVERAGE}x")
    def order(self, symbol: str, side: str, qty: Decimal, reduce: bool, cid: str) -> dict[str,Any]:
        p: dict[str,Any] = {"symbol":symbol,"side":side,"type":"MARKET","quantity":qty,
                           "newClientOrderId":cid,"newOrderRespType":"RESULT"}
        if reduce: p["reduceOnly"] = "true"
        return self.req("POST","/fapi/v1/order",p,True)
    def get_order(self, symbol: str, cid: str) -> dict[str,Any]:
        return self.req("GET","/fapi/v1/order",{"symbol":symbol,"origClientOrderId":cid},True)


def closed(rows: list[list[Any]]) -> list[list[Any]]:
    now = int(time.time()*1000); return [r for r in rows if int(r[6]) < now]


def atr_series(rows: list[list[Any]]) -> list[Decimal | None]:
    """Match pandas ewm(alpha=1/14, adjust=False, min_periods=14)."""
    if not rows: return []
    tr: list[Decimal] = []
    for i,r in enumerate(rows):
        h,l = D(r[2]),D(r[3])
        tr.append(h-l if i==0 else max(h-l,abs(h-D(rows[i-1][4])),abs(l-D(rows[i-1][4]))))
    out: list[Decimal|None] = [None]*len(tr); a = tr[0]
    for i,x in enumerate(tr):
        if i: a = (a*D(ATR_PERIOD-1)+x)/D(ATR_PERIOD)
        if i >= ATR_PERIOD-1: out[i] = a
    return out


def bands(closes: list[Decimal], i: int) -> tuple[Decimal,Decimal]:
    w = closes[i-BB_PERIOD+1:i+1]; m = sum(w,D("0"))/D(BB_PERIOD)
    sd = (sum((x-m)**2 for x in w)/D(BB_PERIOD)).sqrt()
    return m+BB_STD*sd, m-BB_STD*sd


def cci(rows: list[list[Any]], i: int) -> Decimal:
    w = rows[i-CCI_PERIOD+1:i+1]; tp = [(D(r[2])+D(r[3])+D(r[4]))/D(3) for r in w]
    m = sum(tp,D("0"))/D(CCI_PERIOD); dev = sum(abs(x-m) for x in tp)/D(CCI_PERIOD)
    return D("0") if dev==0 else (tp[-1]-m)/(D("0.015")*dev)


def signal_for(api: Binance, symbol: str) -> tuple[str,int,Decimal] | None:
    rows = closed(api.klines(symbol,"15m",ATR_FETCH_LIMIT))
    if len(rows) < max(BB_PERIOD,CCI_PERIOD,ATR_PERIOD)+2: return None
    closes = [D(r[4]) for r in rows]; p,c = len(rows)-2,len(rows)-1
    pu,pl = bands(closes,p); cu,cl = bands(closes,c); pc,cc = cci(rows,p),cci(rows,c); a = atr_series(rows)[c]
    if a is None: return None
    if closes[p] < pl and pc < -CCI_EXTREME and closes[c] > cl and cc > pc: return "long",int(rows[c][6]),a
    if closes[p] > pu and pc > CCI_EXTREME and closes[c] < cu and cc < pc: return "short",int(rows[c][6]),a
    return None


def current_atr(api: Binance, symbol: str) -> Decimal:
    a = atr_series(closed(api.klines(symbol,"15m",ATR_FETCH_LIMIT)))[-1]
    if a is None or a <= 0: raise BotError(f"ATR unavailable for {symbol}")
    return a


def latest_trade(api: Binance, symbol: str) -> tuple[Decimal,int]:
    rows = api.klines(symbol,"5m",2)
    if not rows: raise BotError(f"trade price unavailable for {symbol}")
    return D(rows[-1][4]), int(rows[-1][0])


def parse_pos(x: dict[str,Any]) -> Position:
    return Position(str(x["symbol"]),D(x.get("positionAmt")),D(x.get("entryPrice")),
                    D(x.get("breakEvenPrice") or x.get("entryPrice")),D(x.get("markPrice")),
                    D(x.get("unRealizedProfit")),D(x.get("liquidationPrice")))


def open_positions(api: Binance) -> list[Position]: return [p for p in map(parse_pos,api.positions()) if p.open]


def one_position(api: Binance, symbol: str) -> Position:
    rows = [parse_pos(x) for x in api.positions(symbol) if str(x.get("symbol"))==symbol]
    opens = [p for p in rows if p.open]
    if len(opens)>1: raise BotError(f"multiple live rows for {symbol}")
    return opens[0] if opens else rows[0] if rows else Position(symbol,D(0),D(0),D(0),D(0),D(0),D(0))


def load_rules(api: Binance) -> dict[str,Rules]:
    info = api.req("GET","/fapi/v1/exchangeInfo"); out: dict[str,Rules] = {}
    for s in info.get("symbols",[]):
        if s.get("symbol") not in SYMBOLS: continue
        fs = {f["filterType"]:f for f in s.get("filters",[])}; lot = fs.get("MARKET_LOT_SIZE") or fs["LOT_SIZE"]
        if D(lot.get("stepSize")) <= 0: lot = fs["LOT_SIZE"]
        nf = fs.get("MIN_NOTIONAL") or fs.get("NOTIONAL") or {}
        out[s["symbol"]] = Rules(D(lot["stepSize"]),D(lot["minQty"]),D(nf.get("notional") or nf.get("minNotional")))
    if set(out) != set(SYMBOLS): raise BotError("missing symbol filters")
    return out


def structure_break(api: Binance, p: Position) -> bool:
    rows = closed(api.klines(p.symbol,"15m",ATR_FETCH_LIMIT))
    if len(rows) < max(STRUCTURE_LOOKBACK+1,ATR_MEDIAN_LOOKBACK+ATR_PERIOD): return False
    ats = atr_series(rows); cur = ats[-1]; hist = [x for x in ats[-ATR_MEDIAN_LOOKBACK:] if x is not None]
    if cur is None or len(hist) < ATR_MEDIAN_LOOKBACK: return False
    med = D(statistics.median(hist)); prev = rows[-(STRUCTURE_LOOKBACK+1):-1]
    close = D(rows[-1][4]); low = min(D(r[3]) for r in prev); high = max(D(r[2]) for r in prev)
    broken = close < low if p.side=="long" else close > high
    return broken and med > 0 and cur >= ATR_EXPANSION*med


class Bot:
    def __init__(self, api: Binance, state: State, rules: dict[str,Rules]):
        self.api,self.state,self.rules = api,state,rules
        self.last_signal_scan = 0.0; self.last_structure_scan = 0.0; self.structure_cached = False

    def qty(self, symbol: str, notional: Decimal, price: Decimal) -> Decimal:
        r = self.rules[symbol]; q = r.floor(notional/price)
        if q < r.min_qty or q <= 0 or (r.min_notional > 0 and q*price < r.min_notional):
            raise OrderBelowMinimum(f"{symbol} order below exchange minimum; bot will not auto-upsize")
        return q

    def reconcile_pending(self) -> bool:
        x = self.state.data.get("pending_order")
        if not x: return False
        try: order = self.api.get_order(x["symbol"],x["cid"])
        except BinanceError as e:
            if e.code == -2013:
                self.state.data["pending_order"] = None; self.state.save(); return False
            raise BotError(f"cannot reconcile pending order: {e}") from e
        if order.get("status") != "FILLED": raise BotError(f"pending order status={order.get('status')}; manual review")
        return True

    def market(self, symbol: str, side: str, qty: Decimal, reduce: bool, action: str) -> None:
        if self.state.data.get("pending_order") and self.reconcile_pending():
            raise BotError("previous market order filled but local transition is incomplete")
        cid = (f"rv{action}{int(time.time()*1000)%10**10}{secrets.token_hex(3)}")[:36]
        self.state.data["pending_order"] = {"symbol":symbol,"cid":cid,"action":action,"qty":ds(qty)}; self.state.save()
        try: order = self.api.order(symbol,side,qty,reduce,cid)
        except (BinanceError,BotError) as e:
            if self.reconcile_pending(): return
            raise BotError(f"market order not confirmed: {e}") from e
        if order.get("status") not in (None,"FILLED"): raise BotError(f"unexpected market status={order.get('status')}")

    def clear_pending(self) -> None: self.state.data["pending_order"] = None; self.state.save()

    def bootstrap(self) -> None:
        if not LIVE: LOG.warning("LIVE_TRADING=0: signal-only mode"); return
        if self.reconcile_pending(): raise BotError("recovered FILLED pending order; manual state reconciliation required")
        pos = open_positions(self.api)
        if len(pos)>1: raise BotError("more than one futures position is open")
        if pos:
            p = pos[0]
            if p.symbol not in SYMBOLS or self.state.data.get("active_symbol") != p.symbol or self.state.data.get("side") != p.side:
                raise BotError(f"unknown live position: {p}")
            if any(self.state.data.get(k) is None for k in ("cycle_equity","cycle_cash","round_anchor","round_atr")):
                raise BotError("live position has incompatible state; manual reconciliation required")
            return
        if self.state.data.get("active_symbol"): self.state.reset_cycle()
        if self.api.open_orders(): raise BotError("existing USD-M open orders block this bot")
        if self.api.position_mode(): self.api.set_one_way()

    def open_signal(self, symbol: str, side: str, close_time: int, atr: Decimal) -> None:
        if open_positions(self.api) or self.api.open_orders(): return
        if self.api.position_mode(): raise BotError("one-way mode required")
        self.api.configure(symbol); account = self.api.account()
        actual = D(account.get("totalMarginBalance") or account.get("totalWalletBalance"))
        if actual <= 0: raise BotError("invalid futures equity")
        equity = min(actual,SIZING_CAP) if SIZING_CAP > 0 else actual
        trade,candle = latest_trade(self.api,symbol)
        try:
            q = self.qty(symbol,equity*INITIAL_MARGIN*D(LEVERAGE),trade)
        except OrderBelowMinimum as e:
            self.state.data["last_signal"][symbol] = close_time
            self.state.save()
            LOG.warning("SKIP_ENTRY %s %s close_time=%s trade=%s sizing_equity=%s reason=%s",
                        symbol,side,close_time,ds(trade),ds(equity),e)
            return
        LOG.warning("OPEN %s %s qty=%s trade=%s sizing_equity=%s",symbol,side,ds(q),ds(trade),ds(equity))
        self.market(symbol,"BUY" if side=="long" else "SELL",q,False,"open"); time.sleep(.4); p = one_position(self.api,symbol)
        if not p.open or p.side != side: raise BotError("entry reconciliation failed")
        cycle_cash = equity - p.qty*p.entry*TAKER_FEE
        self.state.data.update({"active_symbol":symbol,"side":side,"cycle_equity":ds(equity),"cycle_cash":ds(cycle_cash),
                                "account_equity_at_start":ds(actual),"round_anchor":ds(p.entry),"round_atr":ds(atr),
                                "add_stage":0,"had_add":False,"rotations":0,"last_add_candle":None,"last_rotation_candle":None,
                                "trail_active":False,"trail_extreme":None,"trail_activation_candle":None})
        self.state.data["last_signal"][symbol] = close_time; self.state.save(); self.clear_pending()
        LOG.info("ENTRY_STATE %s candle=%s cycle_cash=%s",symbol,candle,ds(cycle_cash))

    def close_all(self, p: Position, reason: str, trade: Decimal | None = None) -> None:
        q = self.rules[p.symbol].floor(p.qty)
        LOG.warning("CLOSE_ALL reason=%s %s qty=%s entry=%s trade=%s mark=%s upnl=%s",reason,p.symbol,ds(q),ds(p.entry),
                    ds(trade) if trade is not None else "n/a",ds(p.mark),ds(p.upnl))
        self.market(p.symbol,"SELL" if p.side=="long" else "BUY",q,True,"close"); time.sleep(.4)
        after = one_position(self.api,p.symbol); self.clear_pending()
        if after.open: raise BotError(f"residual position after close: {after.qty}")
        self.state.reset_cycle(); self.structure_cached = False

    def hard_stop_price(self, p: Position) -> Decimal:
        equity = D(self.state.data["cycle_equity"]); cash = D(self.state.data["cycle_cash"])
        target = equity*(D(1)-HARD_STOP); delta = (target-cash)/p.qty
        return p.entry+delta if p.side=="long" else p.entry-delta

    def manage_trail(self, p: Position, trade: Decimal, candle: int) -> bool:
        extreme = D(self.state.data.get("trail_extreme"))
        activation_candle = int(self.state.data["trail_activation_candle"])
        distance = p.entry * TRAIL_PRICE_RATIO

        # Match the backtest ordering: evaluate the previously established stop first.
        stop = extreme-distance if p.side=="long" else extreme+distance
        hit = trade <= stop if p.side=="long" else trade >= stop
        if candle != activation_candle and hit:
            LOG.warning("TRAIL_EXIT %s side=%s trade=%s extreme=%s stop=%s giveback_roi=%s",
                        p.symbol,p.side,ds(trade),ds(extreme),ds(stop),ds(TRAIL_ROI_GIVEBACK))
            self.close_all(p,"tp_trail_5pct_roi_giveback",trade)
            return True

        # The activation candle may establish a better extreme, but cannot trail-exit.
        better = trade > extreme if p.side=="long" else trade < extreme
        if better:
            self.state.data["trail_extreme"] = ds(trade); self.state.save()
        return True

    def manage(self) -> None:
        symbol = self.state.data["active_symbol"]; live_positions = open_positions(self.api)
        if len(live_positions)!=1 or live_positions[0].symbol != symbol:
            if not live_positions: self.state.reset_cycle(); return
            raise BotError(f"max-one-position invariant broken: {live_positions}")
        p = live_positions[0]; equity = D(self.state.data["cycle_equity"]); trade,candle = latest_trade(self.api,symbol)

        # Validated order: completed-15m structure -> hard stop -> ADD(s) -> rotation -> TP/trail.
        if time.monotonic()-self.last_structure_scan >= 15:
            self.structure_cached = structure_break(self.api,p); self.last_structure_scan = time.monotonic()
        if self.structure_cached: self.close_all(p,"15m_structure_break_atr_expansion",trade); return

        risk = self.hard_stop_price(p)
        if (trade <= risk if p.side=="long" else trade >= risk):
            LOG.warning("HARD_STOP %s trade=%s risk_price=%s cycle_cash=%s",symbol,ds(trade),ds(risk),self.state.data["cycle_cash"])
            self.close_all(p,"cycle_-8pct",trade); return

        # Once +50% ROI activates the runner, freeze ADD/rotation and manage only the trail.
        if self.state.data.get("trail_active"):
            self.manage_trail(p,trade,candle); return

        last_rotation = self.state.data.get("last_rotation_candle")
        can_add = last_rotation is None or int(last_rotation) != candle
        did_add = False
        if can_add:
            while int(self.state.data["add_stage"]) < 3:
                stage = int(self.state.data["add_stage"]); anchor = D(self.state.data["round_anchor"]); a = D(self.state.data["round_atr"])
                trigger = anchor-ADD_ATRS[stage]*a if p.side=="long" else anchor+ADD_ATRS[stage]*a
                if not (trade <= trigger if p.side=="long" else trade >= trigger): break
                before = p; q = self.qty(symbol,equity*ADD_MARGINS[stage]*D(LEVERAGE),trade)
                LOG.warning("ADD%d %s qty=%s trigger=%s trade=%s",stage+1,symbol,ds(q),ds(trigger),ds(trade))
                self.market(symbol,"BUY" if p.side=="long" else "SELL",q,False,f"add{stage+1}"); time.sleep(.4); p = one_position(self.api,symbol)
                if not p.open or p.side != before.side or p.qty <= before.qty: raise BotError("add reconciliation failed")
                added_qty = p.qty-before.qty; fill = (p.entry*p.qty-before.entry*before.qty)/added_qty
                cash = D(self.state.data["cycle_cash"]) - added_qty*fill*TAKER_FEE
                self.state.data.update({"cycle_cash":ds(cash),"add_stage":stage+1,"had_add":True,"last_add_candle":candle})
                self.state.save(); self.clear_pending(); did_add = True; trade,candle = latest_trade(self.api,symbol)

        # Recovery/TP activation is forbidden on a 5m candle that added.
        last_add = self.state.data.get("last_add_candle")
        if did_add or (last_add is not None and int(last_add)==candle): return

        if self.state.data["had_add"]:
            before = p; rotation = p.entry*(D(1)+BE_PRICE_RATIO) if p.side=="long" else p.entry*(D(1)-BE_PRICE_RATIO)
            if (trade >= rotation if p.side=="long" else trade <= rotation):
                q = self.rules[symbol].floor(p.qty*(D(1)-KEEP_RATIO))
                if q <= 0: raise BotError("rotation quantity rounds to zero")
                LOG.warning("ROTATE %s close=%s total=%s target=%s trade=%s keep=%s",symbol,ds(q),ds(p.qty),ds(rotation),ds(trade),ds(KEEP_RATIO))
                self.market(symbol,"SELL" if p.side=="long" else "BUY",q,True,"rotate"); time.sleep(.4); p = one_position(self.api,symbol)
                if not p.open: self.clear_pending(); self.state.reset_cycle(); return
                sign = D(1) if before.side=="long" else D(-1); cash = D(self.state.data["cycle_cash"])
                cash += sign*q*(rotation-before.entry) - q*rotation*TAKER_FEE
                self.state.data.update({"cycle_cash":ds(cash),"round_anchor":ds(before.entry),"round_atr":ds(current_atr(self.api,symbol)),
                                        "add_stage":0,"had_add":False,"rotations":self.state.data["rotations"]+1,"last_rotation_candle":candle})
                self.state.save(); self.clear_pending(); trade,candle = latest_trade(self.api,symbol)

        # +50% ROI no longer closes immediately; it activates a 5%p ROI giveback trail.
        p = one_position(self.api,symbol)
        activation = p.entry*(D(1)+TP_ACTIVATION_PRICE_RATIO) if p.side=="long" else p.entry*(D(1)-TP_ACTIVATION_PRICE_RATIO)
        if (trade >= activation if p.side=="long" else trade <= activation):
            self.state.data.update({"trail_active":True,"trail_extreme":ds(trade),"trail_activation_candle":candle})
            self.state.save()
            LOG.warning("TRAIL_ACTIVATE %s side=%s entry=%s trade=%s activation=%s giveback_roi=%s candle=%s",
                        symbol,p.side,ds(p.entry),ds(trade),ds(activation),ds(TRAIL_ROI_GIVEBACK),candle)

    def scan(self) -> None:
        if time.monotonic()-self.last_signal_scan < 15: return
        self.last_signal_scan = time.monotonic()
        if LIVE and open_positions(self.api): return
        for symbol in SYMBOLS:
            s = signal_for(self.api,symbol)
            if not s: continue
            side,close_time,a = s
            if close_time <= int(self.state.data["last_signal"].get(symbol,0)): continue
            LOG.info("SIGNAL %s %s close_time=%s atr=%s",symbol,side,close_time,ds(a))
            if LIVE: self.open_signal(symbol,side,close_time,a)
            else: self.state.data["last_signal"][symbol] = close_time; self.state.save()
            return

    def run(self) -> None:
        self.bootstrap()
        LOG.info("started live=%s symbols=%s 98x cross highfreq initial=1.25%% adds=1.25/1.875/2.5%% keep=50%% BB=1.8 CCI=210 add_atr=1.4/3.0/4.6 TP=activate50%% trail_giveback=5%%p structure=15m/96 atr_median=100 atr_expansion=1.8",LIVE,SYMBOLS)
        while not STOP:
            if LIVE and self.state.data.get("pending_order") and self.reconcile_pending():
                raise BotError("FILLED pending order with incomplete local transition")
            if LIVE and self.state.data.get("active_symbol"): self.manage()
            else: self.scan()
            time.sleep(POLL)


def stop_handler(*_: Any) -> None:
    global STOP; STOP = True


def main() -> None:
    signal.signal(signal.SIGINT,stop_handler); signal.signal(signal.SIGTERM,stop_handler)
    api = Binance(); state = State(STATE_PATH); rules = load_rules(api); Bot(api,state,rules).run()


if __name__ == "__main__":
    try: main()
    except Exception as e:
        LOG.exception("bot stopped: %s",e); raise SystemExit(1)
