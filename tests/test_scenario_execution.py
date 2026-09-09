from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

from scenario_monitor.binance_futures import Binance, ExchangeError
from scenario_monitor.execution import Executor, size_order
from scenario_monitor.execution_store import Store
from scenario_monitor.main import DEFAULT_PLAN, Monitor, Plan

RULES = {
    "PRICE_FILTER": {"tickSize": "0.1"},
    "LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
    "MARKET_LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
    "MIN_NOTIONAL": {"notional": "50"},
}


class Fake:
    def __init__(self):
        self.orders, self.algos, self.sent = {}, {}, []
        self.amount = D(0)
        self.avg = D("78410")
        self.quote_value = (D("78409.9"), self.avg, self.avg)
        self.timeout_entry = self.timeout_protection = False
        self.fail_protection = self.unknown_entry = False
        self.fill_ratio = D(1)
        self.losses = D(0)
        self.foreign = False
        self.close_timeout = False

    def rules(self): return RULES
    def quote(self): return self.quote_value
    def balance(self): return D(200), D(200)
    def fee(self): return D("0.0005")
    def daily_losses(self, *_): return self.losses
    def configure(self): self.sent.append("configure")
    def positions(self):
        if self.foreign: return [{"symbol": "ETHUSDT", "positionAmt": "1"}]
        return [{"symbol": "BTCUSDT", "positionAmt": str(self.amount), "positionSide": "BOTH"}] if self.amount else []
    def position(self):
        return {"positionAmt": str(self.amount), "entryPrice": str(self.avg), "liquidationPrice": "52000" if self.amount > 0 else "105000"}
    def open_orders(self): return []
    def open_algos(self): return [v for v in self.algos.values() if v["algoStatus"] == "NEW"]
    def order(self, cid): return self.orders.get(cid)
    def market(self, cid, side, qty, reduce=False):
        self.sent.append(("market", cid, side, qty, reduce))
        if self.unknown_entry and not reduce: raise TimeoutError()
        qty = min(D(qty), abs(self.amount)) if reduce else (D(qty) * self.fill_ratio).quantize(D("0.001"))
        self.amount += qty * (1 if side == "BUY" else -1)
        self.orders[cid] = {"status": "FILLED", "executedQty": str(qty), "avgPrice": str(self.avg)}
        if (self.timeout_entry and not reduce) or (self.close_timeout and reduce): raise TimeoutError()
        return self.orders[cid]
    def cancel_order(self, cid): self.orders[cid]["status"] = "CANCELED"
    def algo(self, cid): return self.algos.get(cid)
    def protect(self, cid, side, kind, price):
        self.sent.append(("protect", cid))
        if self.fail_protection: raise ExchangeError(-2021)
        self.algos[cid] = dict(clientAlgoId=cid, symbol="BTCUSDT", side=side, orderType=kind,
                               algoStatus="NEW", triggerPrice=price, positionSide="BOTH",
                               closePosition=True, workingType="MARK_PRICE")
        if self.timeout_protection: raise TimeoutError()
    def cancel_algo(self, cid): self.algos[cid]["algoStatus"] = "CANCELED"
    def request(self, method, path, params):
        return {"status": "FILLED"}


