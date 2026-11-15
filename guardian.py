"""動作閘門的執行層（guardian）：LLM 只能「請求」，能不能執行由 gate.decide 決定。

    request_move(equipment_id, station, request_id, dry_run=False)
    get_request_status(request_id)

這一層負責判定以外的所有事：取設備快照、冪等、把先前的請求交給 gate 做速率／重複判斷、
寫稽核鏈、放行後經由主機送 S2F41、記錄 HCACK 與事後的 ARRIVED、驗證人工核准的簽章。
它不 import MCP；MCP 層（mcp_guardian_server.py）只呼叫上面兩個方法。

安全性質：
* **預設拒絕 / fail closed**：快照拿不到、主機不通、送出逾時、任何例外，都是 REJECT；不排隊、不重試。
* **最多執行一次**：送出 S2F41 之前先把請求寫進儲存；同一個 request_id 之後一律回第一次的結果。
* **兩層檢查**：閘門放行後設備仍可能拒絕（HCACK 不是 0/4）；那會被記成「閘門放行、設備拒絕」，不算成功。
* **沒有核准路徑**：這個類別沒有任何「核准」方法。它只會驗證 approve.py 用私鑰簽出的核准；
  這個程序只持有公鑰，簽不出有效的核准。
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Callable, Protocol

import gate
from gate import Decision, Reason, RecentRequest, Verdict
from history import History
from policy import Policy
from request_store import PENDING_APPROVAL, RequestStore

RCMD = "MOVE"
ACCEPTED_HCACKS = (0, 4)

# 請求的狀態
REJECTED = "REJECTED"
SENDING = "SENDING"  # 已放行、正要送出（若停在這裡代表程序在送出途中中斷，結果不明）
IN_PROGRESS = "IN_PROGRESS"  # 設備已接受（HCACK 0/4），等 ARRIVED
COMPLETED = "COMPLETED"
ABORTED = "ABORTED"  # 設備接受後進入 ALARM，沒有到站
EQUIPMENT_REJECTED = "EQUIPMENT_REJECTED"  # 閘門放行、設備拒絕
SEND_FAILED = "SEND_FAILED"  # 閘門放行，但送出失敗或沒等到回覆（設備是否收到不明）
APPROVAL_DENIED = "APPROVAL_DENIED"
APPROVAL_EXPIRED = "APPROVAL_EXPIRED"

MESSAGES = {
    Reason.OK: "allowed by policy",
    Reason.APPROVED: "allowed after human approval",
    Reason.APPROVAL_REQUIRED: "this station needs human approval; the request is queued and has not been executed",
    Reason.BAD_PARAM: "a parameter is missing or malformed",
    Reason.UNKNOWN_EQUIPMENT: "this equipment is not managed by the gate",
    Reason.RCMD_NOT_ALLOWED: "this command is not allowed",
    Reason.STATION_NOT_ALLOWED: "this station is not in the allow-list",
    Reason.NOT_AUTHORIZED_BY_TASK: "this move is not part of what the user authorized for this task",
    Reason.HOST_DOWN: "the equipment could not be reached; nothing was sent and nothing is queued",
    Reason.SNAPSHOT_INVALID: "the equipment state could not be determined",
    Reason.NOT_REMOTE: "the equipment is not in ONLINE-REMOTE control state",
    Reason.ALARM: "the robot is in ALARM",
    Reason.BUSY: "the robot is not idle",
    Reason.LOW_BATTERY: "the battery is too low for this trip plus the safety margin",
    Reason.DUPLICATE: "the same move was requested moments ago",
    Reason.RATE_LIMIT: "too many moves in the rate-limit window",
    Reason.INTERNAL_ERROR: "the request could not be evaluated",
}


class HostPort(Protocol):
    """閘門看到的主機端。任何方法丟例外或回 None 都代表「不通」。"""

    def snapshot(self) -> dict[str, Any] | None:
        """{'control_state', 'robot_state', 'battery'}；拿不到回 None。"""

    def send_move(self, station: str) -> int | None:
        """送 S2F41 MOVE，回傳 HCACK；沒送出去或沒等到回覆回 None。"""

    def arrived_stations(self) -> list[str]:
        """到目前為止收到的所有 ARRIVED 的站名（只會變長）。"""


def approval_message(request_id: str, equipment_id: str, station: str, decision: str, policy_sha256: str, signed_at: float) -> bytes:
    """核准簽章涵蓋的內容。approve.py 與 guardian 用同一個函式，確保簽的和驗的是同一串位元組。"""
    return json.dumps(
        {
            "decision": decision,
            "equipment_id": equipment_id,
            "policy_sha256": policy_sha256,
            "request_id": request_id,
            "signed_at": signed_at,
            "station": station,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _clip(value: Any, limit: int = 120) -> Any:
    """稽核紀錄裡的呼叫端輸入：只留 JSON 能表示的形狀，字串截斷並用 repr 保留控制字元。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else repr(value)
    clipped = text[:limit]
    return {"repr": ascii(clipped), "length": len(text), "type": type(value).__name__}


