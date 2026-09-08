"""Read-only, expiring BTCUSDT scenario monitor. Python 3.12, stdlib only.

No exchange credentials, orders, position inference, or automatic re-analysis.
The built-in September 8 plan expires at its original absolute deadline.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import signal
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG = logging.getLogger("scenario_monitor")
MINUTE = 60_000
HOUR = 60 * MINUTE
FRESH_MS = 90_000
TERMINAL = {"EXPIRED", "INVALIDATED", "MISSED", "SUSPENDED"}
BASE_URL = "https://fapi.binance.com"


def timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Plan timestamps must include a timezone")
    return int(parsed.timestamp() * 1000)


def iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    side: str
    signal_minutes: int
    signal_level: float
    volume_ratio: float
    setup_zone: tuple[float, float] | None
    retest_minutes: int
    retest_zone: tuple[float, float]
    confirmation: float
    invalidation: float
    invalidate_before_signal: bool
    entry: float
    add: float
    stop: float
    targets: tuple[float, float, float]


DEFAULT_PLAN = {
    "plan_id": "btc-20260908-1152-kst-v1",
    "as_of": "2026-09-08T11:52:00+09:00",
    "expires_at": "2026-09-08T15:52:00+09:00",
    "signal_ttl_minutes": 60,
    "entry_tolerance": 30,
    "scenarios": [asdict(s) for s in (
        Scenario("A", "반등 실패 숏", "short", 15, 79450, 1.3,
                 (79450, 79650), 5, (79450, 79650), 79450, 79650, True,
                 79450, 79580, 79850, (78900, 78680, 77950)),
        Scenario("B", "지지 붕괴 숏", "short", 60, 78550, 1.5,
                 None, 15, (78580, 78700), 78620, 78750, False,
                 78580, 78680, 78980, (77950, 77300, 76400)),
        Scenario("C", "저점 이탈 후 회복 롱", "long", 15, 78750, 1.3,
                 (78420, 78620), 5, (78700, 78780), 78750, 78550, False,
                 78780, 78700, 78420, (79250, 79600, 80100)),
        Scenario("D", "상단 회복 롱", "long", 60, 80050, 1.5,
                 None, 15, (79880, 79950), 79950, 79880, False,
                 79950, 79880, 79550, (80500, 81300, 82150)),
    )],
}


@dataclass(frozen=True)
class Plan:
    raw: dict
    scenarios: tuple[Scenario, ...]
    start: int
    end: int
    ttl: int
    tolerance: float
    digest: str

    @classmethod
    def parse(cls, raw: dict) -> Plan:
        start, end = timestamp(raw["as_of"]), timestamp(raw["expires_at"])
        ttl = float(raw["signal_ttl_minutes"]) * MINUTE
        tolerance = float(raw["entry_tolerance"])
        if not raw["plan_id"] or not 0 < end - start <= 4 * HOUR:
            raise ValueError("Plan must have an ID and an absolute lifetime <= 4 hours")
        if not math.isfinite(ttl) or not 0 < ttl <= HOUR:
            raise ValueError("Signal TTL must be in (0, 60] minutes")
        if not math.isfinite(tolerance) or not 0 <= tolerance <= 100:
            raise ValueError("Entry tolerance must be in [0, 100] USDT")
        scenarios = tuple(Scenario(**row) for row in raw["scenarios"])
        if {s.key for s in scenarios} != {"A", "B", "C", "D"} or len(scenarios) != 4:
            raise ValueError("Exactly A, B, C, D scenarios are required")
        for s in scenarios:
            if s.side not in {"long", "short"}:
                raise ValueError("Invalid side")
            expected = {"A": (15, 5), "B": (60, 15), "C": (15, 5), "D": (60, 15)}
            if (s.signal_minutes, s.retest_minutes) != expected[s.key]:
                raise ValueError("Unsupported scenario timeframes")
            levels = [s.signal_level, s.confirmation, s.invalidation, s.entry,
                      s.add, s.stop, *s.targets, *s.retest_zone]
            if s.setup_zone is not None:
                levels.extend(s.setup_zone)
            if any(not math.isfinite(x) or x <= 0 for x in levels):
                raise ValueError("All prices must be finite and positive")
            if len(s.targets) != 3 or len(s.retest_zone) != 2:
                raise ValueError("Expected three targets and a two-price retest zone")
            if s.retest_zone[0] > s.retest_zone[1]:
                raise ValueError("Reversed retest zone")
            if s.setup_zone is not None and (len(s.setup_zone) != 2 or
                                             s.setup_zone[0] >= s.setup_zone[1]):
                raise ValueError("Invalid setup zone")
            if s.key in {"A", "C"} and s.setup_zone is None:
                raise ValueError("A and C require a setup zone")
            if not math.isfinite(s.volume_ratio) or s.volume_ratio < 1:
                raise ValueError("Volume ratio must be >= 1")
            sign = 1 if s.side == "long" else -1
            if any(sign * (e - s.stop) <= 0 for e in (s.entry, s.add)):
                raise ValueError("Stop must be beyond both entries")
            if any(sign * (t - s.entry) <= 0 for t in s.targets):
                raise ValueError("Targets must be in the profitable direction")
            if any(sign * (b - a) <= 0 for a, b in zip(s.targets, s.targets[1:])):
                raise ValueError("Targets must be ordered")
        encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
        return cls(raw, scenarios, start, end, int(ttl), tolerance,
                   hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class Candle:
    start: int
    end: int  # exclusive; Binance closeTime + 1
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def parse(cls, row: list) -> Candle:
        candle = cls(int(row[0]), int(row[6]) + 1,
                     *[float(row[i]) for i in (1, 2, 3, 4, 5)])
        if candle.end - candle.start != MINUTE or candle.start % MINUTE:
            raise ValueError("Invalid 1-minute candle timestamps")
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if any(not math.isfinite(v) for v in values) or candle.volume < 0:
            raise ValueError("Invalid OHLCV values")
        if not 0 < candle.low <= min(candle.open, candle.close) <= max(
                candle.open, candle.close) <= candle.high:
            raise ValueError("Invalid candle range")
        return candle


def aggregate(candles: list[Candle], minutes: int) -> list[Candle]:
    """Only complete UTC-aligned buckets; never infer missing minutes."""
    groups: dict[int, list[Candle]] = {}
    width = minutes * MINUTE
    for c in candles:
        groups.setdefault(c.start // width * width, []).append(c)
    result = []
    for start, group in sorted(groups.items()):
        if len(group) != minutes or any(c.start != start + i * MINUTE
                                       for i, c in enumerate(group)):
            continue
        result.append(Candle(start, start + width, group[0].open,
                             max(c.high for c in group), min(c.low for c in group),
                             group[-1].close, sum(c.volume for c in group)))
    return result


def overlaps(c: Candle, zone: tuple[float, float]) -> bool:
    return c.high >= zone[0] and c.low <= zone[1]


class Monitor:
    def __init__(self, plan: Plan, state: dict | None = None):
        self.plan = plan
        self.state = state if state is not None else {
            "version": 1, "plan_digest": plan.digest, "last_end": plan.start,
            "review_at": 0, "sequence": 0, "outbox": [], "events": [],
            "scenarios": {s.key: {"phase": "WATCHING", "setup_at": 0,
                                   "armed_at": 0, "confirmed_at": 0}
                          for s in plan.scenarios},
        }
        if self.state["version"] != 1 or self.state["plan_digest"] != plan.digest:
            raise ValueError("State belongs to a different plan; use a new state path")
        if set(self.state["scenarios"]) != {s.key for s in plan.scenarios}:
            raise ValueError("Invalid saved scenario state")

    def emit(self, key: str, kind: str, now: int, reason: str, **extra) -> None:
        self.state["sequence"] += 1
        event = {"id": f'{self.plan.raw["plan_id"]}:{self.state["sequence"]}',
                 "scenario": key, "kind": kind, "at": now, "utc": iso(now),
                 "reason": reason, **extra}
        self.state["events"].append(event)
        self.state["outbox"].append(event.copy())

    def end(self, s: Scenario, phase: str, now: int, reason: str) -> None:
        st = self.state["scenarios"][s.key]
        if st["phase"] not in TERMINAL:
            st["phase"] = phase
            self.emit(s.key, phase, now, reason)

    def suspend(self, now: int, reason: str) -> None:
        for s in self.plan.scenarios:
            self.end(s, "SUSPENDED", now, reason)

    def tick(self, now: int) -> None:
        for s in self.plan.scenarios:
            st = self.state["scenarios"][s.key]
            if now >= self.plan.end:
                self.end(s, "EXPIRED", now, "분석 시점 기준 4시간 유효기간 만료")
            elif st["armed_at"] and now >= st["armed_at"] + self.plan.ttl:
                self.end(s, "EXPIRED", now, "신호 발생 후 1시간 진입 대기 만료")

    def process(self, candles: list[Candle], now: int) -> None:
        closed = sorted((c for c in candles if c.end <= now), key=lambda c: c.start)
        if not closed or now - closed[-1].end > FRESH_MS:
            raise ValueError("Stale or empty market data")
        if any(b.start != a.end for a, b in zip(closed, closed[1:])):
            raise ValueError("Market data has gaps or duplicate minutes")
        unseen = [c for c in closed if c.end > self.state["last_end"]]
        if unseen and unseen[0].start > self.state["last_end"]:
            raise ValueError("Cannot recover unobserved market history")
        frames = {n: aggregate(closed, n) for n in (5, 15, 60, 240)}
        lookup = {n: {c.end: (i, c) for i, c in enumerate(bars)}
                  for n, bars in frames.items()}
        for c in unseen:
            self.tick(c.end)
            if c.start < self.plan.start:
                self.state["last_end"] = c.end
                continue
            if c.end in lookup[240] and self.plan.start < c.end < self.plan.end:
                if self.state["review_at"] < c.end:
                    self.state["review_at"] = c.end
                    self.emit("ALL", "REVIEW_DUE", c.end,
                              "4시간봉 확정: 수동 재평가 필요. 가격선/기한 자동 갱신 없음")
            for s in self.plan.scenarios:
                self.process_minute(s, c, frames, lookup, now)
            self.state["last_end"] = c.end
        self.tick(now)

    def process_minute(self, s: Scenario, c: Candle, frames: dict,
                       lookup: dict, now: int) -> None:
        st = self.state["scenarios"][s.key]
        if st["phase"] in TERMINAL:
            return
        long = s.side == "long"
        hour = lookup[60].get(c.end)
        if hour and (s.invalidate_before_signal or st["armed_at"]):
            close = hour[1].close
            invalid = close < s.invalidation if long else close > s.invalidation
            if invalid:
                self.end(s, "INVALIDATED", c.end, f"1시간봉 폐기선 돌파: {close:,.1f}")
                return
        # C's hard floor applies even before its recovery signal. Other stop
        # boundaries apply only after a signal, not to a still-waiting breakout.
        if s.key == "C" or st["armed_at"]:
            touched = c.low <= s.stop if long else c.high >= s.stop
            if touched:
                self.end(s, "INVALIDATED", c.end, "진입 전 손절 경계 도달")
                return
        if st["armed_at"] and c.start >= st["armed_at"]:
            target_hit = c.high >= s.targets[0] if long else c.low <= s.targets[0]
            if target_hit:
                self.end(s, "MISSED", c.end, "진입 확인 없이 첫 익절가 선도달")
                return
        if st["phase"] == "WATCHING":
            if s.setup_zone is not None and overlaps(c, s.setup_zone):
                # A must trade up into its zone; C must actually sweep below it.
                if s.key != "C" or c.low < s.setup_zone[1]:
                    st["setup_at"] = c.end
            signal_bar = lookup[s.signal_minutes].get(c.end)
            if signal_bar is None:
                return
            i, bar = signal_bar
            if bar.start < self.plan.start or i < 20:
                return
            history = frames[s.signal_minutes][i - 20:i]
            mean = statistics.mean(b.volume for b in history)
            direction = bar.close > s.signal_level if long else bar.close < s.signal_level
            body = True
            if s.key in {"A", "C"}:
                body = bar.close > bar.open if long else bar.close < bar.open
            setup = s.setup_zone is None or (
                st["setup_at"] and bar.end - st["setup_at"] <= HOUR)
            if direction and body and setup and mean > 0 and bar.volume >= mean * s.volume_ratio:
                st.update(phase="ARMED", armed_at=bar.end)
                self.emit(s.key, "ARMED", bar.end, "거래량 포함 1차 신호 확정; 재시험 대기",
                          deadline=iso(min(self.plan.end, bar.end + self.plan.ttl)),
                          volume_ratio=round(bar.volume / mean, 3))
            return
        if st["phase"] == "ARMED":
            retest = lookup[s.retest_minutes].get(c.end)
            if retest is None:
                return
            bar = retest[1]
            if bar.start < st["armed_at"]:
                return
            direction = bar.close > s.confirmation if long else bar.close < s.confirmation
            body = bar.close > bar.open if long else bar.close < bar.open
            if overlaps(bar, s.retest_zone) and direction and body:
                st.update(phase="CONFIRMED", confirmed_at=bar.end)
                self.emit(s.key, "CONFIRMED", bar.end, "재시험 확인; 계획 진입가 근접 대기")
        if st["phase"] == "CONFIRMED":
            if abs(c.close - s.entry) <= self.plan.tolerance:
                if now - c.end > FRESH_MS:
                    self.end(s, "MISSED", c.end, "복구 중 발견한 과거 진입 기회; 소급 알림 안 함")
                else:
                    st["phase"] = "READY"
                    self.emit(s.key, "ENTRY_READY", c.end,
                              "진입 조건 충족 알림 (주문/체결 아님)", price=c.close,
                              entry=s.entry, add=s.add, stop=s.stop, targets=s.targets,
                              margin_pct=[15, 10], leverage=3,
                              deadline=iso(min(self.plan.end, st["armed_at"] + self.plan.ttl)))


def request_json(url: str, data: dict | None = None) -> dict | list:
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers={"Content-Type": "application/json",
                                         "User-Agent": "scenario-monitor/1"})
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def market_snapshot() -> tuple[int, list[Candle]]:
    before = time.monotonic()
    server = int(request_json(BASE_URL + "/fapi/v1/time")["serverTime"])
    query = urlencode({"symbol": "BTCUSDT", "interval": "1m", "limit": 1500})
    rows = request_json(BASE_URL + "/fapi/v1/klines?" + query)
    now = server + int((time.monotonic() - before) * 1000)
    candles = [Candle.parse(row) for row in rows]
    return now, candles


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def notification(event: dict) -> str:
    text = (f'[BTC 시나리오 {event["scenario"]}] {event["kind"]}\n'
            f'{event["reason"]}\nUTC: {event["utc"]}\nID: {event["id"]}')
    if event["kind"] == "ENTRY_READY":
        text += (f'\n확인 종가: {event["price"]:,.1f}'
                 f'\n계획 진입/추가: {event["entry"]:,} / {event["add"]:,}'
                 f'\n손절: {event["stop"]:,}\n익절: {event["targets"]}'
                 '\n증거금: 시드 15% + 최대 10%, 격리 3배'
                 '\n추가는 별도 확인 필요. 실제 보유·체결은 추적하지 않음.')
    if "deadline" in event:
        text += f'\n진입 대기 만료(UTC): {event["deadline"]}'
    return text


def flush_outbox(monitor: Monitor, path: Path, token: str, chat_id: str,
                 now: int, sender=request_json, market_ok: bool = True) -> None:
    while monitor.state["outbox"]:
        event = monitor.state["outbox"][0]
        stale_entry = event["kind"] == "ENTRY_READY" and (
            not market_ok or now - event["at"] > FRESH_MS or
            monitor.state["scenarios"][event["scenario"]]["phase"] != "READY")
        if not stale_entry:
            if token:
                payload = sender(f"https://api.telegram.org/bot{token}/sendMessage",
                                 {"chat_id": chat_id, "text": notification(event)})
                if not payload.get("ok"):
                    raise RuntimeError("Telegram rejected notification")
            else:
                LOG.info("%s", notification(event))
        else:
            LOG.warning("Suppressed obsolete entry alert %s", event["id"])
        monitor.state["outbox"].pop(0)
        save_state(path, monitor.state)


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=os.getenv("SCENARIO_PLAN_PATH"))
    parser.add_argument("--print-default-plan", action="store_true")
    parser.add_argument("--validate", action="store_true", help="Offline config validation")
    parser.add_argument("--once", action="store_true", help="One read-only monitoring cycle")
    args = parser.parse_args()
    if args.print_default_plan:
        print(json.dumps(DEFAULT_PLAN, ensure_ascii=False, indent=2))
        return 0
    raw = json.loads(args.plan.read_text()) if args.plan else DEFAULT_PLAN
    plan = Plan.parse(raw)
    if args.validate:
        print(f'Valid plan {raw["plan_id"]}; expires {iso(plan.end)}')
        return 0
    data_dir = Path(os.getenv("SCENARIO_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
    state_path = data_dir / f"state-{plan.digest[:16]}.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    token = os.getenv("SCENARIO_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("SCENARIO_TELEGRAM_CHAT_ID", "").strip()
    if bool(token) != bool(chat_id):
        raise ValueError("Set both SCENARIO_TELEGRAM_BOT_TOKEN and SCENARIO_TELEGRAM_CHAT_ID")
    poll = float(os.getenv("SCENARIO_POLL_SECONDS", "15"))
    if not math.isfinite(poll) or not 5 <= poll <= 60:
        raise ValueError("SCENARIO_POLL_SECONDS must be between 5 and 60")
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # One instance per data directory, including when the plan is changed.
    with (data_dir / "monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = json.loads(state_path.read_text()) if state_path.exists() else None
        monitor = Monitor(plan, saved)
        LOG.info("Read-only monitor %s; expires %s; state=%s", raw["plan_id"], iso(plan.end), state_path)
        if not saved:
            monitor.emit("ALL", "STARTED", int(time.time() * 1000),
                         "시나리오 감시 시작. 실주문/보유 포지션/뉴스 자동 판단 없음")
        while not stopping:
            now = int(time.time() * 1000)
            monitor.tick(now)  # Absolute deadlines advance even during API outages.
            if (data_dir / "PAUSE").exists():
                monitor.suspend(now, "수동 PAUSE: 뉴스/시장 재평가 후 새 계획 필요")
            active = any(s["phase"] not in TERMINAL for s in monitor.state["scenarios"].values())
            market_ok = False
            if active and now >= plan.start:
                try:
                    market_now, candles = market_snapshot()
                    if abs(market_now - now) > MINUTE:
                        raise ValueError("Local/server clock mismatch")
                    monitor.process(candles, market_now)
                    market_ok = True
                    if monitor.state.pop("network_unavailable", False):
                        monitor.emit("ALL", "DATA_RECOVERED", market_now,
                                     "시장 데이터 연결 복구; 누락 봉 순서대로 확인 완료")
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    LOG.error("Market transport failed; retrying: %s", type(exc).__name__)
                    if not monitor.state.get("network_unavailable"):
                        monitor.state["network_unavailable"] = True
                        monitor.emit("ALL", "DATA_UNAVAILABLE", now,
                                     "시장 연결 오류: 진입 알림 중단, 재연결/기한 만료 감시 계속")
                except Exception as exc:
                    # Never log exception text: HTTP URLs can contain credentials.
                    LOG.error("Market read/validation failed: %s", type(exc).__name__)
                    monitor.suspend(now, "시장 데이터 오류/공백: 진입 신호 중단, 새 계획으로 재평가 필요")
            save_state(state_path, monitor.state)
            try:
                flush_outbox(monitor, state_path, token, chat_id,
                             int(time.time() * 1000), market_ok=market_ok)
            except Exception as exc:
                LOG.error("Notification failed; durable outbox retained: %s", type(exc).__name__)
            if args.once:
                return 0
            for _ in range(int(poll * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        raise SystemExit(run())
    except (ValueError, KeyError, TypeError, OSError) as exc:
        LOG.error("Startup failed: %s", type(exc).__name__)
        raise SystemExit(1) from None