def ready(side="long"):
    raw = copy.deepcopy(DEFAULT_PLAN)
    raw.update(plan_id="execution-test", as_of="2026-09-08T00:00:00Z", expires_at="2026-09-08T04:00:00Z")
    s = raw["scenarios"][2]
    s.update(side=side, entry=78410, add=78300 if side == "long" else 78500,
             stop=77900 if side == "long" else 78920,
             targets=[79650, 79800, 80000] if side == "long" else [77170, 77000, 76800])
    m = Monitor(Plan.parse(raw))
    now = m.plan.start + 20 * 60_000
    m.state["scenarios"]["C"].update(phase="READY", armed_at=now - 5 * 60_000)
    m.emit("C", "ENTRY_READY", now, "ready")
    return m, m.state["events"][-1], now


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "execution.sqlite3"
        self.store = Store(self.path)
        self.api = Fake()
        self.m, self.event, self.now = ready()
        self.engine = Executor(self.store, self.api, clock=lambda: self.now / 1000)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def enter(self): self.engine.handle(self.m, self.event)
    def trade(self): return self.store.state["trades"]["execution-test"]
    def markets(self): return [x for x in self.api.sent if isinstance(x, tuple) and x[0] == "market"]

    def test_market_entry_full_exits_and_risk_rounding(self):
        self.enter()
        t = self.trade()
        self.assertEqual(t["phase"], "PROTECTED")
        self.assertEqual(t["qty"], "0.003")
        self.assertLessEqual(D(t["estimate"]), D(2))
        self.assertEqual(len(self.api.algos), 2)
        self.assertEqual(self.api.algos[t["sl_id"]]["orderType"], "STOP_MARKET")
        self.assertEqual(self.api.algos[t["tp_id"]]["triggerPrice"], "79650")

    def test_short_uses_sell_entry_buy_exits(self):
        self.m, self.event, self.now = ready("short")
        self.enter()
        self.assertEqual(self.markets()[0][2], "SELL")
        self.assertTrue(all(x["side"] == "BUY" for x in self.api.algos.values()))

    def test_timeout_after_entry_acceptance_does_not_duplicate(self):
        self.api.timeout_entry = True
        self.enter()
        self.enter()
        self.engine.reconcile()
        self.assertEqual(len(self.markets()), 1)
        self.assertEqual(self.trade()["phase"], "PROTECTED")

    def test_restart_resolves_persisted_intent_without_sending_entry(self):
        self.api.unknown_entry = True
        self.enter()
        t = self.trade()
        self.api.orders[t["entry_id"]] = dict(status="FILLED", executedQty="0.003", avgPrice="78410")
        self.api.amount = D("0.003")
        self.store.close()
        self.store = Store(self.path)
        engine = Executor(self.store, self.api, clock=lambda: self.now / 1000)
        engine.reconcile()
        self.assertEqual(len(self.markets()), 1)
        self.assertEqual(self.trade()["phase"], "PROTECTED")

    def test_unknown_entry_blocks_all_future_plans_and_never_resubmits(self):
        self.api.unknown_entry = True
        self.enter()
        self.now += 5 * 3600_000
        self.engine.reconcile()
        self.assertEqual(self.trade()["phase"], "ENTRY_PENDING")
        self.assertTrue(self.engine.active())
        self.assertEqual(len(self.markets()), 1)

    def test_partial_fill_protects_only_actual_position(self):
        self.api.fill_ratio = D(1) / 3
        self.enter()
        self.assertEqual(D(self.trade()["filled"]), D("0.001"))
        self.assertTrue(all(x["closePosition"] for x in self.api.algos.values()))

    def test_protection_acceptance_timeout_is_reconciled(self):
        self.api.timeout_protection = True
        self.enter()
        self.assertEqual(self.trade()["phase"], "PROTECTED")
        self.assertEqual(len(self.markets()), 1)

    def test_protection_failure_emergency_closes_and_confirms_flat(self):
        self.api.fail_protection = True
        self.enter()
        self.assertEqual(self.trade()["phase"], "EMERGENCY")
        self.assertEqual(self.api.amount, 0)
        self.assertTrue(self.markets()[-1][-1])
        self.now += 11_000
        self.engine.reconcile()
        self.assertEqual(self.trade()["phase"], "EMERGENCY_CLOSED")

    def test_close_response_timeout_does_not_duplicate_close(self):
        self.api.fail_protection = self.api.close_timeout = True
        self.enter()
        self.now += 11_000
        self.engine.reconcile()
        self.assertEqual(len(self.markets()), 2)
        self.assertEqual(self.trade()["phase"], "EMERGENCY_CLOSED")

    def test_expired_plan_still_recovers_active_position(self):
        self.enter()
        self.now = self.m.plan.end + 1000
        self.m.tick(self.now)
        self.api.algos[self.trade()["sl_id"]]["algoStatus"] = "CANCELED"
        self.engine.cycle(self.m, market_ok=False, paused=True)
        self.assertEqual(self.api.amount, 0)
        self.assertEqual(self.trade()["phase"], "EMERGENCY")

    def test_take_profit_cleans_sibling_then_blocks_same_plan(self):
        self.enter()
        t = self.trade()
        self.api.amount = D(0)
        self.api.algos[t["tp_id"]].update(algoStatus="FINISHED", actualOrderId="123")
        self.now += 11_000
        self.engine.reconcile()
        self.assertEqual(t["phase"], "CLOSED_TP")
        self.assertEqual(self.api.open_algos(), [])
        event = dict(self.event, id="another-event", at=self.now)
        self.engine.handle(self.m, event)
        self.assertEqual(len(self.markets()), 1)

    def test_bad_gross_rr_is_skipped_without_selecting_further_target(self):
        raw = copy.deepcopy(self.m.plan.raw)
        raw["scenarios"][2]["targets"] = [78950, 79450, 79800]
        self.m.plan = Plan.parse(raw)
        self.enter()
        self.assertEqual(self.markets(), [])
        self.assertIn("Gross reward/risk", self.store.state["outbox"][-1])

    def test_gross_rr_uses_executable_quote_not_fee_slippage_reserve(self):
        self.m, self.event, self.now = ready("short")
        raw = copy.deepcopy(self.m.plan.raw)
        raw["scenarios"][2].update(entry=78900, add=78900, stop=79060,
                                    targets=[78520, 78250, 77620])
        self.m.plan = Plan.parse(raw)
        self.api.quote_value = (D("78893.8"), D("78894.0"), D("78893.8"))
        self.api.avg = D("78893.8")
        self.enter()
        self.assertEqual(self.trade()["phase"], "PROTECTED")
        self.assertEqual(self.markets()[0][2], "SELL")

    def test_actual_fill_beyond_slippage_limit_is_closed(self):
        raw = copy.deepcopy(self.m.plan.raw)
        raw["entry_tolerance"] = 100
        self.m.plan = Plan.parse(raw)
        self.api.avg = D("78450")
        self.enter()
        self.assertEqual(self.trade()["phase"], "EMERGENCY")

    def test_actual_fill_rechecks_gross_rr(self):
        raw = copy.deepcopy(self.m.plan.raw)
        raw["scenarios"][2]["targets"] = [79175, 79600, 80100]
        self.m.plan = Plan.parse(raw)
        self.api.avg = D("78440")
        self.enter()
        self.assertEqual(self.trade()["phase"], "EMERGENCY")

    def test_all_entry_gates(self):
        cases = ["stale", "future", "expired", "review", "paused", "data", "phase", "foreign", "orders", "quote", "loss", "tick"]
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temp:
                    store = Store(Path(temp) / "s.db")
                    api = Fake()
                    m, e, now = ready()
                    market_ok, paused = True, False
                    if case == "stale": now += 30_001
                    if case == "future": now -= 1
                    if case == "expired": now = m.plan.end
                    if case == "review": m.state["review_at"] = now
                    if case == "paused": paused = True
                    if case == "data": market_ok = False
                    if case == "phase": m.state["scenarios"]["C"]["phase"] = "INVALIDATED"
                    if case == "foreign": api.foreign = True
                    if case == "orders": api.open_orders = lambda: [{"symbol": "BTCUSDT"}]
                    if case == "quote": api.quote_value = (D(79000), D(79001), D(79000))
                    if case == "loss": api.losses = D(4)
                    if case == "tick": api.rules = lambda: dict(RULES, PRICE_FILTER={"tickSize": "3"})
                    Executor(store, api, clock=lambda: now / 1000).handle(m, e, market_ok, paused)
                    self.assertFalse(any(isinstance(x, tuple) and x[0] == "market" for x in api.sent))
                    store.close()

    def test_recheck_time_after_slow_preflight(self):
        def quote():
            self.now += 31_000
            return self.api.quote_value
        self.api.quote = quote
        self.enter()
        self.assertEqual(self.markets(), [])

    def test_actual_fill_beyond_tolerance_closes_without_changing_plan(self):
        self.api.avg = D(78500)
        self.enter()
        self.assertEqual(self.trade()["phase"], "EMERGENCY")
        self.assertEqual(self.trade()["stop"], "77900")

    def test_journal_notification_failure_preserves_outbox(self):
        self.enter()
        before = len(self.store.state["outbox"])
        def fail(_): raise TimeoutError()
        with self.assertRaises(TimeoutError): self.store.flush(fail)
        self.assertEqual(len(self.store.state["outbox"]), before)

    def test_dry_run_never_calls_signed_or_mutating_methods(self):
        for method in ("configure", "balance", "fee", "positions", "market", "protect", "daily_losses"):
            setattr(self.api, method, lambda *a: self.fail("signed call in dry-run"))
        self.engine.mode = "dry-run"
        self.enter()
        self.assertEqual(self.trade()["phase"], "DRY_RUN")

    def test_nonfinite_values_fail_closed(self):
        self.api.quote_value = (D("NaN"), D(78410), D(78410))
        self.enter()
        self.assertEqual(self.markets(), [])

    def test_minimum_order_is_skipped_instead_of_rounding_up(self):
        self.api.balance = lambda: (D(1), D(1))
        self.enter()
        self.assertEqual(self.markets(), [])

    def test_daily_remaining_budget_is_reserved_and_persisted(self):
        self.api.losses = D("3")
        self.enter()
        self.assertEqual(self.trade()["qty"], "0.001")
        self.assertLessEqual(D(self.trade()["estimate"]), D(1))
        self.assertTrue(self.store.state["days"])

    def test_new_plan_cannot_replace_old_trade_protection(self):
        self.enter()
        old_stop = self.trade()["stop"]
        raw = copy.deepcopy(self.m.plan.raw)
        raw["plan_id"] = "next-plan"
        self.m.plan = Plan.parse(raw)
        self.engine.cycle(self.m, market_ok=True)
        self.assertEqual(len(self.markets()), 1)
        self.assertEqual(self.trade()["stop"], old_stop)

    def test_close_cleanup_failure_keeps_account_blocked(self):
        self.enter()
        self.api.amount = D(0)
        self.now += 11_000
        self.api.cancel_algo = lambda _: None  # Exchange did not remove sibling.
        self.engine.reconcile()
        self.assertEqual(self.trade()["phase"], "PROTECTED")
        self.assertTrue(self.engine.active())

    def test_protection_with_wrong_parameters_causes_emergency(self):
        self.enter()
        self.api.algos[self.trade()["sl_id"]]["triggerPrice"] = "70000"
        self.engine.reconcile()
        self.assertEqual(self.trade()["phase"], "EMERGENCY")
        self.assertEqual(self.api.amount, 0)

    def test_zero_fill_does_not_place_exits(self):
        self.api.fill_ratio = D(0)
        self.enter()
        self.assertEqual(self.trade()["phase"], "NO_FILL")
        self.assertEqual(self.api.algos, {})

    def test_clock_boundary_blocks_even_without_review_event(self):
        raw = copy.deepcopy(self.m.plan.raw)
        raw.update(as_of="2026-09-08T03:00:00Z", expires_at="2026-09-08T07:00:00Z")
        self.m = Monitor(Plan.parse(raw))
        self.now = self.m.plan.start + 3600_000
        self.m.state["scenarios"]["C"].update(phase="READY", armed_at=self.now - 60_000)
        self.m.emit("C", "ENTRY_READY", self.now, "ready")
        self.event = self.m.state["events"][-1]
        self.enter()
        self.assertIn("REVIEW_DUE", self.store.state["outbox"][-1])
        self.assertEqual(self.markets(), [])


