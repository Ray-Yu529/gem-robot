"""動作閘門的判定：純函式，確定性，預設拒絕。

    decide(request, snapshot, policy, now, recent=(), approval_granted=False) -> Decision

* 只看傳進來的值：不讀檔、不連線、不看時鐘、沒有全域狀態。同樣的輸入永遠得到同樣的輸出。
* 這個模組只 import 標準函式庫。它不認得 MCP、不認得 GEM、不認得資料庫；上層（guardian）只呼叫它。
* **預設拒絕**：只有走完全部檢查才會回 EXECUTE 或 ESCALATE。欄位缺失、型別不對、快照拿不到、
  任何沒預期到的情況（包括這個函式自己丟例外），一律 REJECT。

檢查順序（第一個不通過的決定原因碼）：
  請求格式 -> 設備 -> 命令 -> 站點 -> （意圖綁定）-> 快照 -> 控制狀態 -> 機器人狀態 -> 電量 -> 重複 -> 速率 -> 站點分級
"""

from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


class Verdict(str, enum.Enum):
    EXECUTE = "EXECUTE"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"


class Reason(str, enum.Enum):
    OK = "OK"  # EXECUTE：auto 站，前置條件都成立
    APPROVED = "APPROVED"  # EXECUTE：escalate 站，已有人工核准，前置條件重新檢查後仍成立
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"  # ESCALATE
    BAD_PARAM = "BAD_PARAM"  # 請求欄位缺失、多餘、型別或格式不對
    UNKNOWN_EQUIPMENT = "UNKNOWN_EQUIPMENT"
    RCMD_NOT_ALLOWED = "RCMD_NOT_ALLOWED"
    STATION_NOT_ALLOWED = "STATION_NOT_ALLOWED"
    NOT_AUTHORIZED_BY_TASK = "NOT_AUTHORIZED_BY_TASK"  # 意圖綁定：這個動作不在這次任務被授權的範圍內
    HOST_DOWN = "HOST_DOWN"  # 拿不到設備快照，或 GEM 連線不通
    SNAPSHOT_INVALID = "SNAPSHOT_INVALID"  # 快照欄位缺失、型別不對或過期
    NOT_REMOTE = "NOT_REMOTE"
    ALARM = "ALARM"
    BUSY = "BUSY"
    LOW_BATTERY = "LOW_BATTERY"
    DUPLICATE = "DUPLICATE"
    RATE_LIMIT = "RATE_LIMIT"
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 判定過程出錯：拒絕


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: Reason

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.EXECUTE


@dataclass(frozen=True)
class RecentRequest:
    """先前的一筆請求（用於速率限制與重複偵測）。"""

    request_id: str
    equipment_id: str
    station: str
    at: float  # 與 now 同一個時鐘
    verdict: str  # "EXECUTE" / "ESCALATE" / "REJECT"


REQUEST_FIELDS = frozenset({"equipment_id", "rcmd", "station", "request_id"})
SNAPSHOT_FIELDS = frozenset(
    {"equipment_id", "communicating", "control_state", "robot_state", "battery", "trip_cost", "taken_at"},
)
# 名稱只接受這個字元集，長度 1-32/64。不去空白、不轉大小寫、不做任何正規化：
# 空字串、空白、分號、換行、控制字元、非 ASCII（含 Unicode 同形字）、超長字串都在這裡被擋掉。
_NAME = re.compile(r"\A[A-Za-z0-9_-]{1,32}\Z")
_REQUEST_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
ROBOT_STATES = frozenset({"IDLE", "MOVING", "ALARM"})


def _reject(reason: Reason) -> Decision:
    return Decision(Verdict.REJECT, reason)


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_ID.match(value) is not None


def decide(  # noqa: PLR0911, PLR0912, PLR0913
    request: Any,
    snapshot: Any,
    policy: Any,
    now: Any,
    recent: Iterable[RecentRequest] = (),
    approval_granted: bool = False,
    authorized_stations: frozenset[str] | None = None,
) -> Decision:
    """回傳判定。永遠不丟例外。

    ``authorized_stations``（意圖綁定，選用）：None = 不啟用；frozenset = 這次任務使用者授權可以去的站
    （空集合 = 這次任務沒有授權任何移動）。由上層以 agent 接觸不到的管道提供。
    """
    try:
        return _decide(request, snapshot, policy, now, recent, approval_granted, authorized_stations)
    except Exception:  # noqa: BLE001 - 無法判定就是拒絕
        return _reject(Reason.INTERNAL_ERROR)


