"""Market entry and exchange-hosted full-position exits, independent of plan lifetime.

The account must be exclusive to this bot. Never run another trader with these
credentials. An ambiguous entry is NEVER resubmitted, even across restarts.
"""
from __future__ import annotations

import hashlib
import os
import time
from decimal import Decimal, ROUND_DOWN

try:
    from .binance_futures import Binance, SYMBOL
    from .execution_store import Store
except ImportError:  # Direct main.py deployment.
    from binance_futures import Binance, SYMBOL
    from execution_store import Store

D = Decimal
DONE = {"CLOSED_TP", "CLOSED_SL", "CLOSED_OTHER", "EMERGENCY_CLOSED", "NO_FILL", "DRY_RUN"}
ORDER_DONE = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
ALGO_DONE = {"FINISHED", "CANCELED", "EXPIRED", "REJECTED"}


def decimal(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite execution value")
    return result


def floor_step(value, step):
    if step <= 0:
        raise ValueError("Invalid quantity step")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def unit_risk(entry, stop, fee, slip, sign):
    return sign * (entry - stop) + (entry + stop) * fee + stop * slip


def size_order(s, tolerance, bid, ask, mark, equity, available, fee, rules, budget, slip):
    if any(not x.is_finite() for x in (bid, ask, mark, equity, available, fee, budget, slip)):
        raise ValueError("Nonfinite execution inputs")
    sign = D(1 if s.side == "long" else -1)
    entry = ask if sign > 0 else bid
    stop, tp = decimal(s.stop), decimal(s.targets[0])
    if not 0 < bid <= ask or mark <= 0 or equity <= 0 or available <= 0:
        raise ValueError("Invalid market or balance")
    if not 0 <= fee <= D("0.01") or not 0 < slip <= D("0.01"):
        raise ValueError("Invalid fee or slippage")
    if abs(entry - decimal(s.entry)) > decimal(tolerance):
        raise ValueError("Quote outside entry tolerance")
    if sign * (mark - stop) <= 0 or sign * (tp - mark) <= 0:
        raise ValueError("Mark price already crossed TP/SL")
    tick = decimal(rules["PRICE_FILTER"]["tickSize"])
    if tick <= 0 or any(x % tick for x in (stop, tp)):
        raise ValueError("TP/SL does not match exchange tick")
    worst = entry * (1 + sign * slip)
    loss = unit_risk(worst, stop, fee, slip, sign)
    profit = sign * (tp - worst) - (worst + tp) * fee - tp * slip
    if sign * (worst - stop) <= 0 or loss <= 0 or profit / loss < D("1.5"):
        raise ValueError("Net reward/risk below 1.5")
    lot = rules["LOT_SIZE"]
    market = rules["MARKET_LOT_SIZE"]
    step = max(decimal(lot["stepSize"]), decimal(market["stepSize"]))
    budget = min(budget, equity * D("0.01"))
    qty = floor_step(min(budget / loss, available / (worst / 3 + worst * fee),
                         decimal(lot["maxQty"]), decimal(market["maxQty"])), step)
    if qty < max(decimal(lot["minQty"]), decimal(market["minQty"])):
        raise ValueError("Risk budget below minimum quantity")
    if any(qty % decimal(x["stepSize"]) for x in (lot, market) if decimal(x["stepSize"]) > 0):
        raise ValueError("Incompatible lot steps")
    if qty * min(bid, mark) < decimal(rules["MIN_NOTIONAL"]["notional"]):
        raise ValueError("Below minimum notional")
    return dict(qty=str(qty), stop=str(stop), tp=str(tp), fee=str(fee), slip=str(slip),
                budget=str(budget), entry_limit=str(worst), sign=int(sign),
                estimate=str(qty * loss), expected_price=str(entry))


class Executor:
    def __init__(self, store, exchange, mode="live", slip=D("0.0005"), clock=time.time):
        if mode not in {"live", "dry-run"}:
            raise ValueError("Invalid execution mode")
        self.store, self.api, self.mode, self.slip, self.clock = store, exchange, mode, slip, clock

    @classmethod
    def from_env(cls, directory):
        mode = os.getenv("SCENARIO_EXECUTION_MODE", "off")
        if mode == "off":
            return None
        if mode not in {"live", "dry-run"}:
            raise ValueError("SCENARIO_EXECUTION_MODE must be off, dry-run or live")
        key, secret = os.getenv("SCENARIO_BINANCE_API_KEY", ""), os.getenv("SCENARIO_BINANCE_API_SECRET", "")
        if mode == "live" and (not key or not secret):
            raise ValueError("Live mode requires dedicated scenario credentials")
        slip = decimal(os.getenv("SCENARIO_SLIPPAGE_BPS", "5")) / 10000
        if not 0 < slip <= D("0.01"):
            raise ValueError("Slippage must be in (0, 100] bps")
        store = Store(directory / f"execution-{mode}.sqlite3")
        return cls(store, Binance(key, secret), mode, slip)

    def active(self):
        return [(k, t) for k, t in self.store.state["trades"].items() if t["phase"] not in DONE]

    def notice(self, key, kind, reason):
        self.store.notify(key, kind, reason)

    def gate(self, monitor, event, now, market_ok, paused):
        plan = monitor.plan
        st = monitor.state["scenarios"][event["scenario"]]
        review = (plan.start // 14_400_000 + 1) * 14_400_000
        if now >= review or monitor.state["review_at"]:
            raise ValueError("REVIEW_DUE: new plan required")
        if not market_ok or paused or st["phase"] != "READY":
            raise ValueError("Market, pause or scenario gate blocked")
        if not plan.start <= now < min(plan.end, st["armed_at"] + plan.ttl):
            raise ValueError("Plan or signal expired")
        if not 0 <= now - event["at"] <= 30_000:
            raise ValueError("Entry event older than 30 seconds")
        if plan.raw["plan_id"] in self.store.state["trades"] or self.active():
            raise ValueError("Plan consumed or account reconciliation pending")

    def handle(self, monitor, event, market_ok=True, paused=False):
        if event["kind"] != "ENTRY_READY":
            return
        # Event IDs belong to a plan digest; one trade budget belongs to plan_id.
        seen = monitor.plan.digest + ":" + event["id"]
        if seen in self.store.state["seen"]:
            return
        self.store.state["seen"].append(seen)
        self.store.save()
        key = monitor.plan.raw["plan_id"]
        try:
            now = int(self.clock() * 1000)
            self.gate(monitor, event, now, market_ok, paused)
            s = next(s for s in monitor.plan.scenarios if s.key == event["scenario"])
            rules = self.api.rules()
            if self.mode == "live":
                if any(decimal(p["positionAmt"]) for p in self.api.positions()):
                    raise ValueError("Existing account position: not adopted")
                if self.api.open_orders() or self.api.open_algos():
                    raise ValueError("Existing account orders: not adopted")
                self.api.configure()
                equity, available = self.api.balance()
                fee = self.api.fee()
                day = (now + 32_400_000) // 86_400_000
                start = day * 86_400_000 - 32_400_000
                loss = self.api.daily_losses(start, now)
                days = self.store.state["days"]
                baseline = decimal(days.setdefault(str(day), str(equity)))
                self.store.save()
                budget = min(equity * D("0.01"), min(baseline, equity) * D("0.02") - loss)
            else:
                equity = available = D("200")
                fee, budget = D("0.0005"), D("2")
            bid, ask, mark = self.api.quote()
            sizing = size_order(s, monitor.plan.tolerance, bid, ask, mark, equity,
                                available, fee, rules, budget, self.slip)
            now = int(self.clock() * 1000)
            self.gate(monitor, event, now, market_ok, paused)
            prefix = "sc" + hashlib.sha256((key + ":" + event["id"]).encode()).hexdigest()[:24]
            trade = dict(sizing, phase="ENTRY_PENDING", created=now, scenario=s.key,
                         entry_id=prefix + "e", sl_id=prefix + "s", tp_id=prefix + "t",
                         attempted=[], emergency_ids=[], tolerance=str(monitor.plan.tolerance),
                         planned_entry=str(s.entry))
            self.store.state["trades"][key] = trade
            if self.mode == "dry-run":
                trade["phase"] = "DRY_RUN"
                self.notice(key, "DRY_RUN", f"주문 없음: {s.side} {sizing['qty']} BTC, TP {sizing['tp']}, SL {sizing['stop']}")
                return
            # Persist intent BEFORE sending; an uncertain entry is never sent twice.
            self.store.save()
            try:
                self.api.market(trade["entry_id"], "BUY" if s.side == "long" else "SELL", sizing["qty"])
            except Exception:
                self.notice(key, "ENTRY_UNCERTAIN", "주문 결과 불명: 거래소 조회로 복구, 재주문 금지")
            self.reconcile()
        except Exception as exc:
            # Fail closed. Do not leak signed URLs or credentials through exceptions.
            self.notice(event["id"], "ENTRY_SKIPPED" if key not in self.store.state["trades"] else "EXECUTION_ERROR",
                        str(exc) if type(exc) is ValueError else type(exc).__name__)

    def emergency(self, key, t, reason):
        t["phase"] = "EMERGENCY"
        self.notice(key, "EMERGENCY_PENDING", reason)
        self.close_position(key, t)

    def close_position(self, key, t):
        position = self.api.position()
        qty = abs(decimal(position["positionAmt"]))
        if not qty:
            return  # Finish only in reconcile after order/position confirmation.
        sign = t["sign"]
        if decimal(position["positionAmt"]) * sign < 0 or qty > decimal(t["qty"]):
            raise ValueError("Foreign position mutation: manual reconciliation required")
        # Only retry after positively seeing a previous attempt terminal. Unknown
        # results stay pending, avoiding duplicate MARKET closes and reverse trades.
        if t["emergency_ids"]:
            previous = self.api.order(t["emergency_ids"][-1])
            if previous is None or previous["status"] not in ORDER_DONE:
                self.notice(key, "EMERGENCY_UNCERTAIN", "청산 결과 불명: 신규 진입 차단, 실제 주문 조회 중")
                return
        cid = t["entry_id"][:-1] + "x" + str(len(t["emergency_ids"]))
        t["emergency_ids"].append(cid)
        self.store.save()
        self.api.market(cid, "SELL" if sign > 0 else "BUY", str(qty), reduce=True)

    def ensure_protection(self, t, name, kind):
        cid = t[name + "_id"]
        order = self.api.algo(cid)
        if order is None and cid not in t["attempted"]:
            t["attempted"].append(cid)
            self.store.save()
            try:
                self.api.protect(cid, "SELL" if t["sign"] > 0 else "BUY", kind, t[name if name == "tp" else "stop"])
            except Exception:
                pass  # Resolve an accepted-but-lost reply by its deterministic ID.
            order = self.api.algo(cid)
        if not order or order["algoStatus"] != "NEW":
            raise ValueError("Protection not confirmed active")
        expected = t["tp"] if name == "tp" else t["stop"]
        if (order["symbol"] != SYMBOL or order["orderType"] != kind or
                order["side"] != ("SELL" if t["sign"] > 0 else "BUY") or
                order["positionSide"] != "BOTH" or str(order["closePosition"]).lower() != "true" or
                order["workingType"] != "MARK_PRICE" or decimal(order["triggerPrice"]) != decimal(expected)):
            raise ValueError("Protection parameters do not match persisted intent")

    def finish(self, key, t):
        # Close-all orders must be cleaned before any subsequent plan can trade.
        # Leave time for in-flight signed submissions to age beyond recvWindow.
        if int(self.clock() * 1000) - t.get("last_protection_at", t["created"]) < 10_000:
            return
        for cid in t["emergency_ids"]:
            order = self.api.order(cid)
            if not order or order["status"] not in ORDER_DONE:
                raise ValueError("Emergency order not terminal; account remains blocked")
        result = "EMERGENCY_CLOSED" if t["phase"] == "EMERGENCY" else "CLOSED_OTHER"
        for name, outcome in (("sl", "CLOSED_SL"), ("tp", "CLOSED_TP")):
            cid = t[name + "_id"]
            order = self.api.algo(cid)
            if order:
                actual = order.get("actualOrderId")
                if actual and str(actual) != "0":
                    fill = self.api.request("GET", "/fapi/v1/order", {"symbol": SYMBOL, "orderId": actual})
                    if fill["status"] == "FILLED" and result != "EMERGENCY_CLOSED":
                        result = outcome
                    if fill["status"] not in ORDER_DONE:
                        raise ValueError("Conditional child order still working")
                if order["algoStatus"] not in ALGO_DONE:
                    self.api.cancel_algo(cid)
        own = {t["sl_id"], t["tp_id"]}
        if any(o["clientAlgoId"] in own for o in self.api.open_algos()):
            raise ValueError("Exit cleanup not confirmed")
        if decimal(self.api.position()["positionAmt"]):
            raise ValueError("Position reopened during exit cleanup")
        t["phase"] = result
        self.notice(key, result, "거래소 포지션 0 및 보호 주문 정리 확인. 같은 플랜 재진입 없음")

    def reconcile(self):
        if self.mode != "live":
            return
        for key, t in self.active():
            try:
                order = self.api.order(t["entry_id"])
                if not order:
                    # Could be delayed or never sent (crash after journaling). Never
                    # infer it safe to resubmit from one negative lookup.
                    self.notice(key, "ENTRY_UNCERTAIN", "진입 주문 확인 불가: 신규 진입 차단, 수동 확인 필요")
                    continue
                if order["status"] not in ORDER_DONE:
                    self.api.cancel_order(t["entry_id"])
                    order = self.api.order(t["entry_id"])
                    if not order or order["status"] not in ORDER_DONE:
                        raise ValueError("Entry remainder not terminal")
                filled = decimal(order["executedQty"])
                if not filled:
                    if decimal(self.api.position()["positionAmt"]):
                        raise ValueError("Position exists despite zero entry fill; manual reconciliation required")
                    t["phase"] = "NO_FILL"
                    self.notice(key, "NO_FILL", "진입 주문 종료, 체결 수량 0")
                    continue
                position = self.api.position()
                amount = decimal(position["positionAmt"])
                if not amount:
                    self.finish(key, t)
                    continue
                if amount * t["sign"] <= 0 or abs(amount) > filled:
                    raise ValueError("Foreign position mutation: not adopted")
                if t["phase"] == "EMERGENCY":
                    self.close_position(key, t)
                    continue
                if t["phase"] == "ENTRY_PENDING":
                    t["filled"] = str(filled)
                    t["average"] = str(decimal(order["avgPrice"]))
                    t["phase"] = "ENTRY_FILLED"
                    self.notice(key, "ENTRY_FILLED", f"{t['scenario']} {filled} BTC, 평단 {t['average']}")
                # SL first. If either order cannot be confirmed, flatten rather
                # than continuing with an unprotected or partly protected trade.
                try:
                    t["last_protection_at"] = int(self.clock() * 1000)
                    self.store.save()
                    self.ensure_protection(t, "sl", "STOP_MARKET")
                    self.ensure_protection(t, "tp", "TAKE_PROFIT_MARKET")
                except Exception:
                    self.emergency(key, t, "TP/SL 확인 실패: 전량 시장가 청산 시도")
                    continue
                avg, stop, tp = decimal(t["average"]), decimal(t["stop"]), decimal(t["tp"])
                fee, slip, sign = decimal(t["fee"]), decimal(t["slip"]), t["sign"]
                risk = unit_risk(avg, stop, fee, slip, sign)
                reward = sign * (tp - avg) - (avg + tp) * fee - tp * slip
                liquidation = decimal(position["liquidationPrice"])
                if (risk <= 0 or filled * risk > decimal(t["budget"]) or reward / risk < D("1.5") or
                        abs(avg - decimal(t["planned_entry"])) > decimal(t["tolerance"]) or
                        (liquidation > 0 and sign * (stop - liquidation) <= 0)):
                    self.emergency(key, t, "실제 체결 위험/손익비/가격/청산가 검사 실패")
                    continue
                t["phase"] = "PROTECTED"
                self.notice(key, "PROTECTION_SET", f"전량 TP {tp} / SL {stop}, Mark Price 기준. 추가 진입 없음")
            except Exception as exc:
                self.notice(key, "EXECUTION_ERROR", f"{type(exc).__name__}: 복구 중, 신규 진입 차단. 거래소 상태 확인 필요")

    def cycle(self, monitor, market_ok, paused=False):
        self.reconcile()  # Runs even when plan expired, PAUSE exists, or data failed.
        for event in list(monitor.state["events"]):
            self.handle(monitor, event, market_ok, paused)
