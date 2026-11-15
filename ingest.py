"""訂閱 MQTT、驗證載荷、寫進資料庫（SQLite）。

核心是 ``Ingestor.handle(topic, payload)``，不依賴 broker，可以直接餵訊息（測試與 simulate_day 就是這樣用）。
``python ingest.py --db pipeline.sqlite --mqtt-host HOST`` 會用 paho-mqtt 訂閱 ``fab/+/+/+``；
``--stack`` 則用 .env 連本機 docker compose 的 mosquitto 與 TimescaleDB（有整合測試）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import db
import payload_schema

SQL_DIR = Path(__file__).resolve().parent / "sql"

INSERTED = "inserted"
DUPLICATE = "duplicate"
REJECTED = "rejected"


def _utc_now_text() -> str:
    return payload_schema.format_ts(datetime.now(timezone.utc))


def connect(db_path: str | Path):
    """開資料庫。SQLite：套用 schema 與 view（可重複執行）。

    ``postgresql://`` DSN：連到 TimescaleDB；schema 由 docker compose 的初始化 SQL（sql/pg/）建立，這裡不動它。
    """
    if db.is_postgres(db_path):
        return db.PgConnection(db_path)
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    connection.executescript((SQL_DIR / "views.sql").read_text(encoding="utf-8"))
    connection.commit()
    return connection


class Ingestor:
    """每則訊息一個交易：驗證 -> 去重 -> 寫入 -> 缺號偵測。

    ``commit_each=False`` 時由呼叫者自己定期 ``commit()``（大量匯入用）。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        commit_each: bool = True,
        now: Callable[[], str] = _utc_now_text,
    ) -> None:
        self.connection = connect(db_path)
        self._commit_each = commit_each
        self._now = now
        self._lock = threading.Lock()
        self.counts = {INSERTED: 0, DUPLICATE: 0, REJECTED: 0}
        # ingest_state 的記憶體快取：{equipment_id: (last_seq, last_ts, last_ts_us)}。
        # 每則訊息只在記憶體更新，commit 之前才寫回資料表（同一個交易），省掉每則訊息兩次往返。
        self._state: dict[str, tuple[int, str, int] | None] = {}
        self._dirty: set[str] = set()

    def handle(self, topic: str, payload: bytes) -> str:
        """處理一則訊息，回傳 'inserted'／'duplicate'／'rejected'。"""
        with self._lock:
            try:
                try:
                    site, message_type, data = payload_schema.validate(topic, payload)
                except payload_schema.PayloadError as exc:
                    self._reject(topic, payload, str(exc))
                    outcome = REJECTED
                else:
                    outcome = self._store(site, message_type, data)
                if self._commit_each:
                    self._commit_locked()
            except Exception:
                self._rollback_locked()
                raise
            self.counts[outcome] += 1
            return outcome

    def commit(self) -> None:
        with self._lock:
            try:
                self._commit_locked()
            except Exception:
                self._rollback_locked()
                raise

    def close(self) -> None:
        with self._lock:
            self._commit_locked()
            self.connection.close()

    def _commit_locked(self) -> None:
        for equipment_id in sorted(self._dirty):
            last_seq, last_ts, last_ts_us = self._state[equipment_id]
            self.connection.execute(
                "INSERT INTO ingest_state (equipment_id, last_seq, last_ts, last_ts_us) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (equipment_id) DO UPDATE SET last_seq = excluded.last_seq, "
                "last_ts = excluded.last_ts, last_ts_us = excluded.last_ts_us",
                (equipment_id, last_seq, last_ts, last_ts_us),
            )
        self._dirty.clear()
        self.connection.commit()

    def _rollback_locked(self) -> None:
        self.connection.rollback()
        self._state.clear()  # 快取可能領先資料庫：丟掉，下次用到再讀
        self._dirty.clear()

    # ---- 內部 ---------------------------------------------------------------

    def _reject(self, topic: str, payload: bytes, reason: str) -> None:
        raw = bytes(payload) if isinstance(payload, (bytes, bytearray)) else str(payload).encode("utf-8")
        digest = hashlib.sha256(topic.encode("utf-8") + b"\x00" + raw).hexdigest()
        self.connection.execute(
            "INSERT INTO rejected (payload_sha256, topic, payload, reason, received_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (payload_sha256) DO NOTHING",
            (digest, topic, raw.decode("utf-8", errors="replace"), reason, self._now()),
        )

    def _store(self, site: str, message_type: str, data: dict) -> str:
        conn = self.connection
        equipment_id, seq, ts = data["equipment_id"], data["seq"], data["ts"]
        ts_us = payload_schema.ts_to_us(ts)

        claimed = conn.execute(
            "INSERT INTO message_log (equipment_id, seq, type) VALUES (?, ?, ?) "
            "ON CONFLICT (equipment_id, seq) DO NOTHING",
            (equipment_id, seq, message_type),
        ).rowcount
        if claimed == 0:
            return DUPLICATE  # 這個 (equipment_id, seq) 已經收過，冪等地忽略

        common = (equipment_id, seq, site, ts, ts_us)
        if message_type == "state":
            conn.execute(
                "INSERT INTO state_changes (equipment_id, seq, site, ts, ts_us, from_state, to_state, heartbeat) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (*common, data["from"], data["to"], int(data["heartbeat"])),
            )
        elif message_type == "event":
            details = {k: v for k, v in data.items() if k not in ("schema_version", "equipment_id", "seq", "ts", "event", "station")}
            conn.execute(
                "INSERT INTO events (equipment_id, seq, site, ts, ts_us, event, station, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (*common, data["event"], data.get("station"), json.dumps(details, sort_keys=True) if details else None),
            )
            if data["event"] == "GAP":
                self._insert_gap(
                    equipment_id,
                    data["first_dropped_seq"],
                    data["last_dropped_seq"],
                    data["dropped"],
                    data["first_dropped_ts"],
                    data["last_dropped_ts"],
                    "publisher_reported",
                )
        elif message_type == "alarm":
            conn.execute(
                "INSERT INTO alarms (equipment_id, seq, site, ts, ts_us, alarm, action, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (*common, data["alarm"], data["action"], data.get("text")),
            )
        else:
            conn.execute(
                "INSERT INTO telemetry (equipment_id, seq, site, ts, ts_us, battery) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (*common, float(data["battery"])),
            )

        self._track_sequence(equipment_id, seq, ts, ts_us)
        return INSERTED

    def _track_sequence(self, equipment_id: str, seq: int, ts: str, ts_us: int) -> None:
        if equipment_id not in self._state:
            row = self.connection.execute(
                "SELECT last_seq, last_ts, last_ts_us FROM ingest_state WHERE equipment_id = ?",
                (equipment_id,),
            ).fetchone()
            self._state[equipment_id] = tuple(row) if row else None
        state = self._state[equipment_id]
        if state is not None:
            last_seq, last_ts, _ = state
            if seq <= last_seq:
                return  # 晚到的舊訊息：已寫入，但不回頭修改缺號紀錄（見 README 限制）
            if seq > last_seq + 1:
                self._insert_gap(equipment_id, last_seq + 1, seq - 1, seq - last_seq - 1, last_ts, ts, "seq_missing")
        self._state[equipment_id] = (seq, ts, ts_us)
        self._dirty.add(equipment_id)

    def _insert_gap(  # noqa: PLR0913
        self,
        equipment_id: str,
        seq_from: int,
        seq_to: int,
        missing: int,
        ts_from: str,
        ts_to: str,
        source: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO gaps (equipment_id, seq_from, seq_to, missing, ts_from, ts_from_us, ts_to, ts_to_us, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (equipment_id, seq_from, source) DO NOTHING",
            (
                equipment_id,
                seq_from,
                seq_to,
                missing,
                ts_from,
                payload_schema.ts_to_us(ts_from),
                ts_to,
                payload_schema.ts_to_us(ts_to),
                source,
            ),
        )


def start_mqtt_ingest(  # noqa: PLR0913
    ingestor: Ingestor,
    host: str,
    port: int = 1883,
    username: str | None = None,
    password: str | None = None,
    client_id: str = "gem-robot-ingest",
):
    """訂閱 ``fab/+/+/+``（QoS 1）並把每則訊息交給 ingestor。回傳已啟動背景網路執行緒的 paho client。

    ``client.subscribed`` 是 threading.Event：SUBACK 回來後才 set，之後發佈的訊息才保證收得到。
    """
    import paho.mqtt.client as mqtt  # noqa: PLC0415

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True)
    client.subscribed = threading.Event()
    if username is not None:
        client.username_pw_set(username, password)

    def on_connect(client, userdata, flags, reason_code, properties):
        del userdata, flags, properties
        if not reason_code.is_failure:
            client.subscribe(f"{payload_schema.TOPIC_ROOT}/+/+/+", qos=payload_schema.QOS)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        del userdata, mid, reason_codes, properties
        client.subscribed.set()

    def on_message(client, userdata, message):
        del client, userdata
        ingestor.handle(message.topic, message.payload)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.connect(host, port)
    client.loop_start()
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description="MQTT -> 資料庫 ingest")
    parser.add_argument("--db", default="pipeline.sqlite", help="SQLite 檔案路徑，或 postgresql:// DSN")
    parser.add_argument("--stack", action="store_true", help="用 .env 連本機 docker compose 的 mosquitto 與 TimescaleDB")
    parser.add_argument("--mqtt-host")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-username")
    parser.add_argument("--mqtt-password")
    args = parser.parse_args()

    if args.stack:
        import stack_env  # noqa: PLC0415

        env = stack_env.load_env()
        target, mqtt = stack_env.owner_dsn(env), stack_env.mqtt_params(env)
    elif args.mqtt_host:
        target = args.db
        mqtt = {
            "host": args.mqtt_host,
            "port": args.mqtt_port,
            "username": args.mqtt_username,
            "password": args.mqtt_password,
        }
    else:
        parser.error("give --stack or --mqtt-host")

    ingestor = Ingestor(target)
    client = start_mqtt_ingest(ingestor, **mqtt)
    print("ingest running; Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        ingestor.close()
        print(ingestor.counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
