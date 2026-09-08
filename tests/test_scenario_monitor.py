from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scenario_monitor.main import (
    Candle, DEFAULT_PLAN, FRESH_MS, HOUR, MINUTE, Monitor, Plan,
    aggregate, flush_outbox, save_state, timestamp,
)

START = timestamp("2026-09-08T00:00:00+00:00")


def make_plan(**changes):
    raw = copy.deepcopy(DEFAULT_PLAN)
    raw.update(as_of="2026-09-08T00:00:00+00:00",
               expires_at="2026-09-08T04:00:00+00:00")
    raw.update(changes)
    return Plan.parse(raw)


def bar(start, price=79000, volume=1, **kw):
    values = dict(open=price, high=price + 1, low=price - 1,
                  close=price, volume=volume)
    values.update(kw)
    return Candle(start, start + MINUTE, **values)


def history():
    return [bar(START - i * MINUTE) for i in range(1260, 0, -1)]


def append_segment(candles, minutes, **kw):
    start = candles[-1].end
    candles.extend(bar(start + i * MINUTE, **kw) for i in range(minutes))


def setup(key):
    candles = history()
    params = {
        "A": (15, dict(open=79500, high=79550, low=79390, close=79400, volume=2)),
        "B": (60, dict(open=78900, high=78910, low=78490, close=78500, volume=2)),
        "C": (15, dict(open=78600, high=78780, low=78550, close=78770, volume=2)),
        "D": (60, dict(open=79900, high=80110, low=79890, close=80100, volume=2)),
    }
    minutes, values = params[key]
    append_segment(candles, minutes, **values)
    return candles


def retest(candles, key):
    params = {
        "A": (5, dict(open=79480, high=79500, low=79430, close=79440)),
        "B": (15, dict(open=78680, high=78690, low=78570, close=78590)),
        "C": (5, dict(open=78710, high=78790, low=78690, close=78770)),
        "D": (15, dict(open=79900, high=79980, low=79890, close=79960)),
    }
    minutes, values = params[key]
    append_segment(candles, minutes, **values)


