"""測試用主機：包一層 secsgem 的 GemHostHandler（HSMS active）。

只用來測試與 demo，不是完整的主機實作。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import secsgem.common
import secsgem.gem
import secsgem.hsms

import gem_ids


@dataclass(frozen=True)
class HostEvent:
    """主機收到的一筆 S6F11 報告。"""

    ceid: int | str
    rptid: int | str
    values: dict = field(default_factory=dict)  # {VID: value}

    @property
    def name(self) -> str:
        return gem_ids.CEID_NAMES.get(self.ceid, str(self.ceid))


@dataclass(frozen=True)
class HostAlarm:
    """主機收到的一筆 S5F1。"""

    alid: int
    alcd: int
    text: str

    @property
    def is_set(self) -> bool:
        return bool(self.alcd & gem_ids.ALCD_SET_BIT)


def stop_pair(equipment, host: HostClient, settle: float = 0.3) -> None:
    """同一個 process 內的設備＋主機要照這個順序關：先設備、等一下、再主機。

    secsgem 0.3.0 的 disable() 與「對方斷線後自動重連／重新 listen」的執行緒之間有競態：
    先關主機（或兩邊緊接著關）時，實測會留下不會結束的非 daemon 執行緒，甚至讓
    disable() 卡死，process 因此無法結束。先關設備、等主機端確實察覺斷線並進入重連等待，
    再關主機，實測 20/20 乾淨。見 README「限制」。
    """
    equipment.stop()
    time.sleep(settle)
    host.stop()


class HostClient:
    def __init__(self, address: str = "127.0.0.1", port: int = 5000, session_id: int = 0) -> None:
        settings = secsgem.hsms.HsmsSettings(
            address=address,
            port=port,
            session_id=session_id,
            connect_mode=secsgem.hsms.HsmsConnectMode.ACTIVE,
            device_type=secsgem.common.DeviceType.HOST,
        )
        self.handler = secsgem.gem.GemHostHandler(settings)
        self.handler.events.collection_event_received += self._on_collection_event
        self.handler.events.alarm_received += self._on_alarm
        self._cond = threading.Condition()
        self._received: list[HostEvent | HostAlarm] = []

    # ---- 連線 ---------------------------------------------------------------

    def start(self) -> None:
        self.handler.enable()

    def stop(self) -> None:
        self.handler.disable()

    def wait_communicating(self, timeout: float = 10.0) -> bool:
        """等 S1F13/S1F14 完成（通訊建立）。"""
        return self.handler.waitfor_communicating(timeout)

    # ---- 控制狀態 -----------------------------------------------------------

    def go_online(self) -> int | None:
        """S1F17，回傳 ONLACK。"""
        return self.handler.go_online()

    def go_offline(self) -> int | None:
        """S1F15，回傳 OFLACK。"""
        return self.handler.go_offline()

    # ---- 事件報告設定 --------------------------------------------------------

    def define_report(self, rptid: int, vids: list[int]) -> int:
        """S2F33，回傳 DRACK。"""
        ack = self._transact(self.handler.stream_function(2, 33)({"DATAID": 0, "DATA": [{"RPTID": rptid, "VID": vids}]}))
        if ack == 0:
            self.handler.report_subscriptions[rptid] = vids  # GemHostHandler 解 S6F11 時要查這張表
        return ack

    def link_report(self, ceid: int, rptids: list[int]) -> int:
        """S2F35，回傳 LRACK。"""
        return self._transact(self.handler.stream_function(2, 35)({"DATAID": 0, "DATA": [{"CEID": ceid, "RPTID": rptids}]}))

    def enable_event(self, ceids: list[int], enable: bool = True) -> int:
        """S2F37，回傳 ERACK。"""
        return self._transact(self.handler.stream_function(2, 37)({"CEED": enable, "CEID": ceids}))

    def enable_move_events(self) -> tuple[int, int, int]:
        """定義「站名」報告，連結到 MOVE_STARTED 與 ARRIVED 並啟用。回傳 (DRACK, LRACK, ERACK)。"""
        ceids = [gem_ids.CEID_MOVE_STARTED, gem_ids.CEID_ARRIVED]
        drack = self.define_report(gem_ids.RPTID_STATION, [gem_ids.DVID_STATION])
        lrack = self._transact(
            self.handler.stream_function(2, 35)(
                {"DATAID": 0, "DATA": [{"CEID": ceid, "RPTID": [gem_ids.RPTID_STATION]} for ceid in ceids]},
            ),
        )
        erack = self.enable_event(ceids)
        return drack, lrack, erack

    # ---- 遠端命令 -----------------------------------------------------------

    def send_remote_command(self, rcmd: str, params: list[tuple[str, Any]] | None = None) -> int:
        """S2F41，回傳 S2F42 的 HCACK。"""
        reply = self.handler.send_remote_command(rcmd, [list(param) for param in (params or [])])
        if reply is None:
            raise TimeoutError("no reply to S2F41")
        return reply.HCACK.get()

    def move(self, station: str) -> int:
        return self.send_remote_command(gem_ids.RCMD_MOVE, [(gem_ids.CPNAME_STATION, station)])

    # ---- 收到的事件與警報 ----------------------------------------------------

    @property
    def received(self) -> list[HostEvent | HostAlarm]:
        """依收到順序排列的事件與警報。"""
        with self._cond:
            return list(self._received)

    def events(self) -> list[HostEvent]:
        return [item for item in self.received if isinstance(item, HostEvent)]

    def alarms(self) -> list[HostAlarm]:
        return [item for item in self.received if isinstance(item, HostAlarm)]

    def wait_for(self, predicate: Callable[[HostEvent | HostAlarm], bool], timeout: float = 5.0):
        """等到第一筆符合條件的事件／警報（包含已經收到的），逾時回 None。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for item in self._received:
                    if predicate(item):
                        return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def wait_for_event(self, ceid: int, timeout: float = 5.0) -> HostEvent | None:
        return self.wait_for(lambda item: isinstance(item, HostEvent) and item.ceid == ceid, timeout)

    def wait_for_alarm(self, alid: int, is_set: bool, timeout: float = 5.0) -> HostAlarm | None:
        return self.wait_for(
            lambda item: isinstance(item, HostAlarm) and item.alid == alid and item.is_set == is_set,
            timeout,
        )

    # ---- 內部 ---------------------------------------------------------------

    def _transact(self, function) -> int:
        reply = self.handler.send_and_waitfor_response(function)
        if reply is None:
            raise TimeoutError(f"no reply to S{function.stream}F{function.function}")
        return self.handler.settings.streams_functions.decode(reply).get()

    def _on_collection_event(self, data: dict) -> None:
        event = HostEvent(
            ceid=data["ceid"].get(),
            rptid=data["rptid"].get(),
            values={item["dvid"]: item["value"] for item in data["values"]},
        )
        with self._cond:
            self._received.append(event)
            self._cond.notify_all()

    def _on_alarm(self, data: dict) -> None:
        alarm = HostAlarm(alid=data["alid"].get(), alcd=data["code"].get(), text=data["text"].get())
        with self._cond:
            self._received.append(alarm)
            self._cond.notify_all()