def _decide(request, snapshot, policy, now, recent, approval_granted, authorized_stations) -> Decision:  # noqa: PLR0911, PLR0912
    if approval_granted is not True and approval_granted is not False:
        return _reject(Reason.INTERNAL_ERROR)
    if authorized_stations is not None and type(authorized_stations) is not frozenset:
        return _reject(Reason.INTERNAL_ERROR)
    if not _number(now):
        return _reject(Reason.INTERNAL_ERROR)

    # ---- 請求格式 ----
    if type(request) is not dict or set(request) != REQUEST_FIELDS:
        return _reject(Reason.BAD_PARAM)
    equipment_id, rcmd, station, request_id = (request[k] for k in ("equipment_id", "rcmd", "station", "request_id"))
    if type(equipment_id) is not str or type(rcmd) is not str or type(station) is not str:
        return _reject(Reason.BAD_PARAM)
    if not valid_request_id(request_id) or type(request_id) is not str:
        return _reject(Reason.BAD_PARAM)
    if not _NAME.match(equipment_id) or not _NAME.match(rcmd) or not _NAME.match(station):
        return _reject(Reason.BAD_PARAM)

    # ---- 白名單（字串完全相等） ----
    if equipment_id not in policy.equipment:
        return _reject(Reason.UNKNOWN_EQUIPMENT)
    if rcmd not in policy.allowed_rcmd:
        return _reject(Reason.RCMD_NOT_ALLOWED)
    needs_approval = station in policy.escalate_stations
    if station not in policy.auto_stations and not needs_approval:
        return _reject(Reason.STATION_NOT_ALLOWED)
    if authorized_stations is not None and station not in authorized_stations:
        return _reject(Reason.NOT_AUTHORIZED_BY_TASK)

    # ---- 設備快照 ----
    if snapshot is None:
        return _reject(Reason.HOST_DOWN)
    if type(snapshot) is not dict or set(snapshot) != SNAPSHOT_FIELDS:
        return _reject(Reason.SNAPSHOT_INVALID)
    if snapshot["communicating"] is False:
        return _reject(Reason.HOST_DOWN)
    if snapshot["communicating"] is not True or snapshot["equipment_id"] != equipment_id:
        return _reject(Reason.SNAPSHOT_INVALID)
    control_state, robot_state = snapshot["control_state"], snapshot["robot_state"]
    battery, trip_cost, taken_at = snapshot["battery"], snapshot["trip_cost"], snapshot["taken_at"]
    if type(control_state) is not str or type(robot_state) is not str or robot_state not in ROBOT_STATES:
        return _reject(Reason.SNAPSHOT_INVALID)
    if not (_number(battery) and _number(trip_cost) and _number(taken_at)):
        return _reject(Reason.SNAPSHOT_INVALID)
    if not 0 <= battery <= 100 or trip_cost < 0:
        return _reject(Reason.SNAPSHOT_INVALID)
    if not 0 <= now - taken_at <= policy.snapshot_max_age_seconds:
        return _reject(Reason.SNAPSHOT_INVALID)

    # ---- 前置條件 ----
    if control_state != policy.required_control_state:
        return _reject(Reason.NOT_REMOTE)
    if robot_state == "ALARM":
        return _reject(Reason.ALARM)
    if robot_state != policy.required_robot_state:
        return _reject(Reason.BUSY)
    if battery < trip_cost + policy.battery_safety_margin:
        return _reject(Reason.LOW_BATTERY)

    # ---- 重複與速率（看先前的請求；同一個 request_id 自己不算） ----
    executed = 0
    for item in recent:
        if type(item) is not RecentRequest or not _number(item.at):
            return _reject(Reason.INTERNAL_ERROR)
        if item.request_id == request_id or item.equipment_id != equipment_id:
            continue
        age = now - item.at
        if age < 0:
            return _reject(Reason.INTERNAL_ERROR)  # 來自未來的紀錄：時鐘有問題，拒絕
        if item.station == station and item.verdict in ("EXECUTE", "ESCALATE") and age < policy.duplicate_window_seconds:
            return _reject(Reason.DUPLICATE)
        if item.verdict == "EXECUTE" and age < policy.rate_window_seconds:
            executed += 1
    if executed >= policy.rate_max_moves:
        return _reject(Reason.RATE_LIMIT)

    # ---- 站點分級 ----
    if needs_approval:
        if approval_granted:
            return Decision(Verdict.EXECUTE, Reason.APPROVED)
        return Decision(Verdict.ESCALATE, Reason.APPROVAL_REQUIRED)
    return Decision(Verdict.EXECUTE, Reason.OK)
