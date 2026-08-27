import csv
import importlib.util
import json
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


def test_spike_uses_60s_volume_only_and_realerts_after_cooldown():
    detector = rv.SpikeDetector(1000.0)
    first = rv.VolumeSnapshot(60_001, 110.0, 50.0, 1050.0, 100.0)
    before_cooldown = rv.VolumeSnapshot(119_999, 111.0, 40.0, 1400.0, 100.0)
    after_cooldown = rv.VolumeSnapshot(120_001, 112.0, 20.0, 1300.0, 100.0)

    assert detector.evaluate(first) is True
    assert detector.evaluate(before_cooldown) is False
    assert detector.evaluate(after_cooldown) is True


def test_spike_does_not_trigger_below_60s_threshold():
    detector = rv.SpikeDetector(1000.0)
    snapshot = rv.VolumeSnapshot(60_001, 110.0, 500.0, 999.9, 100.0)
    assert detector.evaluate(snapshot) is False


def test_current_1m_candle_controls_blue_or_red_marker():
    candle = rv.CandleState()
    candle.update({"t": 60_000, "o": "105"})

    assert candle.direction_emoji(trade(60_100, 106.0, 1.0, 1)) == "🔵"
    assert candle.direction_emoji(trade(60_200, 104.0, 1.0, 2)) == "🔴"
    assert candle.direction_emoji(trade(120_100, 106.0, 1.0, 3)) is None


def test_startup_message_shows_60s_threshold_and_cooldown(tmp_path):
    config = rv.Config(
        symbol="BTCUSDT",
        volume_60s_threshold=1000.0,
        volume_10s_threshold=300.0,
        volume_reset_threshold=600.0,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        telegram_timeout_seconds=10.0,
        websocket_url="wss://example.test",
        data_dir=tmp_path,
    )

    assert rv.build_startup_message(config) == (
        "✅ BTC 거래량 알림봇 시작\n"
        "60s: 1K BTC | cooldown: 60s"
    )


def test_default_data_dir_uses_reversion_live_home(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("VOLUME_DATA_DIR", raising=False)

    config = rv.Config.from_env()

    assert config.data_dir == Path("/home/ubuntu/reversion_live/data")


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


def make_event(recorder):
    trigger = trade(1_000_000, 100.0, 1.0, 42)
    snapshot = rv.VolumeSnapshot(
        timestamp_ms=trigger.timestamp_ms,
        current_price=trigger.price,
        volume_10s=400.0,
        volume_60s=1200.0,
        price_60s_ago=99.0,
    )
    return recorder.record_event("BTCUSDT", trigger, snapshot, "🔵", 98.0)


def test_event_is_written_immediately_and_tracks_horizon_mfe_mae(tmp_path):
    recorder = rv.EventRecorder(tmp_path)
    event = make_event(recorder)

    with recorder.raw_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["event_id"] == event.event_id
    assert rows[0]["candle_direction"] == "BLUE"

    recorder.update_trade(trade(1_020_000, 103.0, 1.0, 43))
    recorder.update_trade(trade(1_040_000, 97.0, 1.0, 44))
    recorder.update_trade(trade(1_060_000, 99.0, 1.0, 45))

    result = event.results["1m"]
    assert round(result["return_pct"], 6) == -1.0
    assert round(result["contrarian_return_pct"], 6) == 1.0
    assert round(result["mfe_pct"], 6) == 3.0
    assert round(result["mae_pct"], 6) == -3.0
    assert result["time_to_mfe_sec"] == 20.0
    assert result["time_to_mae_sec"] == 40.0


def test_event_completes_at_six_hours_and_writes_completed_csv(tmp_path):
    recorder = rv.EventRecorder(tmp_path)
    event = make_event(recorder)

    next_id = 43
    for _, duration_ms in rv.HORIZONS_MS:
        recorder.update_trade(
            trade(event.trigger_time_ms + duration_ms, 101.0, 1.0, next_id)
        )
        next_id += 1

    assert event.event_id not in recorder.active
    with recorder.completed_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["tracking_gap"] == "0"
    assert round(float(rows[0]["return_6h_pct"]), 6) == 1.0
    assert round(float(rows[0]["contrarian_return_6h_pct"]), 6) == -1.0


def test_restart_restores_active_event_and_marks_tracking_gap(tmp_path):
    recorder = rv.EventRecorder(tmp_path)
    event = make_event(recorder)
    recorder.update_trade(trade(event.trigger_time_ms + 60_000, 99.0, 1.0, 43))
    recorder.persist_state(force=True)

    restored = rv.EventRecorder(tmp_path)

    assert event.event_id in restored.active
    assert restored.active[event.event_id].tracking_gap is True
    state = json.loads(restored.state_path.read_text(encoding="utf-8"))
    assert state["version"] == 1