class AdapterTests(unittest.TestCase):
    def test_market_and_algo_payloads_use_distinct_endpoints(self):
        captured = []
        def opener(req, timeout):
            captured.append(req)
            return io.BytesIO(b'{}')
        api = Binance("test-key", "test-secret", opener=opener, clock=lambda: 1000)
        api.market("entry", "BUY", "0.003")
        api.protect("stop", "SELL", "STOP_MARKET", "77900")
        api.market("close", "SELL", "0.003", reduce=True)
        self.assertTrue(captured[0].full_url.endswith("/fapi/v1/order"))
        self.assertTrue(captured[1].full_url.endswith("/fapi/v1/algoOrder"))
        body = parse_qs(captured[1].data.decode())
        self.assertEqual(body["closePosition"], ["true"])
        self.assertEqual(body["workingType"], ["MARK_PRICE"])
        self.assertNotIn("quantity", body)
        self.assertNotIn("reduceOnly", body)
        self.assertEqual(parse_qs(captured[2].data.decode())["reduceOnly"], ["true"])
        self.assertIn("signature", body)

    def test_http_errors_redact_secrets_and_do_not_retry(self):
        count = []
        def opener(req, timeout):
            count.append(1)
            raise HTTPError("https://secret/?signature=secret", 503, "secret", {},
                            io.BytesIO(b'{"code":-1007,"msg":"secret"}'))
        with self.assertRaises(ExchangeError) as caught:
            Binance("key", "secret", opener=opener).market("id", "BUY", "0.003")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(count), 1)

    def test_only_definitive_not_found_returns_none(self):
        api = Binance()
        with patch.object(api, "request", side_effect=ExchangeError(-2013)):
            self.assertIsNone(api.order("id"))
        with patch.object(api, "request", side_effect=ExchangeError(-1007)):
            with self.assertRaises(ExchangeError): api.order("id")

    def test_daily_income_paginates_and_does_not_credit_rebates(self):
        api = Binance()
        rows = [{"asset": "USDT", "incomeType": "COMMISSION", "income": "-0.001"}] * 1000
        with patch.object(api, "request", side_effect=[rows, [
            {"asset": "USDT", "incomeType": "COMMISSION_REBATE", "income": "100"},
            {"asset": "USDT", "incomeType": "REALIZED_PNL", "income": "-0.5"}]]):
            self.assertEqual(api.daily_losses(0, 100), D("1.5"))


if __name__ == "__main__":
    unittest.main()
