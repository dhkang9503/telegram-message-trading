import importlib.util
import sys
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "reversion_live" / "main.py"
    spec = importlib.util.spec_from_file_location("reversion_volume_alert_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rv = load_module()


def trade(timestamp_ms, price, quantity, agg_trade_id):
    return rv.Trade(timestamp_ms, price, quantity, agg_trade_id)


def test_rolling_windows_cross_minute_boundaries_and_keep_60s_price_anchor():
    tracker = rv.RollingVolumeTracker()

    assert tracker.add(trade(0, 100.0, 100.0, 1)).ready is False
    tracker.add(trade(30_000, 102.0, 400.0, 2))
    tracker.add(trade(59_000, 108.0, 300.0, 3))
    snapshot = tracker.add(trade(60_001, 110.0, 400.0, 4))

    assert snapshot is not None
    assert snapshot.ready is True
    assert snapshot.volume_60s == 1100.0
    assert snapshot.volume_10s == 700.0
    assert snapshot.price_60s_ago == 100.0
    assert snapshot.price_change == 10.0
    assert round(snapshot.price_change_pct, 6) == 10.0


def test_spike_alerts_once_until_60s_volume_falls_below_reset_threshold():
    detector = rv.SpikeDetector(1000.0, 300.0, 600.0)
    spike = rv.VolumeSnapshot(60_001, 110.0, 350.0, 1050.0, 100.0)

    assert detector.evaluate(spike) is True
    assert detector.evaluate(spike) is False
    assert detector.alerted is True

    cooling = rv.VolumeSnapshot(70_000, 109.0, 20.0, 599.0, 100.0)
    assert detector.evaluate(cooling) is False
    assert detector.alerted is False
    assert detector.evaluate(spike) is True


def test_current_1m_candle_controls_blue_or_red_marker():
    candle = rv.CandleState()
    candle.update({"t": 60_000, "o": "105"})

    assert candle.direction_emoji(trade(60_100, 106.0, 1.0, 1)) == "🔵"
    assert candle.direction_emoji(trade(60_200, 104.0, 1.0, 2)) == "🔴"
    assert candle.direction_emoji(trade(120_100, 106.0, 1.0, 3)) is None


def test_startup_message_shows_active_thresholds():
    config = rv.Config(
        symbol="BTCUSDT",
        volume_60s_threshold=1000.0,
        volume_10s_threshold=300.0,
        volume_reset_threshold=600.0,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        telegram_timeout_seconds=10.0,
        websocket_url="wss://example.test",
    )

    assert rv.build_startup_message(config) == (
        "✅ BTC 거래량 알림봇 시작\n"
        "60s: 1K BTC | 10s: 300 BTC"
    )


def test_alert_message_contains_current_price_and_60s_dollar_and_percent_change():
    snapshot = rv.VolumeSnapshot(
        timestamp_ms=60_001,
        current_price=63_420.0,
        volume_10s=350.0,
        volume_60s=1080.0,
        price_60s_ago=63_120.0,
    )

    message = rv.build_alert_message("BTCUSDT", "🔵", snapshot)

    assert message.startswith("🔵 BTC 거래량 급증\n")
    assert "1.08K BTC" in message
    assert "$63,420" in message
    assert "+$300" in message
    assert "(+0.48%)" in message
    assert message.endswith("(+0.48%)")


def test_duplicate_or_out_of_order_aggregate_trades_are_ignored():
    tracker = rv.RollingVolumeTracker()
    assert tracker.add(trade(1_000, 100.0, 1.0, 10)) is not None
    assert tracker.add(trade(2_000, 101.0, 1.0, 10)) is None
    assert tracker.add(trade(900, 99.0, 1.0, 11)) is None
