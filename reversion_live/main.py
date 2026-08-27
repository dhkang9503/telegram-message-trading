"""BTCUSDT futures volume-spike alert and event recorder.

The service consumes Binance USD-M Futures aggregate trades and the live 1m
kline over a combined WebSocket connection. An alert is triggered when either
Binance's current 1m kline base-asset volume or a locally calculated rolling
60-second volume reaches the configured threshold. The rolling window catches
spikes that straddle a fixed candle boundary, while the kline volume keeps the
bot aligned with the Binance futures chart.

No trading API key is required and this module never submits orders.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import signal
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from websockets.asyncio.client import connect

KST = ZoneInfo("Asia/Seoul")
LOG = logging.getLogger("reversion_live")

HORIZONS_MS: tuple[tuple[str, int], ...] = (
    ("1m", 60_000),
    ("5m", 5 * 60_000),
    ("10m", 10 * 60_000),
    ("30m", 30 * 60_000),
    ("1h", 60 * 60_000),
    ("2h", 2 * 60 * 60_000),
    ("4h", 4 * 60 * 60_000),
    ("6h", 6 * 60 * 60_000),
)
FINAL_HORIZON = HORIZONS_MS[-1][0]
STATE_PERSIST_INTERVAL_MS = 15_000
ALERT_COOLDOWN_MS = 60_000
VOLUME_STATUS_INTERVAL_MS = 10_000


@dataclass(frozen=True)
class Config:
    symbol: str
    volume_60s_threshold: float
    # Retained for env/backward compatibility and analysis context; no alert gate.
    volume_10s_threshold: float
    # Retained for env/backward compatibility; cooldown controls re-alerting.
    volume_reset_threshold: float
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_timeout_seconds: float
    websocket_url: str
    data_dir: Path

    @classmethod
    def from_env(cls) -> "Config":
        symbol = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
        volume_60s_threshold = float(os.getenv("VOLUME_60S_THRESHOLD", "1000"))
        volume_10s_threshold = float(os.getenv("VOLUME_10S_THRESHOLD", "300"))
        volume_reset_threshold = float(os.getenv("VOLUME_RESET_THRESHOLD", "600"))
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        telegram_timeout_seconds = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))
        data_dir = Path(
            os.getenv("VOLUME_DATA_DIR", "/home/ubuntu/reversion_live/data")
        ).expanduser()

        if not symbol:
            raise ValueError("SYMBOL must not be empty")
        if volume_60s_threshold <= 0:
            raise ValueError("VOLUME_60S_THRESHOLD must be positive")
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
            data_dir=data_dir,
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
    """Trigger when fixed-1m or rolling-60s volume reaches the threshold."""

    def __init__(
        self,
        threshold: float,
        cooldown_ms: int = ALERT_COOLDOWN_MS,
    ) -> None:
        self.threshold = threshold
        self.cooldown_ms = cooldown_ms
        self.last_alert_timestamp_ms: int | None = None

    def reset(self) -> None:
        self.last_alert_timestamp_ms = None

    def evaluate(
        self,
        timestamp_ms: int,
        rolling_60s_volume: float,
        kline_1m_volume: float,
        allow_trigger: bool = True,
    ) -> str | None:
        if not allow_trigger:
            return None
        if max(rolling_60s_volume, kline_1m_volume) < self.threshold:
            return None
        if (
            self.last_alert_timestamp_ms is not None
            and timestamp_ms - self.last_alert_timestamp_ms < self.cooldown_ms
        ):
            return None

        self.last_alert_timestamp_ms = timestamp_ms
        rolling_hit = rolling_60s_volume >= self.threshold
        kline_hit = kline_1m_volume >= self.threshold
        if rolling_hit and kline_hit:
            return "both"
        return "rolling60" if rolling_hit else "kline1m"


@dataclass
class CandleState:
    open_time_ms: int | None = None
    open_price: float | None = None
    volume_1m: float = 0.0

    def reset(self) -> None:
        self.open_time_ms = None
        self.open_price = None
        self.volume_1m = 0.0

    def update(self, kline: dict[str, Any]) -> None:
        self.open_time_ms = int(kline["t"])
        self.open_price = float(kline["o"])
        self.volume_1m = float(kline.get("v", 0.0))

    def is_current_for(self, trade: Trade) -> bool:
        expected_open_ms = trade.timestamp_ms // 60_000 * 60_000
        return self.open_time_ms == expected_open_ms and self.open_price is not None

    def direction_emoji(self, trade: Trade) -> str | None:
        if not self.is_current_for(trade):
            return None
        assert self.open_price is not None
        return "🔵" if trade.price >= self.open_price else "🔴"

    def volume_for_trade(self, trade: Trade) -> float:
        return self.volume_1m if self.is_current_for(trade) else 0.0


def iso_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def market_return_pct(trigger_price: float, price: float) -> float:
    return (price / trigger_price - 1.0) * 100.0


@dataclass
class ActiveEvent:
    event_id: str
    symbol: str
    trigger_time_ms: int
    trigger_price: float
    volume_10s: float
    volume_60s: float
    volume_10s_ratio: float
    price_change_60s_usd: float
    price_change_60s_pct: float
    candle_direction: str
    candle_open: float
    max_price: float
    max_price_time_ms: int
    min_price: float
    min_price_time_ms: int
    tracking_gap: bool = False
    results: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        symbol: str,
        trade: Trade,
        snapshot: VolumeSnapshot,
        candle_direction: str,
        candle_open: float,
    ) -> "ActiveEvent":
        price_change = snapshot.price_change
        price_change_pct = snapshot.price_change_pct
        if price_change is None or price_change_pct is None:
            raise ValueError("cannot record event before rolling window is ready")
        ratio = snapshot.volume_10s / snapshot.volume_60s if snapshot.volume_60s else 0.0
        return cls(
            event_id=f"{symbol}-{trade.timestamp_ms}-{trade.agg_trade_id}",
            symbol=symbol,
            trigger_time_ms=trade.timestamp_ms,
            trigger_price=trade.price,
            volume_10s=snapshot.volume_10s,
            volume_60s=snapshot.volume_60s,
            volume_10s_ratio=ratio,
            price_change_60s_usd=price_change,
            price_change_60s_pct=price_change_pct,
            candle_direction="BLUE" if candle_direction == "🔵" else "RED",
            candle_open=candle_open,
            max_price=trade.price,
            max_price_time_ms=trade.timestamp_ms,
            min_price=trade.price,
            min_price_time_ms=trade.timestamp_ms,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActiveEvent":
        payload = dict(payload)
        payload["results"] = {
            str(name): {str(k): float(v) for k, v in values.items()}
            for name, values in dict(payload.get("results", {})).items()
        }
        payload["tracking_gap"] = True
        return cls(**payload)

    @property
    def complete(self) -> bool:
        return FINAL_HORIZON in self.results

    @property
    def contrarian_sign(self) -> float:
        return -1.0 if self.candle_direction == "BLUE" else 1.0

    def update(self, trade: Trade) -> bool:
        if trade.timestamp_ms < self.trigger_time_ms:
            return False

        changed = False
        if trade.price > self.max_price:
            self.max_price = trade.price
            self.max_price_time_ms = trade.timestamp_ms
            changed = True
        if trade.price < self.min_price:
            self.min_price = trade.price
            self.min_price_time_ms = trade.timestamp_ms
            changed = True

        for name, duration_ms in HORIZONS_MS:
            if name in self.results:
                continue
            if trade.timestamp_ms < self.trigger_time_ms + duration_ms:
                break

            ret = market_return_pct(self.trigger_price, trade.price)
            mfe = market_return_pct(self.trigger_price, self.max_price)
            mae = market_return_pct(self.trigger_price, self.min_price)
            self.results[name] = {
                "price": trade.price,
                "return_pct": ret,
                "contrarian_return_pct": ret * self.contrarian_sign,
                "mfe_pct": mfe,
                "mae_pct": mae,
                "time_to_mfe_sec": (self.max_price_time_ms - self.trigger_time_ms) / 1000.0,
                "time_to_mae_sec": (self.min_price_time_ms - self.trigger_time_ms) / 1000.0,
            }
            changed = True

        return changed


RAW_FIELDS = (
    "event_id",
    "symbol",
    "trigger_time_utc",
    "trigger_time_kst",
    "hour_kst",
    "weekday_kst",
    "trigger_price",
    "volume_10s",
    "volume_60s",
    "volume_10s_ratio",
    "price_change_60s_usd",
    "price_change_60s_pct",
    "candle_direction",
    "candle_open",
)

COMPLETED_FIELDS = list(RAW_FIELDS) + ["tracking_gap"]
for _name, _ in HORIZONS_MS:
    COMPLETED_FIELDS.extend(
        (
            f"price_{_name}",
            f"return_{_name}_pct",
            f"contrarian_return_{_name}_pct",
            f"mfe_{_name}_pct",
            f"mae_{_name}_pct",
            f"time_to_mfe_{_name}_sec",
            f"time_to_mae_{_name}_sec",
        )
    )


class EventRecorder:
    """Persist volume-spike events and follow their price path for six hours."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.raw_path = data_dir / "volume_events_raw.csv"
        self.completed_path = data_dir / "volume_events_completed.csv"
        self.state_path = data_dir / "active_events.json"
        self.active: dict[str, ActiveEvent] = {}
        self.last_persist_trade_ms: int | None = None

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            for item in payload.get("events", []):
                event = ActiveEvent.from_dict(item)
                if not event.complete:
                    self.active[event.event_id] = event
            if self.active:
                LOG.warning(
                    "restored %s active event(s); marked tracking_gap=1 because of restart",
                    len(self.active),
                )
        except Exception:
            LOG.exception("failed to load active event state; starting with empty state")
            self.active = {}

    @staticmethod
    def _append_csv(
        path: Path,
        fieldnames: list[str] | tuple[str, ...],
        row: dict[str, Any],
    ) -> None:
        needs_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())

    def _write_state(self) -> None:
        payload = {"version": 1, "events": [asdict(event) for event in self.active.values()]}
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    def persist_state(
        self,
        force: bool = False,
        trade_timestamp_ms: int | None = None,
    ) -> None:
        if not force and trade_timestamp_ms is not None:
            if (
                self.last_persist_trade_ms is not None
                and trade_timestamp_ms - self.last_persist_trade_ms
                < STATE_PERSIST_INTERVAL_MS
            ):
                return
            self.last_persist_trade_ms = trade_timestamp_ms
        self._write_state()

    @staticmethod
    def _base_row(event: ActiveEvent) -> dict[str, Any]:
        dt_kst = datetime.fromtimestamp(event.trigger_time_ms / 1000, tz=KST)
        return {
            "event_id": event.event_id,
            "symbol": event.symbol,
            "trigger_time_utc": iso_utc(event.trigger_time_ms),
            "trigger_time_kst": dt_kst.isoformat(),
            "hour_kst": dt_kst.hour,
            "weekday_kst": dt_kst.strftime("%a"),
            "trigger_price": event.trigger_price,
            "volume_10s": event.volume_10s,
            "volume_60s": event.volume_60s,
            "volume_10s_ratio": event.volume_10s_ratio,
            "price_change_60s_usd": event.price_change_60s_usd,
            "price_change_60s_pct": event.price_change_60s_pct,
            "candle_direction": event.candle_direction,
            "candle_open": event.candle_open,
        }

    def record_event(
        self,
        symbol: str,
        trade: Trade,
        snapshot: VolumeSnapshot,
        candle_direction: str,
        candle_open: float,
    ) -> ActiveEvent:
        event = ActiveEvent.create(
            symbol,
            trade,
            snapshot,
            candle_direction,
            candle_open,
        )
        self.active[event.event_id] = event
        self._append_csv(self.raw_path, RAW_FIELDS, self._base_row(event))
        self.persist_state(force=True)
        LOG.info(
            "recorded volume event id=%s active_events=%s",
            event.event_id,
            len(self.active),
        )
        return event

    def _completed_row(self, event: ActiveEvent) -> dict[str, Any]:
        row = self._base_row(event)
        row["tracking_gap"] = int(event.tracking_gap)
        for name, _ in HORIZONS_MS:
            result = event.results[name]
            row[f"price_{name}"] = result["price"]
            row[f"return_{name}_pct"] = result["return_pct"]
            row[f"contrarian_return_{name}_pct"] = result["contrarian_return_pct"]
            row[f"mfe_{name}_pct"] = result["mfe_pct"]
            row[f"mae_{name}_pct"] = result["mae_pct"]
            row[f"time_to_mfe_{name}_sec"] = result["time_to_mfe_sec"]
            row[f"time_to_mae_{name}_sec"] = result["time_to_mae_sec"]
        return row

    def update_trade(self, trade: Trade) -> None:
        if not self.active:
            return

        changed = False
        completed_ids: list[str] = []
        for event in list(self.active.values()):
            if event.update(trade):
                changed = True
            if event.complete:
                self._append_csv(
                    self.completed_path,
                    COMPLETED_FIELDS,
                    self._completed_row(event),
                )
                completed_ids.append(event.event_id)
                LOG.info(
                    "completed event id=%s tracking_gap=%s",
                    event.event_id,
                    int(event.tracking_gap),
                )

        for event_id in completed_ids:
            self.active.pop(event_id, None)

        if completed_ids:
            self.persist_state(force=True)
        elif changed:
            self.persist_state(trade_timestamp_ms=trade.timestamp_ms)


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


