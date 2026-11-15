"""稼動率 view：用手算好的合成資料驗證數字完全一致。不需要 broker 或 docker。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import payload_schema
from ingest import INSERTED, Ingestor

EQ = "ROBOT01"
HOUR = 3600.0
COLUMNS = (
    "day, productive_s, standby_s, unscheduled_down_s, offline_s, unknown_s, total_s, denominator_s, availability"
)


class Feed:
    """依序餵訊息；時間用「距離起點幾小時」表示。"""

    def __init__(self, ingestor: Ingestor, start: datetime) -> None:
        self.ingestor = ingestor
        self.start = start
        self.seq = 0
        self.state: str | None = None

    def _send(self, message_type: str, hours: float, body: dict) -> None:
        payload = {
            "schema_version": 1,
            "equipment_id": EQ,
            "seq": self.seq,
            "ts": payload_schema.format_ts(self.start + timedelta(hours=hours)),
            **body,
        }
        topic = payload_schema.topic_for("test", EQ, message_type)
        assert self.ingestor.handle(topic, json.dumps(payload).encode()) == INSERTED
        self.seq += 1

    def state_at(self, hours: float, to: str) -> None:
        self._send("state", hours, {"from": self.state, "to": to, "heartbeat": False})
        self.state = to

    def heartbeat_at(self, hours: float) -> None:
        self._send("state", hours, {"from": self.state, "to": self.state, "heartbeat": True})

    def telemetry_at(self, hours: float) -> None:
        self._send("telemetry", hours, {"battery": 50})

    def lose(self, count: int) -> None:
        self.seq += count  # 這幾個 seq 永遠不會到


def availability(ingestor: Ingestor) -> list[tuple]:
    return ingestor.connection.execute(
        f"SELECT {COLUMNS} FROM v_daily_availability WHERE equipment_id = ? ORDER BY day",  # noqa: S608
        (EQ,),
    ).fetchall()


@pytest.fixture
def ingestor(tmp_path):
    instance = Ingestor(tmp_path / "pipeline.sqlite")
    yield instance
    instance.close()


def test_idle_6h_moving_3h_alarm_1h_is_exactly_30_percent(ingestor):
    feed = Feed(ingestor, datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
    feed.state_at(0, "IDLE")
    feed.state_at(6, "MOVING")
    feed.state_at(9, "ALARM")
    feed.state_at(10, "IDLE")  # 時間軸到這裡為止

    assert availability(ingestor) == [
        ("2026-03-01", 3 * HOUR, 6 * HOUR, 1 * HOUR, 0.0, 0.0, 10 * HOUR, 10 * HOUR, 0.3),
    ]


def test_heartbeats_do_not_change_the_numbers(ingestor):
    feed = Feed(ingestor, datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
    feed.state_at(0, "IDLE")
    for minute in range(1, 360):
        feed.heartbeat_at(minute / 60)
    feed.state_at(6, "MOVING")
    feed.heartbeat_at(7.5)
    feed.telemetry_at(8)
    feed.state_at(9, "ALARM")
    feed.state_at(10, "IDLE")

    assert availability(ingestor) == [
        ("2026-03-01", 3 * HOUR, 6 * HOUR, 1 * HOUR, 0.0, 0.0, 10 * HOUR, 10 * HOUR, 0.3),
    ]


def test_offline_time_is_excluded_from_the_denominator(ingestor):
    feed = Feed(ingestor, datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
    feed.state_at(0, "OFFLINE")
    feed.state_at(2, "IDLE")
    feed.state_at(8, "MOVING")
    feed.state_at(11, "ALARM")
    feed.state_at(12, "OFFLINE")
    feed.state_at(14, "IDLE")

    assert availability(ingestor) == [
        ("2026-03-01", 3 * HOUR, 6 * HOUR, 1 * HOUR, 4 * HOUR, 0.0, 14 * HOUR, 10 * HOUR, 0.3),
    ]


def test_gap_period_is_unknown_and_not_in_the_denominator(ingestor):
    """06:00 起 MOVING；08:00 之後掉了 2 筆，09:00 才又收到心跳。

    08:00–09:00 不知道設備在做什麼 -> UNKNOWN 1 小時，不進分母。
    已知的部分：IDLE 6h、MOVING 2h + 1h、ALARM 1h -> 3 / 10 = 30%。
    """
    feed = Feed(ingestor, datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
    feed.state_at(0, "IDLE")
    feed.state_at(6, "MOVING")
    feed.telemetry_at(8)  # 缺口前最後一筆
    feed.lose(2)
    feed.heartbeat_at(9)  # 缺口後第一筆狀態：MOVING
    feed.state_at(10, "ALARM")
    feed.state_at(11, "IDLE")

    assert ingestor.connection.execute("SELECT seq_from, seq_to, ts_from, ts_to FROM gaps").fetchall() == [
        (3, 4, "2026-03-01T08:00:00.000000Z", "2026-03-01T09:00:00.000000Z"),
    ]
    assert availability(ingestor) == [
        ("2026-03-01", 3 * HOUR, 6 * HOUR, 1 * HOUR, 0.0, 1 * HOUR, 11 * HOUR, 10 * HOUR, 0.3),
    ]
    unknown = ingestor.connection.execute(
        "SELECT seconds FROM v_daily_state_seconds WHERE e10_state = 'UNKNOWN'",
    ).fetchall()
    assert unknown == [(1 * HOUR,)]  # view 中單獨列出


def test_unknown_lasts_until_the_next_state_message_not_just_the_next_message(ingestor):
    """缺口後先到的是 telemetry：狀態仍然未知，要等到下一筆 state 才恢復。"""
    feed = Feed(ingestor, datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
    feed.state_at(0, "IDLE")
    feed.telemetry_at(1)
    feed.lose(5)
    feed.telemetry_at(2)  # 缺口結束，但不知道狀態
    feed.heartbeat_at(2.5)  # 這時才知道
    feed.state_at(4, "MOVING")
    feed.state_at(5, "IDLE")

    assert availability(ingestor) == [
        ("2026-03-01", 1 * HOUR, 2.5 * HOUR, 0.0, 0.0, 1.5 * HOUR, 5 * HOUR, 3.5 * HOUR, 1 / 3.5),
    ]


def test_interval_crossing_midnight_utc_is_split_between_days(ingestor):
    feed = Feed(ingestor, datetime(2026, 3, 1, 22, 0, tzinfo=timezone.utc))
    feed.state_at(0, "IDLE")  # 03-01 22:00
    feed.state_at(3, "MOVING")  # 03-02 01:00
    feed.state_at(5, "IDLE")  # 03-02 03:00

    assert availability(ingestor) == [
        ("2026-03-01", 0.0, 2 * HOUR, 0.0, 0.0, 0.0, 2 * HOUR, 2 * HOUR, 0.0),
        ("2026-03-02", 2 * HOUR, 1 * HOUR, 0.0, 0.0, 0.0, 3 * HOUR, 3 * HOUR, 2 / 3),
    ]


def test_all_offline_day_has_null_availability(ingestor):
    feed = Feed(ingestor, datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
    feed.state_at(0, "OFFLINE")
    feed.heartbeat_at(5)
    row = availability(ingestor)[0]
    assert row[4] == 5 * HOUR and row[7] == 0.0 and row[8] is None


def test_alarm_stats_and_move_durations(ingestor):
    start = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    feed = Feed(ingestor, start)
    feed.state_at(0, "IDLE")
    feed._send("event", 1.0, {"event": "MOVE_STARTED", "station": "A"})
    feed._send("event", 1.5, {"event": "ARRIVED", "station": "A"})
    feed._send("event", 2.0, {"event": "MOVE_STARTED", "station": "B"})
    feed._send("alarm", 2.25, {"alarm": "LOW_BATTERY", "action": "set"})  # 中止，沒有 ARRIVED
    feed._send("alarm", 2.75, {"alarm": "LOW_BATTERY", "action": "clear"})
    feed._send("event", 3.0, {"event": "MOVE_STARTED", "station": "B"})
    feed._send("event", 3.25, {"event": "ARRIVED", "station": "B"})
    feed._send("alarm", 4.0, {"alarm": "LOW_BATTERY", "action": "set"})  # 還沒解除

    assert ingestor.connection.execute(
        "SELECT station, duration_s FROM v_move_durations ORDER BY start_seq",
    ).fetchall() == [("A", 1800.0), ("B", None), ("B", 900.0)]
    assert ingestor.connection.execute(
        "SELECT day, alarm, set_count, cleared_count, total_duration_s, avg_duration_s FROM v_daily_alarm_stats",
    ).fetchall() == [("2026-03-01", "LOW_BATTERY", 2, 1, 1800.0, 1800.0)]
