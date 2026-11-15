"""把設備內部的狀態、事件、警報、電量發佈到 MQTT。

掛法：``TelemetryPublisher(...).attach(equipment)``。只用既有的訂閱點，設備碼不用改：

* ``FakeRobot.add_listener``：機器人狀態轉換（由此導出 MOVE_STARTED／ARRIVED 與 LOW_BATTERY set／clear）
* secsgem 控制狀態機各狀態的 ``events.enter``：設備 OFF-LINE／ON-LINE
* 電量與心跳：由 ``tick()`` 依注入的時鐘定期取樣

**不拖累設備**：設備執行緒上的回呼只做「組 JSON、放進有上限的 deque」。真正碰網路的只有
``flush()``（由 ``tick()`` 呼叫，跑在呼叫者或 publisher 自己的執行緒）。broker 不通時佇列滿了丟最舊的，
累計 ``dropped``；恢復後補發一筆 ``event=GAP`` 說明丟了哪一段。
"""

from __future__ import annotations

import collections
import functools
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

import payload_schema
from fake_robot import Reason, RobotState, Transition

_LOG = logging.getLogger(__name__)
_ONLINE_CONTROL_STATES = ("ONLINE_LOCAL", "ONLINE_REMOTE")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Transport(Protocol):
    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        """送出一則訊息。回 False 或丟例外都代表 broker 目前不可用。不可長時間阻塞。"""


class LoopbackTransport:
    """行程內的假 broker：直接把訊息交給 ``deliver``。測試與 simulate_day 用。"""

    def __init__(self, deliver: Callable[[str, bytes], None]) -> None:
        self._deliver = deliver
        self.available = True

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        del qos, retain
        if not self.available:
            return False
        self._deliver(topic, payload)
        return True


