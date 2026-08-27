"""BTCUSDT futures rolling-volume spike alert bot.

The service consumes Binance USD-M Futures aggregate trades and the live 1m
kline over a combined WebSocket connection. It detects short, intense volume
spikes using rolling 10s/60s windows and sends one compact Telegram alert per
spike. No trading API key is required and this module never submits orders.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from websockets.asyncio.client import connect

KST = ZoneInfo("Asia/Seoul")
LOG = logging.getLogger("reversion_live")


@dataclass(frozen=True)
class Config:
    symbol: str
    volume_60s_threshold: float
    volume_10s_threshold: float
    volume_reset_threshold: float
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_timeout_seconds: float
    websocket_url: str

    @classmethod
    def from_env(cls) -> "Config":
        symbol = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
        volume_60s_threshold = float(os.getenv("VOLUME_60S_THRESHOLD", "1000"))
        volume_10s_threshold = float(os.getenv("VOLUME_10S_THRESHOLD", "300"))
        volume_reset_threshold = float(os.getenv("VOLUME_RESET_THRESHOLD", "600"))
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        telegram_timeout_seconds = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))

        if not symbol:
            raise ValueError("SYMBOL must not be empty")
        if volume_60s_threshold <= 0 or volume_10s_threshold <= 0:
            raise ValueError("volume thresholds must be positive")
        if not 0 <= volume_reset_threshold < volume_60s_threshold:
            raise ValueError(
                "VOLUME_RESET_THRESHOLD must be >= 0 and lower than VOLUME_60S_THRESHOLD"
            )
        if telegram_timeout_seconds <= 0:
            raise ValueError("TELEGRAM_TIMEOUT_SECONDS must be positive")
        if not telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not telegram_chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is required")

        stream_symbol = symbol.lower()
        default_url = (
            "wss://fstream.binance.com/stream?streams="
            f"{stream_symbol}@aggTrade/{stream_symbol}@kline_1m"
        )
        websocket_url = os.getenv("BINANCE_FUTURES_WS_URL", default_url).strip()
        if not websocket_url:
            raise ValueError("BINANCE_FUTURES_WS_URL must not be empty")

        return cls(
            symbol=symbol,
            volume_60s_threshold=volume_60s_threshold,
            volume_10s_threshold=volume_10s_threshold,
            volume_reset_threshold=volume_reset_threshold,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            telegram_timeout_seconds=telegram_timeout_seconds,
            websocket_url=websocket_url,
        )


@dataclass(frozen=True)
class Trade:
    timestamp_ms: int
    price: float
    quantity: float
    agg_trade_id: int


@dataclass(frozen=True)
class VolumeSnapshot:
    timestamp_ms: int
    current_price: float
    volume_10s: float
    volume_60s: float
    price_60s_ago: float | None

    @property
    def ready(self) -> bool:
        return self.price_60s_ago is not None

    @property
    def price_change(self) -> float | None:
        if self.price_60s_ago is None:
            return None
        return self.current_price - self.price_60s_ago

    @property
    def price_change_pct(self) -> float | None:
        if self.price_60s_ago is None or self.price_60s_ago == 0:
            return None
        return (self.current_price / self.price_60s_ago - 1.0) * 100.0


class RollingVolumeTracker:
    """Maintain O(1)-amortized 10s/60s volume windows from aggregate trades."""

    def __init__(self) -> None:
        self.trades_10s: deque[Trade] = deque()
        self.trades_60s: deque[Trade] = deque()
        self.volume_10s = 0.0
        self.volume_60s = 0.0
        self.price_60s_ago: float | None = None
        self.last_timestamp_ms: int | None = None
        self.last_agg_trade_id: int | None = None

    def reset(self) -> None:
        self.trades_10s.clear()
        self.trades_60s.clear()
        self.volume_10s = 0.0
        self.volume_60s = 0.0
        self.price_60s_ago = None
        self.last_timestamp_ms = None
        self.last_agg_trade_id = None

    def add(self, trade: Trade) -> VolumeSnapshot | None:
        if self.last_agg_trade_id is not None and trade.agg_trade_id <= self.last_agg_trade_id:
            return None
        if self.last_timestamp_ms is not None and trade.timestamp_ms < self.last_timestamp_ms:
            LOG.warning(
                "ignoring out-of-order aggTrade id=%s timestamp=%s last_timestamp=%s",
                trade.agg_trade_id,
                trade.timestamp_ms,
                self.last_timestamp_ms,
            )
            return None

        self.last_agg_trade_id = trade.agg_trade_id
        self.last_timestamp_ms = trade.timestamp_ms
        self.trades_10s.append(trade)
        self.trades_60s.append(trade)
        self.volume_10s += trade.quantity
        self.volume_60s += trade.quantity

        cutoff_10s = trade.timestamp_ms - 10_000
        while self.trades_10s and self.trades_10s[0].timestamp_ms < cutoff_10s:
            expired = self.trades_10s.popleft()
            self.volume_10s -= expired.quantity

        cutoff_60s = trade.timestamp_ms - 60_000
        while self.trades_60s and self.trades_60s[0].timestamp_ms < cutoff_60s:
            expired = self.trades_60s.popleft()
            self.volume_60s -= expired.quantity
            self.price_60s_ago = expired.price

        if abs(self.volume_10s) < 1e-12:
            self.volume_10s = 0.0
        if abs(self.volume_60s) < 1e-12:
            self.volume_60s = 0.0

        return VolumeSnapshot(
            timestamp_ms=trade.timestamp_ms,
            current_price=trade.price,
            volume_10s=self.volume_10s,
            volume_60s=self.volume_60s,
            price_60s_ago=self.price_60s_ago,
        )


class SpikeDetector:
    def __init__(self, threshold_60s: float, threshold_10s: float, reset_threshold: float):
        self.threshold_60s = threshold_60s
        self.threshold_10s = threshold_10s
        self.reset_threshold = reset_threshold
        self.alerted = False

    def reset(self) -> None:
        self.alerted = False

    def evaluate(self, snapshot: VolumeSnapshot, allow_trigger: bool = True) -> bool:
        if self.alerted:
            if snapshot.volume_60s < self.reset_threshold:
                self.alerted = False
            return False

        if not snapshot.ready or not allow_trigger:
            return False

        if (
            snapshot.volume_60s >= self.threshold_60s
            and snapshot.volume_10s >= self.threshold_10s
        ):
            self.alerted = True
            return True
        return False


@dataclass
class CandleState:
    open_time_ms: int | None = None
    open_price: float | None = None

    def reset(self) -> None:
        self.open_time_ms = None
        self.open_price = None

    def update(self, kline: dict[str, Any]) -> None:
        self.open_time_ms = int(kline["t"])
        self.open_price = float(kline["o"])

    def direction_emoji(self, trade: Trade) -> str | None:
        expected_open_ms = trade.timestamp_ms // 60_000 * 60_000
        if self.open_time_ms != expected_open_ms or self.open_price is None:
            return None
        return "🔵" if trade.price >= self.open_price else "🔴"


def display_symbol(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") and len(symbol) > 4 else symbol


def format_volume_btc(volume: float) -> str:
    if volume >= 1000:
        text = f"{volume / 1000:.2f}".rstrip("0").rstrip(".")
        return f"{text}K BTC"
    if volume >= 100:
        return f"{volume:.0f} BTC"
    return f"{volume:.2f}".rstrip("0").rstrip(".") + " BTC"


def format_signed_usd(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def build_alert_message(symbol: str, emoji: str, snapshot: VolumeSnapshot) -> str:
    price_change = snapshot.price_change
    price_change_pct = snapshot.price_change_pct
    if price_change is None or price_change_pct is None:
        raise ValueError("snapshot is not warmed up for a 60-second price change")

    when = datetime.fromtimestamp(snapshot.timestamp_ms / 1000, tz=KST).strftime("%H:%M:%S KST")
    return (
        f"{emoji} {display_symbol(symbol)} 거래량 급증\n"
        f"{when} | {format_volume_btc(snapshot.volume_60s)} | "
        f"${snapshot.current_price:,.0f} | {format_signed_usd(price_change)} "
        f"({price_change_pct:+.2f}%)"
    )


def send_telegram_sync(config: Config, text: str) -> None:
    endpoint = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    body = urlencode({"chat_id": config.telegram_chat_id, "text": text}).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=config.telegram_timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw) if raw else {}
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {payload}")


class VolumeSpikeBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tracker = RollingVolumeTracker()
        self.detector = SpikeDetector(
            config.volume_60s_threshold,
            config.volume_10s_threshold,
            config.volume_reset_threshold,
        )
        self.candle = CandleState()
        self.notification_tasks: set[asyncio.Task[None]] = set()
        self.warmup_logged = False

    def reset_stream_state(self) -> None:
        self.tracker.reset()
        self.detector.reset()
        self.candle.reset()
        self.warmup_logged = False

    def _schedule_notification(self, text: str) -> None:
        task = asyncio.create_task(self._send_notification(text))
        self.notification_tasks.add(task)
        task.add_done_callback(self.notification_tasks.discard)

    async def _send_notification(self, text: str) -> None:
        try:
            await asyncio.to_thread(send_telegram_sync, self.config, text)
            LOG.info("telegram alert sent: %s", text.replace("\n", " | "))
        except Exception:
            LOG.exception("failed to send Telegram alert")

    async def process_message(self, raw: str | bytes) -> None:
        payload = json.loads(raw)
        data = payload.get("data", payload)
        event_type = data.get("e")

        if event_type == "kline":
            kline = data.get("k")
            if isinstance(kline, dict):
                self.candle.update(kline)
            return

        if event_type != "aggTrade":
            return

        trade = Trade(
            timestamp_ms=int(data["T"]),
            price=float(data["p"]),
            quantity=float(data["q"]),
            agg_trade_id=int(data["a"]),
        )
        snapshot = self.tracker.add(trade)
        if snapshot is None:
            return

        if snapshot.ready and not self.warmup_logged:
            LOG.info("rolling 60s window warmed up; spike detection active")
            self.warmup_logged = True

        emoji = self.candle.direction_emoji(trade)
        if not self.detector.evaluate(snapshot, allow_trigger=emoji is not None):
            return

        assert emoji is not None
        message = build_alert_message(self.config.symbol, emoji, snapshot)
        LOG.warning(
            "VOLUME_SPIKE volume_60s=%.3f volume_10s=%.3f price=%.2f change=%.2f pct=%.4f",
            snapshot.volume_60s,
            snapshot.volume_10s,
            snapshot.current_price,
            snapshot.price_change,
            snapshot.price_change_pct,
        )
        self._schedule_notification(message)

    async def run(self, stop_event: asyncio.Event) -> None:
        reconnect_delay = 1.0
        while not stop_event.is_set():
            self.reset_stream_state()
            try:
                LOG.info("connecting Binance Futures WebSocket: %s", self.config.websocket_url)
                async with connect(
                    self.config.websocket_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_queue=2048,
                ) as websocket:
                    LOG.info("Binance Futures WebSocket connected; warming up rolling window")
                    reconnect_delay = 1.0
                    async for raw in websocket:
                        if stop_event.is_set():
                            break
                        await self.process_message(raw)
            except Exception:
                if stop_event.is_set():
                    break
                LOG.exception(
                    "Binance WebSocket disconnected; reconnecting in %.0fs",
                    reconnect_delay,
                )

            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=reconnect_delay)
            except TimeoutError:
                pass
            reconnect_delay = min(reconnect_delay * 2.0, 30.0)

        if self.notification_tasks:
            await asyncio.gather(*self.notification_tasks, return_exceptions=True)


async def async_main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOG.info(
        "started symbol=%s volume_60s_threshold=%s volume_10s_threshold=%s reset=%s",
        config.symbol,
        config.volume_60s_threshold,
        config.volume_10s_threshold,
        config.volume_reset_threshold,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await VolumeSpikeBot(config).run(stop_event)
    LOG.info("stopped")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
