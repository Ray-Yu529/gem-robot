"""ingest：schema 驗證、rejected、冪等、缺號。不需要 broker 或 docker。"""

from __future__ import annotations

import json

import pytest

import payload_schema
from ingest import DUPLICATE, INSERTED, REJECTED, Ingestor

EQ = "ROBOT01"


def msg(message_type: str, seq: int, ts: str, **body) -> tuple[str, bytes]:
    payload = {"schema_version": 1, "equipment_id": EQ, "seq": seq, "ts": ts, **body}
    return payload_schema.topic_for("test", EQ, message_type), json.dumps(payload).encode("utf-8")


def count(ingestor: Ingestor, table: str) -> int:
    return ingestor.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


@pytest.fixture
def ingestor(tmp_path):
    instance = Ingestor(tmp_path / "pipeline.sqlite")
    yield instance
    instance.close()


VALID = [
    ("state", {"from": None, "to": "OFFLINE", "heartbeat": False}, "state_changes"),
    ("state", {"from": "IDLE", "to": "IDLE", "heartbeat": True}, "state_changes"),
    ("event", {"event": "MOVE_STARTED", "station": "A"}, "events"),
    ("event", {"event": "ARRIVED", "station": "A"}, "events"),
    ("alarm", {"alarm": "LOW_BATTERY", "action": "set"}, "alarms"),
    ("alarm", {"alarm": "LOW_BATTERY", "action": "clear"}, "alarms"),
    ("telemetry", {"battery": 87.5}, "telemetry"),
    ("telemetry", {"battery": 100}, "telemetry"),
]


@pytest.mark.parametrize(("message_type", "body", "table"), VALID)
def test_valid_payloads_are_stored(ingestor, message_type, body, table):
    assert ingestor.handle(*msg(message_type, 0, "2026-03-01T00:00:00Z", **body)) == INSERTED
    assert count(ingestor, table) == 1
    assert count(ingestor, "rejected") == 0
    stored_ts, stored_us = ingestor.connection.execute(f"SELECT ts, ts_us FROM {table}").fetchone()  # noqa: S608
    assert stored_ts == "2026-03-01T00:00:00Z"
    assert stored_us == 1772323200 * 1_000_000  # UTC