class MqttTransport:
    """paho-mqtt 2.x 的轉接（對 docker compose 的 mosquitto 有整合測試）。

    未連線時直接回 False（讓 publisher 自己的有上限佇列處理），不使用 paho 內部的離線佇列。
    回 True 只代表 paho 已接受並送出 PUBLISH，不等 PUBACK。
    """

    def __init__(  # noqa: PLR0913
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "",
        keepalive: int = 30,
    ) -> None:
        import paho.mqtt.client as mqtt  # noqa: PLC0415 - 沒裝 paho 也能用 LoopbackTransport

        self._success = mqtt.MQTT_ERR_SUCCESS
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username is not None:
            self._client.username_pw_set(username, password)
        self._client.connect_async(host, port, keepalive)
        self._client.loop_start()  # paho 自己的網路執行緒，會自動重連

    def wait_connected(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self._client.is_connected():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        if not self._client.is_connected():
            return False
        return self._client.publish(topic, payload, qos=qos, retain=retain).rc == self._success

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


@dataclass
class _Queued:
    seq: int
    ts: str
    topic: str
    payload: bytes


class TelemetryPublisher:
    def __init__(  # noqa: PLR0913
        self,
        transport: Transport,
        *,
        site: str,
        equipment_id: str,
        now: Callable[[], datetime] = utc_now,
        max_queue: int = 1000,
        telemetry_interval: float = 5.0,
        heartbeat_interval: float = 60.0,
        start_seq: int = 0,
        alarm_text: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._alarm_text = alarm_text  # (alarm, action) -> 選用的警報文字
        if max_queue < 1:
            raise ValueError("max_queue must be >= 1")
        self._transport = transport
        self._site = site
        self._equipment_id = equipment_id
        self._now = now
        self._max_queue = max_queue
        self._telemetry_interval = timedelta(seconds=telemetry_interval)
        self._heartbeat_interval = timedelta(seconds=heartbeat_interval)

        self._lock = threading.Lock()  # 佇列、seq、狀態；葉節點鎖，持有時不呼叫外部程式
        self._flush_lock = threading.Lock()  # 同一時間只有一個 flush 在碰 transport
        self._queue: collections.deque[_Queued] = collections.deque()
        self._seq = start_seq
        self._enqueued = 0
        self._published = 0
        self._dropped = 0
        self._gap_reports = 0
        self._pending_gap: dict[str, Any] | None = None

        self._robot = None
        self._robot_state: str | None = None
        self._offline = True
        self._effective: str | None = None
        self._next_telemetry: datetime | None = None
        self._next_heartbeat: datetime | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- 掛到設備上 ----------------------------------------------------------

    def attach(self, equipment) -> TelemetryPublisher:
        """訂閱設備的機器人與控制狀態，並發出第一筆狀態（from = null）。"""
        robot = equipment.robot
        robot_state = robot.state().value
        control_name = equipment.control_state_name
        machine = equipment.control_state
        for state in (machine.equipment_offline, machine.host_offline, machine.online_local, machine.online_remote):
            state.events.enter.register(functools.partial(self._on_control_state, state.name))
        with self._lock:
            self._robot = robot
            self._robot_state = robot_state
            self._offline = control_name not in _ONLINE_CONTROL_STATES
            now = self._now()
            self._next_telemetry = now
            self._next_heartbeat = now + self._heartbeat_interval
            self._update_state_locked(now)
        robot.add_listener(self._on_robot_transition)
        return self

    # ---- 設備執行緒上的回呼：只排隊，不碰網路 ----------------------------------

    def _on_control_state(self, name: str, _data: dict) -> None:
        with self._lock:
            self._offline = name not in _ONLINE_CONTROL_STATES
            if self._robot is not None:
                self._update_state_locked(self._now())

    def _on_robot_transition(self, transition: Transition) -> None:
        with self._lock:
            now = self._now()
            self._robot_state = transition.new.value
            self._update_state_locked(now)
            if transition.reason is Reason.GOTO:
                self._enqueue_locked("event", now, {"event": "MOVE_STARTED", "station": transition.station})
            elif transition.reason is Reason.ARRIVED:
                self._enqueue_locked("event", now, {"event": "ARRIVED", "station": transition.station})
            if transition.new is RobotState.ALARM:
                self._enqueue_alarm_locked(now, "set")
            elif transition.old is RobotState.ALARM:
                self._enqueue_alarm_locked(now, "clear")

    def _enqueue_alarm_locked(self, now: datetime, action: str) -> None:
        body = {"alarm": "LOW_BATTERY", "action": action}
        text = self._alarm_text("LOW_BATTERY", action) if self._alarm_text is not None else None
        if text is not None:
            body["text"] = str(text)[:2000]
        self._enqueue_locked("alarm", now, body)

    def _update_state_locked(self, now: datetime) -> None:
        effective = "OFFLINE" if self._offline else self._robot_state
        if effective != self._effective:
            self._enqueue_locked("state", now, {"from": self._effective, "to": effective, "heartbeat": False})
            self._effective = effective

    # ---- 定期工作 ------------------------------------------------------------

    def tick(self) -> None:
        """到期就取樣電量、補發心跳，然後嘗試把佇列送出去。"""
        robot = self._robot
        if robot is not None:
            now = self._now()
            battery = None
            if now >= self._next_telemetry:
                battery = round(float(robot.battery()), 3)  # 在 publisher 的鎖外讀，避免鎖順序問題
            with self._lock:
                if battery is not None:
                    self._enqueue_locked("telemetry", now, {"battery": battery})
                    while self._next_telemetry <= now:
                        self._next_telemetry += self._telemetry_interval
                if now >= self._next_heartbeat:
                    self._enqueue_locked(
                        "state",
                        now,
                        {"from": self._effective, "to": self._effective, "heartbeat": True},
                    )
                    while self._next_heartbeat <= now:
                        self._next_heartbeat += self._heartbeat_interval
        self.flush()

    def flush(self) -> int:
        """把佇列裡的訊息依序送出，遇到 broker 不可用就停。回傳這次送出的筆數。"""
        sent = 0
        with self._flush_lock:
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    item = self._queue.popleft()
                if not self._try_publish(item):
                    with self._lock:
                        if len(self._queue) >= self._max_queue:
                            self._note_drop_locked(item)  # 送不出去的這筆就是最舊的
                        else:
                            self._queue.appendleft(item)
                    break
                sent += 1
                with self._lock:
                    self._published += 1
                    if self._pending_gap is not None:
                        # broker 恢復了：補一筆 GAP（排在佇列尾，seq 照常遞增）
                        gap, self._pending_gap = self._pending_gap, None
                        self._gap_reports += 1
                        self._enqueue_locked("event", self._now(), {"event": "GAP", **gap})
        return sent

    def _try_publish(self, item: _Queued) -> bool:
        try:
            return bool(self._transport.publish(item.topic, item.payload, payload_schema.QOS, payload_schema.RETAIN))
        except Exception:  # noqa: BLE001 - transport 的任何錯都當成「不可用」
            _LOG.debug("publish failed", exc_info=True)
            return False

    # ---- 佇列 ----------------------------------------------------------------

    def _enqueue_locked(self, message_type: str, now: datetime, body: dict[str, Any]) -> None:
        ts = payload_schema.format_ts(now)
        payload = {
            "schema_version": payload_schema.SCHEMA_VERSION,
            "equipment_id": self._equipment_id,
            "seq": self._seq,
            "ts": ts,
            **body,
        }
        item = _Queued(
            seq=self._seq,
            ts=ts,
            topic=payload_schema.topic_for(self._site, self._equipment_id, message_type),
            payload=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )
        self._seq += 1
        self._enqueued += 1
        if len(self._queue) >= self._max_queue:
            self._note_drop_locked(self._queue.popleft())
        self._queue.append(item)

    def _note_drop_locked(self, item: _Queued) -> None:
        self._dropped += 1
        if self._pending_gap is None:
            self._pending_gap = {
                "dropped": 0,
                "first_dropped_seq": item.seq,
                "last_dropped_seq": item.seq,
                "first_dropped_ts": item.ts,
                "last_dropped_ts": item.ts,
            }
        gap = self._pending_gap
        gap["dropped"] += 1
        gap["last_dropped_seq"] = item.seq
        gap["last_dropped_ts"] = item.ts

    # ---- 觀察 ----------------------------------------------------------------

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "enqueued": self._enqueued,
                "published": self._published,
                "dropped": self._dropped,
                "queued": len(self._queue),
                "gap_reports": self._gap_reports,
                "next_seq": self._seq,
            }

    # ---- 即時模式用的背景執行緒（測試與 simulate_day 直接呼叫 tick()） -----------

    def start(self, interval: float = 1.0) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    _LOG.exception("telemetry tick failed")

        self._stop.clear()
        self._thread = threading.Thread(target=loop, name="telemetry_publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
