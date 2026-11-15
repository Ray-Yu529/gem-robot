"""GEM 設備端：secsgem 的 GemEquipmentHandler + 假機器人。

依公開資料與個人理解實作的 GEM 子集，未經標準一致性驗證。

與 secsgem 0.3.0 內建行為不同的地方（都寫在 README「限制」）：

* ``_on_s02f41`` 整個覆寫：內建版本不檢查控制狀態、機器人狀態與參數值，一律回 HCACK=4。
* ``trigger_collection_events`` 覆寫：內建版本每次呼叫各開一條執行緒送 S6F11，
  連續兩個事件的先後不保證。這裡改成在觸發當下組好報告、丟進單一送出佇列依序送。
"""

from __future__ import annotations

import collections
import functools
import logging
import queue
import threading
import time
from typing import Any

import secsgem.common
import secsgem.gem
import secsgem.hsms
import secsgem.secs
from secsgem.gem.communication_state_machine import CommunicationState
from secsgem.gem.control_state_machine import ControlState

import gem_ids
from fake_robot import FakeRobot, GotoResult, Reason, RobotState, Transition
from history import History
from message_tap import MessageTap, TappedMessage

HCACK = secsgem.secs.data_items.HCACK

_LOG = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class RobotGemEquipment(secsgem.gem.GemEquipmentHandler):
    """把假機器人包成一台 HSMS passive 的 GEM 設備。"""

    def __init__(  # noqa: PLR0913
        self,
        robot: FakeRobot,
        history: History,
        *,
        address: str = "127.0.0.1",
        port: int = 5000,
        session_id: int = 0,
        initial_control_state: str = "HOST_OFFLINE",
        initial_online_control_state: str = "REMOTE",
        poll_interval: float | None = 0.05,
    ) -> None:
        # 這些要在 super().__init__ 之前就緒：父類別建構時就會跑控制狀態機並可能觸發事件。
        self._robot = robot
        self._history = history
        self._op_lock = threading.RLock()  # 鎖順序：_op_lock -> robot 的鎖 -> history 的鎖
        self._pending: collections.deque[Transition] = collections.deque()
        self._outbox: queue.Queue = queue.Queue()
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._poller: threading.Thread | None = None
        self._last_control_state: str | None = None

        settings = secsgem.hsms.HsmsSettings(
            address=address,
            port=port,
            session_id=session_id,
            connect_mode=secsgem.hsms.HsmsConnectMode.PASSIVE,
            device_type=secsgem.common.DeviceType.EQUIPMENT,
        )
        super().__init__(settings, initial_control_state, initial_online_control_state)

        self._mdln = "FAKEROBOT"
        self._softrev = "0.1.0"

        # 遠端命令：只留 MOVE（內建的 START/STOP 沒有實作，移除後會回 HCACK=1）
        self.remote_commands.clear()
        self.remote_commands[gem_ids.RCMD_MOVE] = secsgem.gem.RemoteCommand(
            gem_ids.RCMD_MOVE,
            "Move robot to station",
            [gem_ids.CPNAME_STATION],
            gem_ids.CEID_ARRIVED,
        )

        # 資料變數與事件
        self._station_dv = secsgem.gem.DataValue(
            gem_ids.DVID_STATION,
            "STATION",
            secsgem.secs.variables.String,
            use_callback=False,
        )
        self._station_dv.value = ""
        self.data_values[gem_ids.DVID_STATION] = self._station_dv
        for ceid, name in gem_ids.CEID_NAMES.items():
            data_values = [gem_ids.DVID_STATION] if ceid in (gem_ids.CEID_MOVE_STARTED, gem_ids.CEID_ARRIVED) else []
            self.collection_events[ceid] = secsgem.gem.CollectionEvent(ceid, name, data_values)

        # 警報：設備端預設啟用（主機仍可用 S5F3 開關）
        alarm = secsgem.gem.Alarm(
            gem_ids.ALID_LOW_BATTERY,
            gem_ids.ALID_NAMES[gem_ids.ALID_LOW_BATTERY],
            "Battery below threshold",
            self.settings.data_items.ALCD.EQUIPMENT_STATUS_WARNING,
            gem_ids.CEID_LOW_BATTERY_SET,
            gem_ids.CEID_LOW_BATTERY_CLEARED,
        )
        alarm.enabled = True
        self.alarms[gem_ids.ALID_LOW_BATTERY] = alarm

        # 控制狀態轉換 -> 歷史（只記四個穩定狀態，略過 ONLINE/ATTEMPT_ONLINE 等過渡狀態）
        machine = self.control_state
        for state in (machine.equipment_offline, machine.host_offline, machine.online_local, machine.online_remote):
            state.events.enter.register(functools.partial(self._on_control_state_entered, state.name))
        self._on_control_state_entered(self.control_state_name, {})

        self._tap = MessageTap(self._on_tapped_message, endpoint=self.settings.name)
        robot.add_listener(self._on_robot_transition)
        if robot.state() is RobotState.ALARM:
            self._outbox.put(functools.partial(self._apply_alarm, True))

    # ---- 生命週期 -----------------------------------------------------------

    def start(self) -> None:
        """開始聽 HSMS 連線，並啟動送出佇列與（可選的）機器人輪詢執行緒。"""
        self._stop_event.clear()
        self._tap.install()
        self._history.append("equipment", {"action": "start", "endpoint": self.settings.name})
        self._worker = threading.Thread(target=self._worker_loop, name="gem_equipment_outbox", daemon=True)
        self._worker.start()
        self.enable()
        if self._poll_interval is not None:
            self._poller = threading.Thread(target=self._poll_loop, name="gem_equipment_robot_poll", daemon=True)
            self._poller.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._poller is not None:
            self._poller.join(timeout=2)
            self._poller = None
        self.wait_idle(1.0)  # 盡量把排隊中的事件送完再斷線
        if self.communication_state.current != CommunicationState.DISABLED:
            self.disable()
        if self._worker is not None:
            self._outbox.put(None)
            self._worker.join(timeout=2)
            self._worker = None
        self._history.append("equipment", {"action": "stop"})
        self._tap.uninstall()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """等送出佇列清空（事件／警報都送完並收到回覆）。"""
        deadline = time.monotonic() + timeout
        with self._op_lock:
            pass  # 等進行中的 S2F41 處理／輪詢把它產生的事件排進佇列
        while self._outbox.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        return True

    # ---- 狀態查詢與現場（operator）操作 ---------------------------------------

    @property
    def robot(self) -> FakeRobot:
        return self._robot

    @property
    def history(self) -> History:
        return self._history

    @property
    def control_state_name(self) -> str:
        return self.control_state.current.name

    def poll_robot(self) -> None:
        """推進機器人模擬，並把產生的狀態轉換變成事件／警報。"""
        with self._op_lock:
            self._robot.poll()
            self._drain_robot_transitions()

    def charge_robot(self) -> bool:
        """現場操作：替機器人充電。"""
        with self._op_lock:
            charged = self._robot.charge()
            self._history.append("operator", {"action": "charge", "ok": charged})
            self._drain_robot_transitions()
            return charged

    # 控制狀態的現場切換直接用 secsgem 提供的：
    # control_switch_online() / control_switch_offline() /
    # control_switch_online_local() / control_switch_online_remote()

    # ---- S2F41 遠端命令 ------------------------------------------------------

    def _on_s02f41(self, handler, message):
        """覆寫 secsgem 內建的 S2F41 處理：先做前置檢查，再回 S2F42。"""
        del handler
        function = self.settings.streams_functions.decode(message)
        rcmd = function.RCMD.get()
        params = [(param["CPNAME"], param["CPVAL"]) for param in function.PARAMS.get()]

        with self._op_lock:
            self._history.append(
                "host_command",
                {"sf": "S2F41", "system": message.header.system, "rcmd": _jsonable(rcmd), "params": _jsonable(params)},
            )
            hcack, reason = self._check_and_execute(rcmd, params)
            self._history.append(
                "host_command_reply",
                {"sf": "S2F42", "system": message.header.system, "hcack": hcack, "reason": reason},
            )
            # 先送 S2F42，再放行 MOVE_STARTED；兩者都在 _op_lock 內，輪詢執行緒插不進來。
            self.send_response(self.stream_function(2, 42)({"HCACK": hcack, "PARAMS": []}), message.header.system)
            self._drain_robot_transitions()
        return None

    def _check_and_execute(self, rcmd: Any, params: list[tuple[Any, Any]]) -> tuple[int, str]:
        """確定性的前置檢查。檢查順序：控制狀態 -> RCMD -> 參數 -> 機器人狀態。"""
        if self.control_state.current != ControlState.ONLINE_REMOTE:
            return HCACK.CANT_PERFORM_NOW, f"control state is {self.control_state_name}, not ONLINE_REMOTE"
        if rcmd not in self.remote_commands:
            return HCACK.INVALID_COMMAND, "unknown RCMD"
        # 目前唯一的命令是 MOVE
        if [name for name, _ in params] != [gem_ids.CPNAME_STATION]:
            return HCACK.PARAMETER_INVALID, "MOVE needs exactly one parameter: STATION"
        station = params[0][1]
        if not self._robot.has_station(station):
            return HCACK.PARAMETER_INVALID, "unknown station"
        state = self._robot.state()
        if state is not RobotState.IDLE:
            return HCACK.CANT_PERFORM_NOW, f"robot is {state.value}"
        result = self._robot.goto(station)
        if result is GotoResult.ACCEPTED:
            return HCACK.ACK_FINISH_LATER, "accepted, completion reported by ARRIVED event"
        if result is GotoResult.UNKNOWN_STATION:
            return HCACK.PARAMETER_INVALID, "unknown station"
        return HCACK.CANT_PERFORM_NOW, "robot is not IDLE"

    # ---- 機器人 -> 事件／警報 -------------------------------------------------

    def _on_robot_transition(self, transition: Transition) -> None:
        # 在機器人的鎖內被呼叫：只記錄並排隊，不做任何會阻塞的事。
        self._history.append(
            "robot_state",
            {
                "from": transition.old.value,
                "to": transition.new.value,
                "reason": transition.reason.value,
                "station": transition.station,
                "battery": round(transition.battery, 3),
                "robot_clock": transition.at,
            },
        )
        self._pending.append(transition)

    def _drain_robot_transitions(self) -> None:
        while self._pending:
            transition = self._pending.popleft()
            if transition.reason is Reason.GOTO:
                self._station_dv.value = transition.station
                self.trigger_collection_events([gem_ids.CEID_MOVE_STARTED])
            elif transition.reason is Reason.ARRIVED:
                self._station_dv.value = transition.station
                self.trigger_collection_events([gem_ids.CEID_ARRIVED])
            if transition.new is RobotState.ALARM:
                self._outbox.put(functools.partial(self._apply_alarm, True))
            elif transition.old is RobotState.ALARM:
                self._outbox.put(functools.partial(self._apply_alarm, False))

    def trigger_collection_events(self, ceids):
        """覆寫：在觸發當下組好報告（值的快照），交給單一佇列依序送出 S6F11。"""
        if not isinstance(ceids, list):
            ceids = [ceids]
        for ceid in ceids:
            if isinstance(ceid, secsgem.gem.CollectionEventId):
                ceid = ceid.value
            name = self.get_ceid_name(ceid)
            link = self.registered_collection_events.get(ceid)
            if link is None or not link.enabled:
                self._history.append("event", {"ceid": ceid, "name": name, "reported": False, "reason": "not linked or not enabled by host"})
                continue
            reports = self._build_collection_event(ceid)
            logged = [
                {"rptid": report["RPTID"], "values": [_jsonable(value.get()) for value in report["V"]]}
                for report in reports
            ]
            self._history.append("event", {"ceid": ceid, "name": name, "reported": True, "reports": _jsonable(logged)})
            self._outbox.put(functools.partial(self._send_event, ceid, name, reports))

    def _is_communicating(self) -> bool:
        return self.communication_state.current == CommunicationState.COMMUNICATING

    def _send_event(self, ceid: int | str, name: str, reports: list) -> None:
        if not self._is_communicating():
            self._history.append("event_ack", {"ceid": ceid, "name": name, "sent": False, "reason": "not communicating"})
            return
        response = self.send_and_waitfor_response(
            self.stream_function(6, 11)({"DATAID": 1, "CEID": ceid, "RPT": reports}),
        )
        ack = None
        if response is not None and response.header.function == 12:
            ack = self.settings.streams_functions.decode(response).get()
        self._history.append("event_ack", {"ceid": ceid, "name": name, "sent": True, "ackc6": _jsonable(ack)})

    def _apply_alarm(self, is_set: bool) -> None:
        alid = gem_ids.ALID_LOW_BATTERY
        alarm = self.alarms[alid]
        communicating = self._is_communicating()
        changed = alarm.set != is_set
        self._history.append(
            "alarm",
            {
                "alid": alid,
                "name": alarm.name,
                "set": is_set,
                "alcd": alarm.code | (gem_ids.ALCD_SET_BIT if is_set else 0),
                "reported": bool(changed and alarm.enabled and communicating),
            },
        )
        if not communicating:
            alarm.set = is_set  # 沒有主機可報告：只更新狀態，主機之後可用 S5F5 查
        elif is_set:
            self.set_alarm(alid)  # secsgem：送 S5F1（ALCD | 0x80）並等 S5F2
        else:
            self.clear_alarm(alid)  # secsgem：送 S5F1（ALCD bit 8 = 0）並等 S5F2

    # ---- 歷史：控制狀態與原始訊息 ---------------------------------------------

    def _on_control_state_entered(self, name: str, _data: dict) -> None:
        self._history.append("control_state", {"from": self._last_control_state, "to": name})
        self._last_control_state = name

    def _on_tapped_message(self, tapped: TappedMessage) -> None:
        if tapped.is_data:
            self._history.append(
                "secs_message",
                {"dir": tapped.direction, "sf": tapped.label, "system": tapped.system, "wbit": tapped.wbit, "sml": tapped.sml},
            )

    # ---- 執行緒 --------------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job = self._outbox.get()
            try:
                if job is None:
                    return
                job()
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("outbox job failed")
                self._history.append("error", {"where": "outbox", "error": repr(exc)})
            finally:
                self._outbox.task_done()

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self.poll_robot()
            except Exception:  # noqa: BLE001
                _LOG.exception("robot poll failed")