class ScenarioMonitorTests(unittest.TestCase):
    def test_all_four_scenarios_require_separate_signal_and_retest(self):
        for key in "ABCD":
            with self.subTest(key=key):
                monitor = Monitor(make_plan())
                candles = setup(key)
                monitor.process(candles, candles[-1].end)
                self.assertEqual(monitor.state["scenarios"][key]["phase"], "ARMED")
                retest(candles, key)
                monitor.process(candles, candles[-1].end)
                self.assertEqual(monitor.state["scenarios"][key]["phase"], "READY")
                events = [e["kind"] for e in monitor.state["events"] if e["scenario"] == key]
                self.assertEqual(events, ["ARMED", "CONFIRMED", "ENTRY_READY"])

    def test_volume_baseline_excludes_signal_bar(self):
        candles = setup("A")
        for i in range(-15, 0):
            c = candles[i]
            candles[i] = Candle(c.start, c.end, c.open, c.high, c.low, c.close, 1.3)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "ARMED")

    def test_insufficient_volume_does_not_arm(self):
        candles = setup("A")
        for i in range(-15, 0):
            c = candles[i]
            candles[i] = Candle(c.start, c.end, c.open, c.high, c.low, c.close, 1.29)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "WATCHING")

    def test_unfinished_candles_cannot_signal(self):
        candles = setup("A")
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end - 1)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "WATCHING")

    def test_invalidation_precedes_entry_at_same_close(self):
        plan = make_plan()
        monitor = Monitor(plan)
        st = monitor.state["scenarios"]["A"]
        st.update(phase="CONFIRMED", armed_at=START + 30 * MINUTE)
        monitor.state["last_end"] = START + 59 * MINUTE
        candles = history()
        append_segment(candles, 60, price=79700)
        monitor.process(candles, candles[-1].end)
        self.assertEqual(st["phase"], "INVALIDATED")

    def test_a_hourly_close_invalidates_even_before_signal(self):
        candles = history()
        append_segment(candles, 60, price=79700)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "INVALIDATED")

    def test_b_and_d_are_not_invalidated_before_their_signal(self):
        candles = history()
        append_segment(candles, 60, price=79000)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        for key in "BD":
            self.assertEqual(monitor.state["scenarios"][key]["phase"], "WATCHING")

    def test_hourly_invalidation_for_each_armed_scenario(self):
        for key, price in [("A", 79651), ("B", 78751), ("C", 78549), ("D", 79879)]:
            with self.subTest(key=key):
                monitor = Monitor(make_plan())
                st = monitor.state["scenarios"][key]
                st.update(phase="ARMED", armed_at=START + 30 * MINUTE)
                monitor.state["last_end"] = START + 59 * MINUTE
                candles = history()
                append_segment(candles, 60, price=price)
                monitor.process(candles, candles[-1].end)
                self.assertEqual(st["phase"], "INVALIDATED")

    def test_c_floor_touch_invalidates_before_signal(self):
        candles = history()
        append_segment(candles, 1, price=78500, low=78420)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["C"]["phase"], "INVALIDATED")

    def test_wick_between_polls_is_not_missed(self):
        candles = setup("C")
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        append_segment(candles, 1, price=78700, low=78400)
        append_segment(candles, 2, price=78700)
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["C"]["phase"], "INVALIDATED")

    def test_first_target_before_entry_cancels_each_signal(self):
        for key in "ABCD":
            with self.subTest(key=key):
                monitor = Monitor(make_plan())
                candles = setup(key)
                monitor.process(candles, candles[-1].end)
                s = next(s for s in monitor.plan.scenarios if s.key == key)
                append_segment(candles, 1, price=s.targets[0])
                monitor.process(candles, candles[-1].end)
                self.assertEqual(monitor.state["scenarios"][key]["phase"], "MISSED")

    def test_exact_signal_deadline_expires_without_any_new_candles(self):
        monitor = Monitor(make_plan())
        candles = setup("A")
        monitor.process(candles, candles[-1].end)
        deadline = candles[-1].end + HOUR
        monitor.tick(deadline - 1)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "ARMED")
        monitor.tick(deadline)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "EXPIRED")

    def test_global_expiry_wins_over_later_signal_deadline(self):
        monitor = Monitor(make_plan())
        st = monitor.state["scenarios"]["A"]
        st.update(phase="ARMED", armed_at=monitor.plan.end - 10 * MINUTE)
        monitor.tick(monitor.plan.end)
        self.assertTrue(all(s["phase"] == "EXPIRED" for s in monitor.state["scenarios"].values()))

    def test_restart_preserves_expiry_and_does_not_duplicate_events(self):
        monitor = Monitor(make_plan())
        candles = setup("A")
        retest(candles, "A")
        monitor.process(candles, candles[-1].end)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, monitor.state)
            restored = Monitor(make_plan(), json.loads(path.read_text()))
            count = len(restored.state["events"])
            restored.process(candles, candles[-1].end)
            self.assertEqual(len(restored.state["events"]), count)
            restored.tick(START + 75 * MINUTE)
            self.assertEqual(restored.state["scenarios"]["A"]["phase"], "EXPIRED")

    def test_replayed_old_entry_is_never_actionable(self):
        candles = setup("A")
        retest(candles, "A")
        append_segment(candles, 5, price=79440)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "MISSED")
        self.assertFalse(any(e["kind"] == "ENTRY_READY" for e in monitor.state["events"]))

    def test_confirmed_retest_away_from_entry_does_not_issue_entry_alert(self):
        candles = setup("A")
        append_segment(candles, 5, open=79480, high=79500, low=79300, close=79310)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "CONFIRMED")

    def test_wrong_retest_body_does_not_confirm(self):
        candles = setup("A")
        append_segment(candles, 5, open=79420, high=79500, low=79410, close=79440)
        monitor = Monitor(make_plan())
        monitor.process(candles, candles[-1].end)
        self.assertEqual(monitor.state["scenarios"]["A"]["phase"], "ARMED")

    def test_missing_or_duplicate_minutes_are_rejected(self):
        for duplicate in (False, True):
            candles = setup("A")
            if duplicate:
                candles.insert(-1, candles[-2])
            else:
                del candles[-2]
            with self.assertRaises(ValueError):
                Monitor(make_plan()).process(candles, candles[-1].end)

    def test_stale_data_is_rejected(self):
        candles = setup("A")
        with self.assertRaises(ValueError):
            Monitor(make_plan()).process(candles, candles[-1].end + FRESH_MS + 1)

    def test_manual_suspend_does_not_reset_on_next_tick(self):
        monitor = Monitor(make_plan())
        monitor.suspend(START, "news pause")
        monitor.tick(START + HOUR)
        self.assertTrue(all(s["phase"] == "SUSPENDED" for s in monitor.state["scenarios"].values()))

    def test_builtin_deadline_is_fixed_not_relative_to_startup(self):
        plan = Plan.parse(DEFAULT_PLAN)
        self.assertEqual(plan.start, timestamp("2026-09-08T11:52:00+09:00"))
        self.assertEqual(plan.end, timestamp("2026-09-08T15:52:00+09:00"))
        monitor = Monitor(plan)
        monitor.tick(plan.end + 10 * HOUR)
        self.assertTrue(all(s["phase"] == "EXPIRED" for s in monitor.state["scenarios"].values()))

    def test_changed_plan_cannot_reuse_saved_state(self):
        with self.assertRaises(ValueError):
            Monitor(make_plan(plan_id="another"), Monitor(make_plan()).state)

    def test_validation_rejects_naive_timestamps_and_nonfinite_prices(self):
        with self.assertRaises(ValueError):
            make_plan(as_of="2026-09-08T00:00:00")
        raw = copy.deepcopy(DEFAULT_PLAN)
        raw["scenarios"][0]["entry"] = float("nan")
        with self.assertRaises(ValueError):
            Plan.parse(raw)

    def test_aggregation_drops_incomplete_buckets(self):
        candles = [bar(START + i * MINUTE) for i in range(14)]
        self.assertEqual(aggregate(candles, 15), [])
        candles.append(bar(START + 14 * MINUTE))
        self.assertEqual(len(aggregate(candles, 15)), 1)

    def test_four_hour_review_emitted_once_without_extending_plan(self):
        raw = copy.deepcopy(DEFAULT_PLAN)
        raw.update(as_of="2026-09-08T03:00:00+00:00", expires_at="2026-09-08T07:00:00+00:00")
        monitor = Monitor(Plan.parse(raw))
        candles = history()
        append_segment(candles, 240)
        monitor.process(candles, START + 4 * HOUR)
        monitor.process(candles, START + 4 * HOUR)
        self.assertEqual(sum(e["kind"] == "REVIEW_DUE" for e in monitor.state["events"]), 1)
        self.assertEqual(monitor.plan.end, START + 7 * HOUR)

    def test_failed_telegram_delivery_keeps_durable_outbox(self):
        monitor = Monitor(make_plan())
        monitor.emit("ALL", "STARTED", START, "start")
        def fail(*_):
            raise RuntimeError("transport unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, monitor.state)
            with self.assertRaises(RuntimeError):
                flush_outbox(monitor, path, "test-token", "test-chat", START, sender=fail)
            self.assertEqual(len(json.loads(path.read_text())["outbox"]), 1)

    def test_obsolete_entry_alert_is_not_delivered_after_network_recovery(self):
        monitor = Monitor(make_plan())
        candles = setup("A")
        retest(candles, "A")
        monitor.process(candles, candles[-1].end)
        sent = []
        def send(_url, payload):
            sent.append(payload["text"])
            return {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            flush_outbox(monitor, Path(directory) / "state.json", "test", "chat",
                         candles[-1].end + FRESH_MS + 1, sender=send)
        self.assertFalse(any("ENTRY_READY" in text for text in sent))
        self.assertEqual(monitor.state["outbox"], [])

    def test_entry_alert_is_suppressed_when_market_connection_is_unavailable(self):
        monitor = Monitor(make_plan())
        candles = setup("A")
        retest(candles, "A")
        monitor.process(candles, candles[-1].end)
        sent = []
        def send(_url, payload):
            sent.append(payload["text"])
            return {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            flush_outbox(monitor, Path(directory) / "state.json", "test", "chat",
                         candles[-1].end, sender=send, market_ok=False)
        self.assertFalse(any("ENTRY_READY" in text for text in sent))

    def test_expired_plan_cli_does_not_contact_exchange(self):
        raw = copy.deepcopy(DEFAULT_PLAN)
        raw.update(as_of="2000-01-01T00:00:00Z", expires_at="2000-01-01T04:00:00Z")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps(raw))
            env = dict(os.environ, SCENARIO_DATA_DIR=directory,
                       SCENARIO_PLAN_PATH=str(plan_path),
                       SCENARIO_TELEGRAM_BOT_TOKEN="", SCENARIO_TELEGRAM_CHAT_ID="")
            result = subprocess.run([sys.executable, str(root / "scenario_monitor/main.py"), "--once"],
                                    env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(next(Path(directory).glob("state-*.json")).read_text())
            self.assertTrue(all(s["phase"] == "EXPIRED" for s in state["scenarios"].values()))
            self.assertFalse(any(e["kind"] == "DATA_UNAVAILABLE" for e in state["events"]))


if __name__ == "__main__":
    unittest.main()