class Guardian:
    def __init__(  # noqa: PLR0913
        self,
        policy: Policy,
        host: HostPort,
        audit: History,
        store: RequestStore,
        *,
        trip_cost: float,
        clock: Callable[[], float] = time.time,
        approver_public_key: bytes | None = None,
        before_send: Callable[[], None] | None = None,
        authorized_stations: frozenset[str] | None = None,
    ) -> None:
        self._policy = policy  # 不可變物件；這個類別沒有任何重新載入或修改政策的方法
        self._host = host
        self._audit = audit
        self._store = store
        self._trip_cost = float(trip_cost)
        self._clock = clock
        self._approver_public_key = approver_public_key
        # 意圖綁定：只能在建構時給定；這個類別沒有任何方法可以事後改它
        self._authorized_stations = authorized_stations
        self._before_send = before_send  # 只給測試用：在放行之後、送出之前插入動作（製造競態）
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = {}  # request_id -> 送出當下已收到的 ARRIVED 筆數
        self._audit.append("guardian_start", {"policy_sha256": policy.sha256, "policy_version": policy.version})

    @property
    def policy_sha256(self) -> str:
        return self._policy.sha256

    # ---- 對外的兩個操作 -------------------------------------------------------

    def request_move(self, equipment_id: Any, station: Any, request_id: Any, dry_run: Any = False) -> dict[str, Any]:
        with self._lock:
            try:
                return self._request_move(equipment_id, station, request_id, dry_run)
            except Exception as exc:  # noqa: BLE001 - 任何意外都是拒絕
                self._audit.append("guardian_error", {"where": "request_move", "error": type(exc).__name__})
                return self._response(Decision(Verdict.REJECT, Reason.INTERNAL_ERROR), request_id, REJECTED)

    def get_request_status(self, request_id: Any) -> dict[str, Any]:
        with self._lock:
            if not gate.valid_request_id(request_id):
                return {"ok": False, "error": {"code": "BAD_PARAM", "message": "request_id is malformed"}}
            try:
                self._poll()
            except Exception as exc:  # noqa: BLE001
                self._audit.append("guardian_error", {"where": "poll", "error": type(exc).__name__})
            record = self._store.get(request_id)
            if record is None:
                return {"ok": True, "found": False, "request_id": request_id, "message": "no such request"}
            return {**record["result"], "found": True}

    def poll(self) -> None:
        """處理已簽核的核准與到站事件。get_request_status 與 request_move 會自動呼叫。"""
        with self._lock:
            self._poll()

    # ---- request_move --------------------------------------------------------

    def _request_move(self, equipment_id: Any, station: Any, request_id: Any, dry_run: Any) -> dict[str, Any]:
        self._poll()
        now = self._clock()
        params = {
            "equipment_id": _clip(equipment_id),
            "station": _clip(station),
            "request_id": request_id if gate.valid_request_id(request_id) else _clip(request_id),
        }
        if dry_run is not True and dry_run is not False:
            decision = Decision(Verdict.REJECT, Reason.BAD_PARAM)
            self._audit_decision(params, None, decision, dry_run=None, now=now)
            return self._response(decision, request_id, REJECTED)

        # 冪等：同一個 request_id 已經處理過，就回第一次的結果，不再判定、不再送出。
        if not dry_run and gate.valid_request_id(request_id):
            existing = self._store.get(request_id)
            if existing is not None:
                same = existing["equipment_id"] == equipment_id and existing["station"] == station
                self._audit.append(
                    "request_replay",
                    {"request_id": request_id, "params": params, "same_params": same, "policy_sha256": self._policy.sha256},
                )
                if same:
                    return {**existing["result"], "replayed": True}
                # 同一個 request_id 卻換了參數：拒絕，而且不改動原本那筆
                decision = Decision(Verdict.REJECT, Reason.BAD_PARAM)
                response = self._response(decision, request_id, REJECTED)
                response["message"] = "this request_id was already used with different parameters"
                return response

        request = {"equipment_id": equipment_id, "rcmd": RCMD, "station": station, "request_id": request_id}
        snapshot = self._take_snapshot(equipment_id, now)
        decision = gate.decide(
            request, snapshot, self._policy, now, self._recent(now), authorized_stations=self._authorized_stations,
        )
        self._audit_decision(params, snapshot, decision, dry_run=dry_run, now=now)

        if dry_run:
            response = self._response(decision, request_id, "DRY_RUN")
            response["dry_run"] = True
            return response
        if decision.reason is Reason.BAD_PARAM:
            return self._response(decision, request_id, REJECTED)  # 連 request_id 都不可信，不存

        status = {Verdict.REJECT: REJECTED, Verdict.ESCALATE: PENDING_APPROVAL, Verdict.EXECUTE: SENDING}[decision.verdict]
        response = self._response(decision, request_id, status)
        stored = self._store.insert(
            {
                "request_id": request_id,
                "equipment_id": equipment_id,
                "station": station,
                "created_at": now,
                "verdict": decision.verdict.value,
                "reason": decision.reason.value,
                "status": status,
                "policy_sha256": self._policy.sha256,
                "result": response,
            },
        )
        if not stored:  # 理論上不會發生（上面已查過）；寧可不執行
            return self._response(Decision(Verdict.REJECT, Reason.INTERNAL_ERROR), request_id, REJECTED)
        if decision.verdict is Verdict.EXECUTE:
            response = self._execute(request_id, station, decision)
        return response

    def _execute(self, request_id: str, station: str, decision: Decision) -> dict[str, Any]:
        """送出 S2F41 並如實記錄設備的回應。呼叫前請求必須已經以 SENDING 存進儲存。"""
        if self._before_send is not None:
            self._before_send()
        cursor = self._arrival_count()
        try:
            hcack = self._host.send_move(station)
        except Exception:  # noqa: BLE001
            hcack = None
        if hcack is None:
            status, outcome = SEND_FAILED, "gate_allowed_send_failed"
        elif hcack in ACCEPTED_HCACKS:
            status, outcome = IN_PROGRESS, "gate_allowed_equipment_accepted"
            self._in_flight[request_id] = cursor
        else:
            status, outcome = EQUIPMENT_REJECTED, "gate_allowed_equipment_rejected"
        response = self._response(decision, request_id, status)
        response.update({"hcack": hcack, "outcome": outcome, "executed": status == IN_PROGRESS})
        if status == SEND_FAILED:
            response["message"] = "the gate allowed the move but it could not be delivered; it will not be retried"
        elif status == EQUIPMENT_REJECTED:
            response["message"] = "the gate allowed the move but the equipment refused it"
        self._store.update(request_id, status=status, result=response, hcack=hcack)
        self._audit.append(
            "execution",
            {
                "request_id": request_id,
                "station": station,
                "gate_verdict": decision.verdict.value,
                "gate_reason": decision.reason.value,
                "hcack": hcack,
                "outcome": outcome,
                "status": status,
                "policy_sha256": self._policy.sha256,
            },
        )
        return response

    # ---- 核准與到站 -----------------------------------------------------------

    def _poll(self) -> None:
        now = self._clock()
        self._poll_arrivals()
        approvals = {row[0]: row for row in self._store.unprocessed_approvals()}
        for record in self._store.with_status(PENDING_APPROVAL):
            request_id = record["request_id"]
            approval = approvals.pop(request_id, None)
            expired = now - record["created_at"] > self._policy.approval_ttl_seconds
            if approval is None:
                if expired:
                    self._finish_pending(record, APPROVAL_EXPIRED, "the approval window has passed")
                continue
            _, decision_text, signed_at, signature = approval
            self._store.mark_approval_processed(request_id)
            valid = self._verify_approval(record, decision_text, signed_at, signature)
            self._audit.append(
                "approval_received",
                {
                    "request_id": request_id,
                    "decision": decision_text,
                    "signature_valid": valid,
                    "expired": expired,
                    "policy_sha256": self._policy.sha256,
                },
            )
            if not valid:
                continue  # 無效的核准等於沒有核准：請求繼續等待，直到過期
            if decision_text == "deny":
                self._finish_pending(record, APPROVAL_DENIED, "a human approver denied this request")
            elif expired:
                self._finish_pending(record, APPROVAL_EXPIRED, "the approval arrived after the approval window")
            else:
                self._execute_approved(record, now)
        for request_id in approvals:  # 針對不存在或不在等待中的請求的核准：記下來，不做任何事
            self._store.mark_approval_processed(request_id)
            self._audit.append("approval_ignored", {"request_id": request_id, "policy_sha256": self._policy.sha256})

    def _execute_approved(self, record: dict[str, Any], now: float) -> None:
        """核准只解除「需要核准」這一項；其他前置條件用現在的設備狀態重新檢查。"""
        request_id, equipment_id, station = record["request_id"], record["equipment_id"], record["station"]
        request = {"equipment_id": equipment_id, "rcmd": RCMD, "station": station, "request_id": request_id}
        snapshot = self._take_snapshot(equipment_id, now)
        decision = gate.decide(
            request, snapshot, self._policy, now, self._recent(now), approval_granted=True,
            authorized_stations=self._authorized_stations,
        )
        params = {"equipment_id": equipment_id, "station": station, "request_id": request_id}
        self._audit_decision(params, snapshot, decision, dry_run=False, now=now, phase="after_approval")
        if decision.verdict is not Verdict.EXECUTE:
            response = self._response(Decision(Verdict.REJECT, decision.reason), request_id, REJECTED)
            response["message"] = "approved by a human, but the preconditions no longer hold: " + response["message"]
            self._store.update(request_id, status=REJECTED, result=response, verdict="REJECT", reason=decision.reason.value)
            return
        self._store.update(
            request_id,
            status=SENDING,
            result=self._response(decision, request_id, SENDING),
            verdict="EXECUTE",
            reason=decision.reason.value,
        )
        self._execute(request_id, station, decision)

    def _finish_pending(self, record: dict[str, Any], status: str, message: str) -> None:
        response = dict(record["result"])
        response.update({"status": status, "message": message, "executed": False})
        self._store.update(record["request_id"], status=status, result=response)
        self._audit.append(
            "approval_closed",
            {"request_id": record["request_id"], "status": status, "policy_sha256": self._policy.sha256},
        )

    def _verify_approval(self, record: dict[str, Any], decision: str, signed_at: float, signature: str) -> bool:
        if self._approver_public_key is None or decision not in ("approve", "deny"):
            return False
        try:
            from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: PLC0415

            message = approval_message(
                record["request_id"], record["equipment_id"], record["station"], decision, record["policy_sha256"], signed_at,
            )
            try:
                Ed25519PublicKey.from_public_bytes(self._approver_public_key).verify(base64.b64decode(signature), message)
            except InvalidSignature:
                return False
            # 簽的必須是「這一版政策下的這一筆」；簽章時間不能早於請求
            return record["policy_sha256"] == self._policy.sha256 and signed_at >= record["created_at"]
        except Exception:  # noqa: BLE001 - 驗不了就是無效
            return False

    def _arrival_count(self) -> int:
        try:
            return len(self._host.arrived_stations())
        except Exception:  # noqa: BLE001
            return 0

    def _poll_arrivals(self) -> None:
        if not self._in_flight:
            return
        try:
            arrived = list(self._host.arrived_stations())
        except Exception:  # noqa: BLE001
            return
        for request_id, cursor in list(self._in_flight.items()):
            record = self._store.get(request_id)
            if record is None:
                del self._in_flight[request_id]
                continue
            if record["station"] in arrived[cursor:]:
                self._close_in_flight(record, COMPLETED, "ARRIVED", "the robot arrived")
                continue
            try:
                snapshot = self._host.snapshot()
            except Exception:  # noqa: BLE001
                snapshot = None
            if snapshot is not None and snapshot.get("robot_state") == "ALARM":
                self._close_in_flight(record, ABORTED, "ALARM", "the move was accepted but the robot went into ALARM before arriving")

    def _close_in_flight(self, record: dict[str, Any], status: str, event: str, message: str) -> None:
        del self._in_flight[record["request_id"]]
        response = dict(record["result"])
        response.update({"status": status, "message": message})
        self._store.update(record["request_id"], status=status, result=response)
        self._audit.append(
            "move_finished",
            {
                "request_id": record["request_id"],
                "station": record["station"],
                "event": event,
                "status": status,
                "hcack": record["hcack"],
                "policy_sha256": self._policy.sha256,
            },
        )

    # ---- 共用 ----------------------------------------------------------------

    def _take_snapshot(self, equipment_id: Any, now: float) -> dict[str, Any] | None:
        """向主機要設備狀態。任何失敗都回 None（gate 會判 HOST_DOWN）。"""
        try:
            raw = self._host.snapshot()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(raw, dict):
            return None
        return {
            "equipment_id": equipment_id,
            "communicating": True,
            "control_state": raw.get("control_state"),
            "robot_state": raw.get("robot_state"),
            "battery": raw.get("battery"),
            "trip_cost": self._trip_cost,
            "taken_at": now,
        }

    def _recent(self, now: float) -> list[RecentRequest]:
        window = max(self._policy.rate_window_seconds, self._policy.duplicate_window_seconds)
        return [RecentRequest(*row) for row in self._store.recent(now - window)]

    def _audit_decision(self, params, snapshot, decision: Decision, *, dry_run, now: float, phase: str = "request") -> None:
        summary = None
        if snapshot is not None:
            summary = {key: snapshot.get(key) for key in ("control_state", "robot_state", "battery", "trip_cost")}
            summary = {key: (value if isinstance(value, (str, int, float, bool)) or value is None else repr(value)[:60])
                       for key, value in summary.items()}
        self._audit.append(
            "gate_decision",
            {
                "phase": phase,
                "request_id": params["request_id"],
                "params": params,
                "rcmd": RCMD,
                "dry_run": dry_run,
                "snapshot": summary,
                "policy_sha256": self._policy.sha256,
                "verdict": decision.verdict.value,
                "reason": decision.reason.value,
                "gate_clock": now,
            },
        )

    def _response(self, decision: Decision, request_id: Any, status: str) -> dict[str, Any]:
        return {
            "ok": True,
            "request_id": request_id if gate.valid_request_id(request_id) else None,
            "verdict": decision.verdict.value,
            "reason": decision.reason.value,
            "status": status,
            "executed": False,
            "hcack": None,
            "dry_run": False,
            "replayed": False,
            "message": MESSAGES[decision.reason],
            "policy_sha256": self._policy.sha256,
        }