def build_startup_message(config: Config) -> str:
    return (
        f"✅ {display_symbol(config.symbol)} 거래량 알림봇 시작\n"
        f"1m/60s: {format_volume_btc(config.volume_60s_threshold)} | "
        f"cooldown: {ALERT_COOLDOWN_MS // 1000}s"
    )


def build_alert_message(
    symbol: str,
    emoji: str,
    snapshot: VolumeSnapshot,
    kline_1m_volume: float,
) -> str:
    when = datetime.fromtimestamp(
        snapshot.timestamp_ms / 1000,
        tz=KST,
    ).strftime("%H:%M:%S KST")
    volumes = (
        f"1m {format_volume_btc(kline_1m_volume)} | "
        f"60s {format_volume_btc(snapshot.volume_60s)}"
    )
    if snapshot.price_change is None or snapshot.price_change_pct is None:
        change_text = "60s Δ warming up"
    else:
        change_text = (
            f"{format_signed_usd(snapshot.price_change)} "
            f"({snapshot.price_change_pct:+.2f}%)"
        )
    return (
        f"{emoji} {display_symbol(symbol)} 거래량 급증\n"
        f"{when} | {volumes} | ${snapshot.current_price:,.0f} | {change_text}"
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
        self.detector = SpikeDetector(config.volume_60s_threshold)
        self.candle = CandleState()
        self.recorder = EventRecorder(config.data_dir)
        self.notification_tasks: set[asyncio.Task[None]] = set()
        self.warmup_logged = False
        self.last_status_log_ms: int | None = None

    def reset_stream_state(self) -> None:
        self.tracker.reset()
        self.detector.reset()
        self.candle.reset()
        self.warmup_logged = False
        self.last_status_log_ms = None

    def _schedule_notification(self, text: str) -> None:
        task = asyncio.create_task(self._send_notification(text))
        self.notification_tasks.add(task)
        task.add_done_callback(self.notification_tasks.discard)

    async def _send_notification(self, text: str) -> None:
        try:
            await asyncio.to_thread(send_telegram_sync, self.config, text)
            LOG.info("telegram notification sent: %s", text.replace("\n", " | "))
        except Exception:
            LOG.exception("failed to send Telegram notification")

    def _log_volume_status(
        self,
        trade: Trade,
        snapshot: VolumeSnapshot,
        kline_1m_volume: float,
    ) -> None:
        if (
            self.last_status_log_ms is not None
            and trade.timestamp_ms - self.last_status_log_ms < VOLUME_STATUS_INTERVAL_MS
        ):
            return
        self.last_status_log_ms = trade.timestamp_ms
        LOG.info(
            "VOLUME_STATUS kline_1m=%.3f rolling60=%.3f rolling10=%.3f ready=%s",
            kline_1m_volume,
            snapshot.volume_60s,
            snapshot.volume_10s,
            int(snapshot.ready),
        )

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

        try:
            self.recorder.update_trade(trade)
        except Exception:
            LOG.exception("failed to update active volume-event tracking")

        if snapshot.ready and not self.warmup_logged:
            LOG.info("rolling 60s window warmed up; full event recording active")
            self.warmup_logged = True

        emoji = self.candle.direction_emoji(trade)
        kline_1m_volume = self.candle.volume_for_trade(trade)
        self._log_volume_status(trade, snapshot, kline_1m_volume)

        trigger_source = self.detector.evaluate(
            trade.timestamp_ms,
            snapshot.volume_60s,
            kline_1m_volume,
            allow_trigger=emoji is not None,
        )
        if trigger_source is None:
            return

        assert emoji is not None
        assert self.candle.open_price is not None
        message = build_alert_message(
            self.config.symbol,
            emoji,
            snapshot,
            kline_1m_volume,
        )
        LOG.warning(
            "VOLUME_SPIKE source=%s kline_1m=%.3f rolling60=%.3f rolling10=%.3f "
            "price=%.2f change=%s pct=%s",
            trigger_source,
            kline_1m_volume,
            snapshot.volume_60s,
            snapshot.volume_10s,
            snapshot.current_price,
            "n/a" if snapshot.price_change is None else f"{snapshot.price_change:.2f}",
            "n/a" if snapshot.price_change_pct is None else f"{snapshot.price_change_pct:.4f}",
        )

        if snapshot.ready:
            try:
                self.recorder.record_event(
                    self.config.symbol,
                    trade,
                    snapshot,
                    emoji,
                    self.candle.open_price,
                )
            except Exception:
                LOG.exception("failed to record volume event")
        else:
            LOG.warning(
                "volume alert fired during rolling-window warmup; CSV event recording skipped"
            )
        self._schedule_notification(message)

    async def run(self, stop_event: asyncio.Event) -> None:
        reconnect_delay = 1.0
        startup_notification_scheduled = False

        try:
            while not stop_event.is_set():
                self.reset_stream_state()
                try:
                    LOG.info(
                        "connecting Binance Futures WebSocket: %s",
                        self.config.websocket_url,
                    )
                    async with connect(
                        self.config.websocket_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=10,
                        max_queue=2048,
                    ) as websocket:
                        LOG.info(
                            "Binance Futures WebSocket connected; warming up rolling window"
                        )
                        reconnect_delay = 1.0
                        if not startup_notification_scheduled:
                            self._schedule_notification(build_startup_message(self.config))
                            startup_notification_scheduled = True

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
        finally:
            try:
                self.recorder.persist_state(force=True)
            except Exception:
                LOG.exception("failed to persist active event state during shutdown")
            if self.notification_tasks:
                await asyncio.gather(*self.notification_tasks, return_exceptions=True)


async def async_main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOG.info(
        "started symbol=%s volume_threshold=%s sources=kline1m,rolling60 "
        "alert_cooldown=%ss data_dir=%s",
        config.symbol,
        config.volume_60s_threshold,
        ALERT_COOLDOWN_MS // 1000,
        config.data_dir,
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