INVALID = [
    ("missing ts", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "battery": 50}, "'ts' is a required property"),
    ("missing seq", "telemetry", {"schema_version": 1, "equipment_id": EQ, "ts": "2026-03-01T00:00:00Z", "battery": 50}, "'seq' is a required property"),
    ("missing type field", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z"}, "'battery' is a required property"),
    ("seq is a string", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": "0", "ts": "2026-03-01T00:00:00Z", "battery": 50}, "seq"),
    ("seq is a float", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 1.0, "ts": "2026-03-01T00:00:00Z", "battery": 50}, "seq"),
    ("battery is a string", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "battery": "high"}, "battery"),
    ("battery out of range", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "battery": 140}, "battery"),
    ("unknown state", "state", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "from": "IDLE", "to": "DANCING", "heartbeat": False}, "to"),
    ("heartbeat not bool", "state", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "from": "IDLE", "to": "IDLE", "heartbeat": 1}, "heartbeat"),
    ("event without station", "event", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "event": "ARRIVED"}, "'station' is a required property"),
    ("bad alarm action", "alarm", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "alarm": "LOW_BATTERY", "action": "maybe"}, "action"),
    ("wrong schema_version", "telemetry", {"schema_version": 2, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "battery": 50}, "schema_version"),
    ("ts not UTC", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T08:00:00+08:00", "battery": 50}, "ts"),
    ("ts not a real date", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-02-31T00:00:00Z", "battery": 50}, "ts"),
    ("extra field", "telemetry", {"schema_version": 1, "equipment_id": EQ, "seq": 0, "ts": "2026-03-01T00:00:00Z", "battery": 50, "voltage": 3.3}, "Additional properties"),
    ("equipment mismatch", "telemetry", {"schema_version": 1, "equipment_id": "OTHER", "seq": 0, "ts": "2026-03-01T00:00:00Z", "battery": 50}, "does not match topic"),
]


@pytest.mark.parametrize(("label", "message_type", "payload", "reason_part"), INVALID, ids=[case[0] for case in INVALID])
def test_invalid_payloads_go_to_rejected_with_a_reason(ingestor, label, message_type, payload, reason_part):
    topic = payload_schema.topic_for("test", EQ, message_type)
    raw = json.dumps(payload).encode("utf-8")
    assert ingestor.handle(topic, raw) == REJECTED

    rows = ingestor.connection.execute("SELECT topic, payload, reason FROM rejected").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == topic
    assert rows[0][1] == raw.decode("utf-8")  # 原始訊息留著，不丟掉
    assert reason_part in rows[0][2]
    for table in ("state_changes", "events", "alarms", "telemetry", "message_log", "gaps"):
        assert count(ingestor, table) == 0


def test_non_json_and_bad_topic_are_rejected(ingestor):
    assert ingestor.handle(payload_schema.topic_for("test", EQ, "telemetry"), b"{not json") == REJECTED
    assert ingestor.handle(payload_schema.topic_for("test", EQ, "telemetry"), b"\xff\xfe") == REJECTED
    assert ingestor.handle(payload_schema.topic_for("test", EQ, "telemetry"), b"[1, 2]") == REJECTED
    assert ingestor.handle("fab/test/ROBOT01/unknown", b"{}") == REJECTED
    assert ingestor.handle("somewhere/else", b"{}") == REJECTED
    reasons = [row[0] for row in ingestor.connection.execute("SELECT reason FROM rejected ORDER BY id")]
    assert len(reasons) == 5
    assert "not valid UTF-8 JSON" in reasons[0]
    assert "topic does not match" in reasons[4]


def test_same_message_twice_is_stored_once(ingestor):
    message = msg("event", 7, "2026-03-01T00:00:00Z", event="ARRIVED", station="A")
    assert ingestor.handle(*message) == INSERTED
    assert ingestor.handle(*message) == DUPLICATE
    assert ingestor.handle(*message) == DUPLICATE
    assert count(ingestor, "events") == 1
    assert count(ingestor, "message_log") == 1
    assert count(ingestor, "gaps") == 0
    assert ingestor.counts == {INSERTED: 1, DUPLICATE: 2, REJECTED: 0}


def test_same_seq_is_claimed_once_across_message_types(ingestor):
    assert ingestor.handle(*msg("telemetry", 3, "2026-03-01T00:00:00Z", battery=50)) == INSERTED
    assert ingestor.handle(*msg("alarm", 3, "2026-03-01T00:00:01Z", alarm="LOW_BATTERY", action="set")) == DUPLICATE
    assert count(ingestor, "alarms") == 0


def test_rejected_message_redelivery_is_stored_once(ingestor):
    topic = payload_schema.topic_for("test", EQ, "telemetry")
    assert ingestor.handle(topic, b"{}") == REJECTED
    assert ingestor.handle(topic, b"{}") == REJECTED
    assert count(ingestor, "rejected") == 1


def test_seq_jump_creates_a_gap_record(ingestor):
    ingestor.handle(*msg("telemetry", 0, "2026-03-01T00:00:00Z", battery=90))
    ingestor.handle(*msg("telemetry", 1, "2026-03-01T00:00:05Z", battery=89))
    ingestor.handle(*msg("telemetry", 5, "2026-03-01T00:00:25Z", battery=85))
    ingestor.handle(*msg("telemetry", 6, "2026-03-01T00:00:30Z", battery=84))

    rows = ingestor.connection.execute(
        "SELECT equipment_id, seq_from, seq_to, missing, ts_from, ts_to, source FROM gaps",
    ).fetchall()
    assert rows == [(EQ, 2, 4, 3, "2026-03-01T00:00:05Z", "2026-03-01T00:00:25Z", "seq_missing")]

    # 重送跳號後的那一筆不會多出第二筆缺號紀錄
    assert ingestor.handle(*msg("telemetry", 5, "2026-03-01T00:00:25Z", battery=85)) == DUPLICATE
    assert count(ingestor, "gaps") == 1


def test_contiguous_seq_has_no_gaps_and_gaps_are_per_equipment(ingestor):
    for seq in range(5):
        ingestor.handle(*msg("telemetry", seq, f"2026-03-01T00:00:0{seq}Z", battery=90))
    other = {"schema_version": 1, "equipment_id": "ROBOT02", "seq": 100, "ts": "2026-03-01T00:00:00Z", "battery": 50}
    ingestor.handle(payload_schema.topic_for("test", "ROBOT02", "telemetry"), json.dumps(other).encode())
    assert count(ingestor, "gaps") == 0  # ROBOT02 的第一筆是 100，不算缺號（之前的本來就沒看過）


def test_gap_event_from_publisher_is_recorded(ingestor):
    ingestor.handle(*msg("telemetry", 0, "2026-03-01T00:00:00Z", battery=90))
    ingestor.handle(
        *msg(
            "event",
            1,
            "2026-03-01T01:00:00Z",
            event="GAP",
            dropped=12,
            first_dropped_seq=40,
            last_dropped_seq=51,
            first_dropped_ts="2026-03-01T00:10:00Z",
            last_dropped_ts="2026-03-01T00:20:00Z",
        ),
    )
    rows = ingestor.connection.execute("SELECT seq_from, seq_to, missing, source FROM gaps").fetchall()
    assert rows == [(40, 51, 12, "publisher_reported")]
    assert ingestor.connection.execute("SELECT event, station FROM events").fetchall() == [("GAP", None)]


def test_ingest_state_survives_restart(tmp_path):
    path = tmp_path / "pipeline.sqlite"
    first = Ingestor(path)
    first.handle(*msg("telemetry", 0, "2026-03-01T00:00:00Z", battery=90))
    first.close()

    second = Ingestor(path)  # 重開（schema 與 view 重新套用）不會弄丟狀態
    second.handle(*msg("telemetry", 3, "2026-03-01T00:00:15Z", battery=88))
    assert second.connection.execute("SELECT seq_from, seq_to FROM gaps").fetchall() == [(1, 2)]
    second.close()