class SimHostPort:
    """確定性的假主機端：模擬設備端的 HCACK 前置檢查。給情境測試與 dry-run 用，不經過 HSMS。"""

    def __init__(self, stations: tuple[str, ...] = ("A", "B", "CHARGER", "RESTRICTED")) -> None:
        self.stations = stations
        self.up = True
        self.control_state = "ONLINE_REMOTE"
        self.robot_state = "IDLE"
        self.battery = 100.0
        self.sent: list[str] = []  # 每一次真的送出去的 MOVE
        self.arrived: list[str] = []
        self._moving_to: str | None = None

    def snapshot(self) -> dict[str, Any] | None:
        if not self.up:
            return None
        return {"control_state": self.control_state, "robot_state": self.robot_state, "battery": self.battery}

    def send_move(self, station: str) -> int | None:
        if not self.up:
            return None
        self.sent.append(station)
        if self.control_state != "ONLINE_REMOTE":
            return 2
        if station not in self.stations:
            return 3
        if self.robot_state != "IDLE":
            return 2
        self.robot_state, self._moving_to = "MOVING", station
        return 4

    def arrive(self) -> None:
        """模擬到站。"""
        if self._moving_to is not None:
            self.arrived.append(self._moving_to)
            self.robot_state, self._moving_to = "IDLE", None

    def arrived_stations(self) -> list[str]:
        return list(self.arrived)


class GemHostPort:
    """真的主機端：經由 HostClient（HSMS）。每個呼叫都有時間上限，逾時就當成不通。"""

    def __init__(self, host, call_timeout: float = 5.0) -> None:
        self._host = host
        self._call_timeout = call_timeout

    def _call(self, fn: Callable[[], Any]) -> Any:
        """在另一條執行緒呼叫並限時；沒連線時根本不呼叫（secsgem 在未連線時送訊息會永遠卡住）。"""
        if not self._host.is_communicating():
            return None
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = fn()
            except Exception:  # noqa: BLE001
                box["value"] = None

        thread = threading.Thread(target=run, name="gem_host_port_call", daemon=True)
        thread.start()
        thread.join(self._call_timeout)
        return None if thread.is_alive() else box.get("value")

    def snapshot(self) -> dict[str, Any] | None:
        return self._call(self._host.read_status)

    def send_move(self, station: str) -> int | None:
        return self._call(lambda: self._host.move(station))

    def arrived_stations(self) -> list[str]:
        import gem_ids  # noqa: PLC0415

        return [
            event.values.get(gem_ids.DVID_STATION)
            for event in self._host.events()
            if event.ceid == gem_ids.CEID_ARRIVED
        ]
