"""TelemetryPublisher：掛在設備上、載荷格式、心跳、broker 斷線時的行為。不需要 broker 或 docker。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import gem_ids
import payload_schema
from fake_robot import FakeRobot, ManualClock, RobotConfig, RobotState
from gem_equipment import RobotGemEquipment
from history import History
from ingest import INSERTED, Ingestor
from telemetry_publisher import LoopbackTransport, TelemetryPublisher

START = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
CONFIG = RobotConfig(
    stations=("A", "B", "CHARGER"),
    start_station="CHARGER",
    move_seconds=20.0,
    battery_initial=100.0,
    drain_per_second=1.0,
    low_threshold=20.0,
)


class Recorder:
    """假 broker：記下每一則 publish；available=False 時丟例外（模擬 broker 連不上）。"""

    def __init__(self) -> None:
        self.available = True
        self.calls: list[tuple[str, dict, int, bool]] = []
        self.attempts = 0

    def publish(self, topic, payload, qos, retain):
        self.attempts += 1
        if not self.available:
            raise ConnectionError("broker unreachable")
        self.calls.append((topic, json.loads(payload), qos, retain))
        return True

    def of_type(self, message_type: str) -> list[dict]:
        return [payload for topic, payload, _, _ in self.calls if topic.endswith("/" + message_type)]


class Bench:
    """不開任何 socket 的設備（不呼叫 start()），一開始就是 ONLINE_REMOTE。"""

    def __init__(self, tmp_path, transport, **publisher_kwargs) -> None:
        self.clock = ManualClock()
        self.robot = FakeRobot(CONFIG, clock=self.clock)
        self.equipment = RobotGemEquipment(
            self.robot,
            History(tmp_path / "history.jsonl"),
            initial_control_state="ONLINE",
            poll_interval=None,
        )
        assert self.equipment.control_state_name == "ONLINE_REMOTE"
        self.publisher = TelemetryPublisher(
            transport,
            site="test",
            equipment_id="ROBOT01",
            now=lambda: START + timedelta(seconds=self.clock()),
            **publisher_kwargs,
        ).attach(self.equipment)

    def run(self, seconds: float, step: float = 5.0) -> None:
        for _ in range(round(seconds / step)):
            self.clock.advance(step)
            self.equipment.poll_robot()
            self.publisher.tick()


def test_topics_qos_retain_and_payloads_match_the_schema(tmp_path):
    recorder = Recorder()
    bench = Bench(tmp_path, recorder)
    bench.publisher.tick()
    assert bench.robot.goto("A").name == "ACCEPTED"
    bench.run(60)

    assert recorder.calls, "nothing was published"
    for topic, payload, qos, retain in recorder.calls:
        assert qos == 1 and retain is False
        site, message_type, parsed = payload_schema.validate(topic, json.dumps(payload))
        assert (site, parsed["equipment_id"]) == ("test", "ROBOT01")
        assert topic == f"fab/test/ROBOT01/{message_type}"
    assert [payload["seq"] for _, payload, _, _ in recorder.calls] == list(range(len(recorder.calls)))
    assert recorder.calls[0][1]["ts"] == "2026-03-01T00:00:00.000000Z"  # UTC


def test_state_is_published_on_transitions_plus_a_60s_heartbeat(tmp_path):
    recorder = Recorder()
    bench = Bench(tmp_path, recorder)
    bench.publisher.tick()
    bench.robot.goto("A")  # t=0 開始移動，t=20 到站
    bench.run(180)

    states = recorder.of_type("state")
    transitions = [(s["from"], s["to"], s["ts"][11:19]) for s in states if not s["heartbeat"]]
    heartbeats = [(s["from"], s["to"], s["ts"][11:19]) for s in states if s["heartbeat"]]
    assert transitions == [
        (None, "IDLE", "00:00:00"),
        ("IDLE", "MOVING", "00:00:00"),
        ("MOVING", "IDLE", "00:00:20"),
    ]
    assert heartbeats == [("IDLE", "IDLE", "00:01:00"), ("IDLE", "IDLE", "00:02:00"), ("IDLE", "IDLE", "00:03:00")]


def test_telemetry_every_5_seconds(tmp_path):
    recorder = Recorder()
    bench = Bench(tmp_path, recorder)
    bench.publisher.tick()
    bench.robot.goto("A")
    bench.run(30, step=1.0)  # 每秒 tick 一次，仍然只有每 5 秒一筆

    telemetry = recorder.of_type("telemetry")
    assert [t["ts"][17:19] for t in telemetry] == ["00", "05", "10", "15", "20", "25", "30"]
    assert [t["battery"] for t in telemetry] == [100, 95, 90, 85, 80, 80, 80]


def test_move_events_carry_the_station_and_alarm_set_clear(tmp_path):
    recorder = Recorder()
    bench = Bench(tmp_path, recorder)
    bench.robot.goto("A")
    bench.run(20)
    for station in ("B", "CHARGER", "A"):  # 每趟 20%，第 5 趟途中跌破 20%
        bench.robot.goto(station)
        bench.run(20)
    bench.robot.goto("B")
    bench.run(20)
    assert bench.robot.state() is RobotState.ALARM
    bench.equipment.charge_robot()
    bench.publisher.tick()

    events = [(e["event"], e["station"]) for e in recorder.of_type("event")]
    assert events[:2] == [("MOVE_STARTED", "A"), ("ARRIVED", "A")]
    assert events[-1] == ("MOVE_STARTED", "B")  # 最後一趟被警報中止，沒有 ARRIVED
    assert [(a["alarm"], a["action"]) for a in recorder.of_type("alarm")] == [
        ("LOW_BATTERY", "set"),
        ("LOW_BATTERY", "clear"),
    ]
    assert [s["to"] for s in recorder.of_type("state") if not s["heartbeat"]][-2:] == ["ALARM", "IDLE"]


def test_equipment_offline_is_published_as_offline_state(tmp_path):
    recorder = Recorder()
    bench = Bench(tmp_path, recorder)
    bench.equipment.control_switch_offline()
    bench.publisher.tick()
    assert [(s["from"], s["to"]) for s in recorder.of_type("state")] == [(None, "IDLE"), ("IDLE", "OFFLINE")]


def test_broker_down_drops_oldest_counts_them_and_reports_a_gap_on_recovery(tmp_path):
    recorder = Recorder()
    recorder.available = False
    bench = Bench(tmp_path, recorder, max_queue=10)

    # broker 不通的 10 分鐘內，機器人照常工作
    assert bench.robot.goto("A").name == "ACCEPTED"
    bench.run(600)
    assert bench.robot.state() is RobotState.IDLE and bench.robot.position() == "A"

    stats = bench.publisher.stats()
    assert stats["published"] == 0 and recorder.calls == []
    assert stats["queued"] == 10  # 佇列有上限
    assert stats["dropped"] == stats["enqueued"] - 10  # 其餘全部丟掉（最舊的先丟）
    assert stats["dropped"] == bench.publisher.dropped > 100
    dropped, enqueued = stats["dropped"], stats["enqueued"]

    # 恢復：送出佇列裡剩下的 10 筆 + 一筆 GAP
    recorder.available = True
    bench.publisher.flush()
    seqs = [payload["seq"] for _, payload, _, _ in recorder.calls]
    assert seqs == list(range(enqueued - 10, enqueued + 1))
    gap = recorder.calls[-1][1]
    assert gap["event"] == "GAP"
    assert gap["dropped"] == dropped
    assert (gap["first_dropped_seq"], gap["last_dropped_seq"]) == (0, dropped - 1)
    assert gap["first_dropped_ts"] == "2026-03-01T00:00:00.000000Z"
    assert bench.publisher.stats()["gap_reports"] == 1

    # 之後不會再重複回報
    bench.run(10)
    assert [e["event"] for e in recorder.of_type("event")].count("GAP") == 1
    assert bench.publisher.dropped == dropped


def test_recovered_stream_produces_gap_rows_in_the_database(tmp_path):
    ingestor = Ingestor(tmp_path / "pipeline.sqlite")
    transport = LoopbackTransport(ingestor.handle)
    bench = Bench(tmp_path, transport, max_queue=20)
    bench.run(60)  # 正常 1 分鐘
    before = bench.publisher.stats()["enqueued"]

    transport.available = False
    bench.run(600)
    transport.available = True
    bench.run(60)

    stats = bench.publisher.stats()
    assert stats["dropped"] > 0 and stats["queued"] == 0
    assert ingestor.counts[INSERTED] == stats["published"] == stats["enqueued"] - stats["dropped"]
    rows = ingestor.connection.execute("SELECT source, seq_from, seq_to, missing FROM gaps ORDER BY source").fetchall()
    expected = (before, before + stats["dropped"] - 1, stats["dropped"])
    assert rows == [("publisher_reported", *expected), ("seq_missing", *expected)]
    ingestor.close()


def test_equipment_keeps_working_over_hsms_while_broker_is_unavailable(rig):
    """完整的設備（HSMS + 主機）掛上連不到 broker 的 publisher：GEM 行為不受影響。"""
    recorder = Recorder()
    recorder.available = False
    publisher = TelemetryPublisher(
        recorder,
        site="test",
        equipment_id="ROBOT01",
        now=lambda: START + timedelta(seconds=rig.clock()),
        max_queue=5,
    ).attach(rig.equipment)

    rig.go_online_remote()
    for station in ("A", "B", "CHARGER"):
        assert rig.host.move(station) == 4
        assert rig.host.move("A") == 2  # 移動中
        for _ in range(5):
            rig.advance(1.0)
            publisher.tick()
    assert rig.equipment.wait_idle()

    assert [event.name for event in rig.host.events()] == ["MOVE_STARTED", "ARRIVED"] * 3
    assert rig.host.events()[-1].values == {gem_ids.DVID_STATION: "CHARGER"}
    assert rig.robot.position() == "CHARGER"
    assert rig.history.verify() is True

    stats = publisher.stats()
    assert recorder.attempts > 0 and stats["published"] == 0
    assert stats["queued"] == 5
    assert stats["dropped"] == stats["enqueued"] - 5 > 0


def test_max_queue_must_be_positive():
    with pytest.raises(ValueError):
        TelemetryPublisher(Recorder(), site="s", equipment_id="e", max_queue=0)
