"""Guardian：冪等、速率、fail closed、競態、核准、稽核鏈、MCP 工具面。

大部分用確定性的假主機端（SimHostPort）；執行路徑、競態、斷線另外用真的 HSMS（rig fixture）驗證。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

import approve
import gem_ids
import guardian as guardian_module
import mcp_guardian_server
import run_scenarios
from guardian import GemHostPort, Guardian, SimHostPort
from history import History, verify_file
from host_client import HostClient
from policy import load_policy
from request_store import RequestStore

ROOT = Path(guardian_module.__file__).resolve().parent
POLICY = load_policy()
TRIP = 10.0


class Clock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class Bench:
    def __init__(self, tmp_path, host=None, **kwargs) -> None:
        self.clock = Clock()
        self.port = host if host is not None else SimHostPort()
        self.audit_path = tmp_path / "audit.jsonl"
        self.store = RequestStore(tmp_path / "requests.sqlite")
        self.guardian = Guardian(POLICY, self.port, History(self.audit_path), self.store, trip_cost=TRIP, clock=self.clock, **kwargs)

    def move(self, station, request_id, **kwargs):
        return self.guardian.request_move("ROBOT01", station, request_id, **kwargs)

    def audit(self, kind=None) -> list[dict]:
        rows = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        return [row["data"] for row in rows if kind is None or row["kind"] == kind]


@pytest.fixture
def bench(tmp_path):
    return Bench(tmp_path)


# ---- 基本路徑 ------------------------------------------------------------------------------------


def test_allowed_move_is_sent_once_and_completion_is_recorded(bench):
    result = bench.move("A", "r1")
    assert (result["verdict"], result["reason"], result["status"]) == ("EXECUTE", "OK", "IN_PROGRESS")
    assert result["executed"] is True and result["hcack"] == 4 and result["outcome"] == "gate_allowed_equipment_accepted"
    assert bench.port.sent == ["A"]

    bench.port.arrive()
    status = bench.guardian.get_request_status("r1")
    assert status["found"] is True and status["status"] == "COMPLETED"
    finished = bench.audit("move_finished")
    assert finished == [{"request_id": "r1", "station": "A", "event": "ARRIVED", "status": "COMPLETED", "hcack": 4,
                         "policy_sha256": POLICY.sha256}]


def test_rejected_request_sends_nothing(bench):
    bench.port.robot_state = "MOVING"
    result = bench.move("A", "r1")
    assert (result["verdict"], result["reason"], result["executed"], result["hcack"]) == ("REJECT", "BUSY", False, None)
    assert bench.port.sent == []
    assert bench.guardian.get_request_status("r1")["status"] == "REJECTED"
    assert bench.guardian.get_request_status("nope") == {"ok": True, "found": False, "request_id": "nope", "message": "no such request"}
    assert bench.guardian.get_request_status("bad id!")["ok"] is False


def test_dry_run_returns_the_decision_without_sending_or_recording(bench):
    result = bench.move("A", "r1", dry_run=True)
    assert (result["verdict"], result["reason"], result["status"], result["dry_run"]) == ("EXECUTE", "OK", "DRY_RUN", True)
    assert result["executed"] is False and bench.port.sent == [] and bench.store.count() == 0
    assert bench.guardian.get_request_status("r1")["found"] is False
    assert bench.move("RESTRICTED", "r2", dry_run=True)["verdict"] == "ESCALATE"
    assert bench.store.with_status("PENDING_APPROVAL") == []  # dry_run 不會進待核准佇列
    assert bench.move("A", "r1")["executed"] is True  # 之後可以用同一個 request_id 真的送
    assert [row["dry_run"] for row in bench.audit("gate_decision")] == [True, True, False]
    assert bench.move("A", "r3", dry_run="yes")["reason"] == "BAD_PARAM"


# ---- 冪等 ----------------------------------------------------------------------------------------


def test_same_request_id_ten_times_sends_one_move(bench):
    results = [bench.move("A", "same-id") for _ in range(10)]
    assert bench.port.sent == ["A"]
    assert results[0]["replayed"] is False
    assert all(result == {**results[0], "replayed": True} for result in results[1:])
    assert len(bench.audit("execution")) == 1 and len(bench.audit("request_replay")) == 9


def test_request_id_reused_with_other_parameters_is_rejected_and_first_result_kept(bench):
    first = bench.move("A", "r1")
    other = bench.move("B", "r1")
    assert (other["verdict"], other["reason"]) == ("REJECT", "BAD_PARAM") and "different parameters" in other["message"]
    assert bench.port.sent == ["A"]
    assert bench.guardian.get_request_status("r1")["verdict"] == first["verdict"] == "EXECUTE"


def test_rejections_are_idempotent_too(bench):
    bench.port.robot_state = "MOVING"
    first = bench.move("A", "r1")
    bench.port.robot_state = "IDLE"  # 狀態變好了，但同一個 request_id 仍回第一次的結果
    assert bench.move("A", "r1") == {**first, "replayed": True}
    assert bench.port.sent == []
    assert bench.move("A", "r2")["executed"] is True  # 要重試就用新的 request_id


# ---- 速率限制與重複偵測（注入時鐘） ---------------------------------------------------------------


def run_move(bench, station, request_id):
    result = bench.move(station, request_id)
    bench.port.arrive()
    return result["verdict"], result["reason"]


def test_rate_limit_and_duplicate_windows_follow_the_injected_clock(bench):
    assert run_move(bench, "A", "r1") == ("EXECUTE", "OK")  # t = 0
    bench.clock.now += 59
    assert run_move(bench, "A", "r2") == ("REJECT", "DUPLICATE")  # 同站 59 秒內
    bench.clock.now += 1
    assert run_move(bench, "A", "r3") == ("EXECUTE", "OK")  # t = 60：剛好滿 60 秒
    bench.clock.now += 10
    assert run_move(bench, "B", "r4") == ("EXECUTE", "OK")  # t = 70：5 分鐘內第 3 次
    bench.clock.now += 10
    assert run_move(bench, "CHARGER", "r5") == ("REJECT", "RATE_LIMIT")  # t = 80：第 4 次
    bench.clock.now += 219
    assert run_move(bench, "CHARGER", "r6") == ("REJECT", "RATE_LIMIT")  # t = 299：r1 還在視窗內
    bench.clock.now += 1
    assert run_move(bench, "CHARGER", "r7") == ("EXECUTE", "OK")  # t = 300：r1 滑出視窗
    assert run_move(bench, "A", "r8") == ("REJECT", "RATE_LIMIT")  # r3、r4、r7 又滿 3 次
    assert bench.port.sent == ["A", "A", "B", "CHARGER"]


# ---- fail closed ---------------------------------------------------------------------------------


def test_host_down_is_rejected_and_leaves_nothing_queued(bench):
    bench.port.up = False
    result = bench.move("A", "r1")
    assert (result["verdict"], result["reason"], result["status"]) == ("REJECT", "HOST_DOWN", "REJECTED")
    assert bench.port.sent == [] and bench.store.with_status("PENDING_APPROVAL") == []
    assert bench.store.with_status("SENDING") == [] and bench.store.with_status("IN_PROGRESS") == []

    bench.port.up = True  # 恢復之後，先前被拒絕的請求不會被補送
    bench.guardian.poll()
    assert bench.guardian.get_request_status("r1")["status"] == "REJECTED"
    assert bench.port.sent == []


class FlakyPort(SimHostPort):
    """快照拿得到，但送出時不通（或丟例外）。"""

    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.attempts = 0

    def send_move(self, station):
        self.attempts += 1
        if self.mode == "raise":
            raise ConnectionError("link dropped")
        return None  # 逾時：沒有等到 S2F42


@pytest.mark.parametrize("mode", ["timeout", "raise"])
def test_send_failure_after_gate_approval_is_recorded_and_never_retried(tmp_path, mode):
    bench = Bench(tmp_path, host=FlakyPort(mode))
    result = bench.move("A", "r1")
    assert (result["verdict"], result["status"], result["executed"], result["hcack"]) == ("EXECUTE", "SEND_FAILED", False, None)
    assert result["outcome"] == "gate_allowed_send_failed"
    for _ in range(3):  # 重送同一個 request_id、輪詢，都不會再送
        bench.move("A", "r1")
        bench.guardian.poll()
    assert bench.port.attempts == 1
    assert bench.audit("execution")[0]["outcome"] == "gate_allowed_send_failed"


class BrokenPort:
    def snapshot(self):
        raise RuntimeError("boom")

    def send_move(self, station):
        raise AssertionError("must not be called")

    def arrived_stations(self):
        raise RuntimeError("boom")


def test_exceptions_from_the_host_are_a_rejection_not_a_crash(tmp_path):
    bench = Bench(tmp_path, host=BrokenPort())
    assert bench.move("A", "r1")["reason"] == "HOST_DOWN"
    for bad in (None, 5, ["A"], {"station": "A"}, b"A"):
        assert bench.move(bad, "r2")["reason"] == "BAD_PARAM"
    assert bench.guardian.request_move(None, None, None)["verdict"] == "REJECT"


def test_gem_host_port_fails_closed_when_not_communicating_or_too_slow():
    class Host:
        communicating = False
        calls = 0

        def is_communicating(self):
            return self.communicating

        def read_status(self):
            self.calls += 1
            time.sleep(1.0)
            return {"control_state": "ONLINE_REMOTE", "robot_state": "IDLE", "battery": 100.0}

        def move(self, station):
            self.calls += 1
            raise TimeoutError("no reply to S2F41")

        def events(self):
            return []

    host = Host()
    port = GemHostPort(host, call_timeout=0.2)
    assert port.snapshot() is None and port.send_move("A") is None and host.calls == 0  # 沒連線：根本不送
    host.communicating = True
    started = time.monotonic()
    assert port.snapshot() is None  # 太慢：當成不通
    assert time.monotonic() - started < 0.8
    assert port.send_move("A") is None  # 逾時例外：當成不通


# ---- 真的 HSMS：執行、冪等、競態、斷線 -------------------------------------------------------------


def gem_bench(rig, tmp_path, **kwargs) -> Bench:
    rig.go_online_remote()
    return Bench(tmp_path, host=GemHostPort(rig.host, call_timeout=5.0), **kwargs)


def s2f41_count(rig) -> int:
    return sum(1 for record in rig.history.records() if record["kind"] == "host_command")


def test_gem_execute_records_hcack_and_arrived(rig, tmp_path):
    bench = gem_bench(rig, tmp_path)
    results = [bench.move("A", "gem-1") for _ in range(10)]  # 同一個 request_id 送 10 次
    assert (results[0]["verdict"], results[0]["hcack"], results[0]["status"]) == ("EXECUTE", 4, "IN_PROGRESS")
    assert s2f41_count(rig) == 1  # 設備端只收到一次 S2F41
    assert rig.robot.target() == "A"

    assert bench.guardian.get_request_status("gem-1")["status"] == "IN_PROGRESS"
    rig.advance(5.0)
    assert rig.host.wait_for_event(gem_ids.CEID_ARRIVED) is not None
    assert bench.guardian.get_request_status("gem-1")["status"] == "COMPLETED"
    snapshot = bench.audit("gate_decision")[0]["snapshot"]
    assert snapshot == {"control_state": "ONLINE_REMOTE", "robot_state": "IDLE", "battery": 100.0, "trip_cost": TRIP}
    assert [row["hcack"] for row in bench.audit("execution")] == [4]
    assert bench.audit("move_finished")[0]["event"] == "ARRIVED"


def test_gem_state_rejections_never_reach_the_equipment(rig, tmp_path):
    bench = gem_bench(rig, tmp_path)
    rig.equipment.control_switch_online_local()
    assert bench.move("A", "g1")["reason"] == "NOT_REMOTE"
    rig.equipment.control_switch_online_remote()
    assert bench.move("NOWHERE", "g2")["reason"] == "STATION_NOT_ALLOWED"
    assert bench.move("RESTRICTED", "g3")["verdict"] == "ESCALATE"
    assert bench.move("A", "g4")["executed"] is True
    assert bench.move("B", "g5")["reason"] == "BUSY"
    assert s2f41_count(rig) == 1


def test_gem_race_gate_allows_then_equipment_refuses(rig, tmp_path):
    """閘門看到 IDLE 而放行；在送出之前機器人變成 MOVING -> 設備端回 HCACK=2。"""
    bench = gem_bench(rig, tmp_path, before_send=lambda: rig.robot.goto("B"))
    result = bench.move("A", "race-1")

    assert (result["verdict"], result["reason"]) == ("EXECUTE", "OK")  # 閘門放行
    assert result["hcack"] == 2 and result["executed"] is False  # 設備拒絕
    assert result["status"] == "EQUIPMENT_REJECTED" and result["outcome"] == "gate_allowed_equipment_rejected"
    assert "equipment refused" in result["message"]
    assert rig.robot.target() == "B"  # 機器人做的是別的事，不是這個請求

    execution = bench.audit("execution")[0]
    assert (execution["gate_verdict"], execution["hcack"], execution["outcome"]) == ("EXECUTE", 2, "gate_allowed_equipment_rejected")
    equipment_reply = [r["data"] for r in rig.history.records() if r["kind"] == "host_command_reply"][-1]
    assert equipment_reply["hcack"] == 2
    rig.advance(5.0)
    assert bench.guardian.get_request_status("race-1")["status"] == "EQUIPMENT_REJECTED"  # 不會被當成完成


def test_gem_equipment_gone_is_host_down_and_nothing_is_sent_later(rig, tmp_path):
    bench = gem_bench(rig, tmp_path)
    assert bench.move("A", "ok-1")["executed"] is True
    rig.advance(5.0)
    before = s2f41_count(rig)

    rig.equipment.stop()  # 設備端斷線
    deadline = time.monotonic() + 5
    while rig.host.is_communicating() and time.monotonic() < deadline:
        time.sleep(0.05)
    bench.clock.now += 120
    result = bench.move("B", "down-1")
    assert (result["verdict"], result["reason"], result["executed"]) == ("REJECT", "HOST_DOWN", False)
    assert bench.store.with_status("PENDING_APPROVAL") == [] and bench.store.with_status("SENDING") == []
    assert rig.host.handler.protocol._send_queue.empty()  # noqa: SLF001 - secsgem 的送出佇列裡也沒有殘留
    assert s2f41_count(rig) == before


# ---- 核准：只有私鑰簽得出來 ------------------------------------------------------------------------


@pytest.fixture
def keys():
    private, public = approve.generate_keypair()
    return {"private": private, "public": base64.b64decode(public)}


def test_escalated_request_waits_until_a_signed_approval_then_rechecks_and_executes(tmp_path, keys):
    bench = Bench(tmp_path, approver_public_key=keys["public"])
    result = bench.move("RESTRICTED", "esc-1")
    assert (result["verdict"], result["status"], result["executed"]) == ("ESCALATE", "PENDING_APPROVAL", False)
    bench.guardian.poll()
    assert bench.port.sent == []  # 沒有核准就不會動

    bench.clock.now += 30
    assert "approve recorded" in approve.sign_decision(bench.store, "esc-1", "approve", keys["private"], now=bench.clock.now)
    status = bench.guardian.get_request_status("esc-1")
    assert (status["verdict"], status["reason"], status["status"], status["hcack"]) == ("EXECUTE", "APPROVED", "IN_PROGRESS", 4)
    assert bench.port.sent == ["RESTRICTED"]
    assert bench.audit("approval_received")[0]["signature_valid"] is True
    assert [row["phase"] for row in bench.audit("gate_decision")] == ["request", "after_approval"]
    bench.guardian.poll()
    assert bench.port.sent == ["RESTRICTED"]  # 核准只用一次


def test_approval_does_not_override_preconditions(tmp_path, keys):
    bench = Bench(tmp_path, approver_public_key=keys["public"])
    bench.move("RESTRICTED", "esc-1")
    bench.port.robot_state = "ALARM"
    approve.sign_decision(bench.store, "esc-1", "approve", keys["private"], now=bench.clock.now)
    status = bench.guardian.get_request_status("esc-1")
    assert (status["verdict"], status["reason"], status["status"]) == ("REJECT", "ALARM", "REJECTED")
    assert "approved by a human" in status["message"] and bench.port.sent == []


def test_denied_and_expired_approvals(tmp_path, keys):
    bench = Bench(tmp_path, approver_public_key=keys["public"])
    bench.move("RESTRICTED", "esc-deny")
    approve.sign_decision(bench.store, "esc-deny", "deny", keys["private"], now=bench.clock.now)
    assert bench.guardian.get_request_status("esc-deny")["status"] == "APPROVAL_DENIED"

    bench.clock.now += 120
    bench.move("RESTRICTED", "esc-late")
    bench.clock.now += 601  # 超過政策的 600 秒
    assert bench.guardian.get_request_status("esc-late")["status"] == "APPROVAL_EXPIRED"
    with pytest.raises(ValueError, match="not waiting"):
        approve.sign_decision(bench.store, "esc-late", "approve", keys["private"], now=bench.clock.now)
    assert bench.port.sent == []


def test_forged_or_unverifiable_approvals_are_ignored(tmp_path, keys):
    other_private, _ = approve.generate_keypair()
    bench = Bench(tmp_path, approver_public_key=keys["public"])

    bench.move("RESTRICTED", "forged-key")  # 用別的私鑰簽
    approve.sign_decision(bench.store, "forged-key", "approve", other_private, now=bench.clock.now)
    bench.clock.now += 61
    bench.move("RESTRICTED", "forged-row")  # 直接往儲存塞一筆沒有有效簽章的「核准」（模擬能寫檔的攻擊者）
    bench.store.add_approval("forged-row", "approve", bench.clock.now, base64.b64encode(b"x" * 64).decode())
    bench.clock.now += 61
    bench.move("RESTRICTED", "replayed-sig")  # 把別筆請求的有效簽章搬過來用
    bench.clock.now += 61
    bench.move("RESTRICTED", "donor")
    approve.sign_decision(bench.store, "donor", "deny", keys["private"], now=bench.clock.now)
    donor_signature = bench.store.unprocessed_approvals()[-1][3]
    bench.store.add_approval("replayed-sig", "approve", bench.clock.now, donor_signature)

    bench.guardian.poll()
    assert bench.port.sent == []
    for request_id in ("forged-key", "forged-row", "replayed-sig"):
        assert bench.guardian.get_request_status(request_id)["status"] == "PENDING_APPROVAL"
    validity = {row["request_id"]: row["signature_valid"] for row in bench.audit("approval_received")}
    assert validity == {"forged-key": False, "forged-row": False, "replayed-sig": False, "donor": True}

    (tmp_path / "nokey").mkdir()
    no_key = Bench(tmp_path / "nokey", approver_public_key=None)  # 沒設定公鑰：任何核准都無效
    no_key.move("RESTRICTED", "esc-1")
    approve.sign_decision(no_key.store, "esc-1", "approve", keys["private"], now=no_key.clock.now)
    assert no_key.guardian.get_request_status("esc-1")["status"] == "PENDING_APPROVAL" and no_key.port.sent == []


# ---- 稽核鏈 --------------------------------------------------------------------------------------


def test_every_request_is_audited_with_the_policy_hash(bench):
    bench.move("A", "r1")
    bench.port.arrive()
    bench.move("A;B", "r2")
    bench.move("RESTRICTED", "r3")
    bench.move("B", "r4", dry_run=True)
    bench.port.up = False
    bench.move("B", "r5")
    bench.move("x" * 5000, "r6")

    decisions = bench.audit("gate_decision")
    assert [(d["request_id"], d["verdict"], d["reason"], d["dry_run"]) for d in decisions] == [
        ("r1", "EXECUTE", "OK", False),
        ("r2", "REJECT", "BAD_PARAM", False),
        ("r3", "ESCALATE", "APPROVAL_REQUIRED", False),
        ("r4", "EXECUTE", "OK", True),
        ("r5", "REJECT", "HOST_DOWN", False),
        ("r6", "REJECT", "BAD_PARAM", False),
    ]
    assert all(row["policy_sha256"] == POLICY.sha256 for row in bench.audit() if "policy_sha256" in row)
    assert decisions[0]["snapshot"]["battery"] == 100.0 and decisions[4]["snapshot"] is None
    assert decisions[1]["params"]["station"] == {"repr": "'A;B'", "length": 3, "type": "str"}
    assert decisions[5]["params"]["station"]["length"] == 5000 and len(decisions[5]["params"]["station"]["repr"]) < 200
    assert bench.audit("execution")[0]["hcack"] == 4
    assert verify_file(bench.audit_path) is True


def test_scenarios_match_expected_and_audit_chain_detects_tampering(tmp_path):
    audit_path = tmp_path / "scenarios_audit.jsonl"
    outcomes = run_scenarios.run_all(audit_path, tmp_path)
    scenarios = run_scenarios.load_scenarios()

    assert 28 <= len(scenarios) <= 45
    assert {s["type"] for s in scenarios} == set(run_scenarios.TYPES)
    mismatches = [(o.scenario_id, o.path, o.problems) for o in outcomes if not o.ok]
    assert mismatches == []  # 混淆表與 expected 完全一致
    assert {o.path for o in outcomes} == {"gate", "mcp"} and len(outcomes) >= 2 * len(scenarios) - 4
    for outcome in outcomes:
        if outcome.path == "mcp" and outcome.actual[0] != "EXECUTE":
            assert outcome.sent == 0

    assert verify_file(audit_path) is True
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if '"verdict":"REJECT"' in line)
    lines[index] = lines[index].replace('"verdict":"REJECT"', '"verdict":"EXECUTE"')  # 竄改一筆
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    assert verify_file(audit_path) is False


# ---- MCP 工具面：沒有核准、沒有改政策 ---------------------------------------------------------------


def test_mcp_tool_list_has_no_approval_or_policy_tools(bench):
    async def go():
        async with Client(mcp_guardian_server.build_server(bench.guardian)) as client:
            tools = (await client.list_tools()).tools
            ok = await client.call_tool("request_move", {"equipment_id": "ROBOT01", "station": "A", "request_id": "m1"})
            odd = await client.call_tool("request_move", {"equipment_id": "ROBOT01", "station": 5, "request_id": "m2"})
            status = await client.call_tool("get_request_status", {"request_id": "m1"})
            missing = [await client.call_tool(name, {"request_id": "esc"}) for name in ("approve_request", "approve", "reload_policy", "set_policy")]
            return tools, ok, odd, status, missing

    tools, ok, odd, status, missing = asyncio.run(go())
    assert {tool.name for tool in tools} == {"request_move", "get_request_status"}
    forbidden = ("approv", "deny", "policy", "reload", "override", "force", "bypass", "admin", "unlock", "sql", "rcmd", "command")
    for tool in tools:
        assert not any(word in tool.name.lower() for word in forbidden)
        assert tool.description == mcp_guardian_server.DESCRIPTIONS[tool.name]
        assert set(tool.input_schema["properties"]) <= {"equipment_id", "station", "request_id", "dry_run"}
    assert ok.structured_content["executed"] is True and status.structured_content["status"] == "IN_PROGRESS"
    assert odd.is_error is not True and odd.structured_content["reason"] == "BAD_PARAM"  # 型別不對也進閘門並留紀錄
    assert all(result.is_error for result in missing)
    assert len(bench.audit("gate_decision")) == 2


def test_guardian_has_no_approval_or_policy_mutation_api():
    public = {name for name in dir(Guardian) if not name.startswith("_")}
    assert public == {"request_move", "get_request_status", "poll", "policy_sha256"}
    for path in ("guardian.py", "mcp_guardian_server.py", "gate.py", "request_store.py"):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert "load_policy(" not in source or path == "mcp_guardian_server.py"
        assert ".sign(" not in source and "Ed25519PrivateKey" not in source  # 這些模組簽不出核准
    assert (ROOT / "mcp_guardian_server.py").read_text(encoding="utf-8").count("load_policy(") == 1  # 只在啟動時載入一次
    for path in ("gate.py", "guardian.py", "policy.py", "request_store.py"):  # 閘門這一側都不認得 MCP
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(module.split(".")[0] == "mcp" for module in modules), path


def test_mcp_server_process_cannot_hold_the_approver_credential(tmp_path):
    # 1. 程式碼裡只有「看到私鑰就拒絕啟動」這一處會碰私鑰的環境變數
    for path in ("mcp_guardian_server.py", "guardian.py", "gate.py", "request_store.py", "policy.py"):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert "GUARDIAN_APPROVER_PRIVATE_KEY" not in source or path == "mcp_guardian_server.py"
    assert (ROOT / "mcp_guardian_server.py").read_text(encoding="utf-8").count("PRIVATE_KEY_ENV") == 3  # 定義、檢查、錯誤訊息

    # 2. 環境裡有私鑰時，server 程序拒絕啟動
    private, _ = approve.generate_keypair()
    command = [sys.executable, str(ROOT / "mcp_guardian_server.py"), "--equipment-port", "1", "--store", str(tmp_path / "s.sqlite"),
               "--audit", str(tmp_path / "a.jsonl")]
    completed = subprocess.run(  # noqa: S603
        command, env={**os.environ, approve.PRIVATE_KEY_ENV: private}, capture_output=True, text=True, timeout=60, cwd=ROOT, check=False,
    )
    assert completed.returncode == 2 and "refusing to start" in completed.stderr
    assert private not in completed.stderr and private not in completed.stdout
    assert not (tmp_path / "s.sqlite").exists()

    # 3. approve.py 沒有私鑰就簽不了
    store = RequestStore(tmp_path / "pending.sqlite")
    store.close()
    env = {key: value for key, value in os.environ.items() if key != approve.PRIVATE_KEY_ENV}
    denied = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "approve.py"), "approve", "--store", str(tmp_path / "pending.sqlite"), "some-id"],
        env=env, capture_output=True, text=True, timeout=60, cwd=ROOT, check=False,
    )
    assert denied.returncode == 2 and "is not set" in denied.stderr


def test_approve_cli_is_a_separate_process_that_signs_with_the_env_credential(tmp_path, keys):
    bench = Bench(tmp_path, approver_public_key=keys["public"])
    bench.move("RESTRICTED", "cli-1")
    env = {**os.environ, approve.PRIVATE_KEY_ENV: keys["private"]}
    run = lambda *args: subprocess.run(  # noqa: E731, S603
        [sys.executable, str(ROOT / "approve.py"), *args], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT, check=False,
    )
    listing = run("list", "--store", str(bench.store.path))
    assert listing.returncode == 0 and "cli-1  ROBOT01 -> RESTRICTED" in listing.stdout
    approved = run("approve", "--store", str(bench.store.path), "cli-1")
    assert approved.returncode == 0 and keys["private"] not in approved.stdout + approved.stderr
    assert run("approve", "--store", str(bench.store.path), "cli-1").returncode == 1  # 已經有決定了
    assert run("approve", "--store", str(bench.store.path), "no-such").returncode == 1

    assert bench.guardian.get_request_status("cli-1")["status"] == "IN_PROGRESS"
    assert bench.port.sent == ["RESTRICTED"]


def test_guardian_server_over_stdio_fails_closed_when_no_equipment_is_listening(tmp_path):
    """真的 stdio 子行程；設備不存在 -> REJECT/HOST_DOWN，沒有任何東西被排隊。"""
    from conftest import free_port

    server = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_guardian_server.py"), "--equipment-port", str(free_port()), "--store", str(tmp_path / "s.sqlite"),
              "--audit", str(tmp_path / "a.jsonl")],
        cwd=str(ROOT),
    )

    async def go():
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool("request_move", {"equipment_id": "ROBOT01", "station": "A", "request_id": "s1"})
            status = await client.call_tool("get_request_status", {"request_id": "s1"})
            return names, result.structured_content, status.structured_content

    names, result, status = asyncio.run(go())
    assert names == {"request_move", "get_request_status"}
    assert (result["verdict"], result["reason"], result["executed"]) == ("REJECT", "HOST_DOWN", False)
    assert status["status"] == "REJECTED"
    assert verify_file(tmp_path / "a.jsonl") is True
    store = RequestStore(tmp_path / "s.sqlite")
    assert store.count() == 1 and store.with_status("PENDING_APPROVAL") == []
    store.close()
