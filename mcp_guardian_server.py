"""動作用的 MCP server：只有 ``request_move`` 與 ``get_request_status`` 兩個工具。

* LLM 只能「請求」；放不放行由 gate.decide（確定性、預設拒絕）決定，這裡只是把參數原樣交給 guardian。
* **沒有核准工具、沒有改政策或重載政策的工具。** 核准只能由獨立的 approve.py 用私鑰簽出；
  這個程序只拿公鑰（GUARDIAN_APPROVER_PUBLIC_KEY），而且環境裡若出現私鑰變數會拒絕啟動。
* 與唯讀的 mcp_server.py 是分開的兩個 server：查資料的那個仍然完全沒有寫入路徑。
* 工具描述是靜態文字。

啟動（stdio）::

    python mcp_guardian_server.py --equipment-port 5000 --store guardian.sqlite --audit guardian_audit.jsonl

依 MCP Python SDK 2.3.0 撰寫。
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

from guardian import Guardian

SERVER_NAME = "gem-robot-guardian"
SERVER_VERSION = "0.1.0"
PRIVATE_KEY_ENV = "GUARDIAN_APPROVER_PRIVATE_KEY"  # 只有 approve.py 可以有；這個程序看到它就不啟動
PUBLIC_KEY_ENV = "GUARDIAN_APPROVER_PUBLIC_KEY"

INSTRUCTIONS = (
    "Requests robot moves on simulated equipment. A request is only a request: an independent, deterministic "
    "policy gate decides whether it is executed, and it rejects by default. verdict is EXECUTE, REJECT or ESCALATE "
    "with a reason code. ESCALATE means the request waits for a human approver; there is no tool to approve, "
    "retry around, or change the policy. Report rejections to the user as they are."
)

DESCRIPTIONS = {
    "request_move": (
        "Request that one equipment's robot move to a station. The policy gate decides: verdict EXECUTE (sent to the "
        "equipment; see hcack and status), REJECT (not sent; see reason) or ESCALATE (queued for human approval, not "
        "sent). request_id is chosen by the caller (1-64 characters from A-Z, a-z, 0-9, '_' and '-') and makes the "
        "call idempotent: repeating a request_id returns the first result and never sends the move again. "
        "dry_run=true returns the gate's decision without sending anything or recording the request_id."
    ),
    "get_request_status": (
        "Read-only. Current status of a previous request_move by request_id: the gate verdict and reason, whether it "
        "was sent, the equipment's HCACK, and whether the robot arrived."
    ),
}

# 參數在 schema 上宣告為字串／布林，但不讓 SDK 先擋掉型別不對的值：
# 任何值都要進到閘門、被判 BAD_PARAM 並留下稽核紀錄。
_Str = Annotated[Any, WithJsonSchema({"type": "string"})]
_Bool = Annotated[Any, WithJsonSchema({"type": "boolean", "default": False})]


def build_server(guardian: Guardian) -> MCPServer:
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=SERVER_VERSION, log_level="WARNING")

    def request_move(equipment_id: _Str, station: _Str, request_id: _Str, dry_run: _Bool = False) -> dict[str, Any]:
        return guardian.request_move(equipment_id, station, request_id, dry_run)

    def get_request_status(request_id: _Str) -> dict[str, Any]:
        return guardian.get_request_status(request_id)

    server.add_tool(
        request_move,
        name="request_move",
        description=DESCRIPTIONS["request_move"],
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    )
    server.add_tool(
        get_request_status,
        name="get_request_status",
        description=DESCRIPTIONS["get_request_status"],
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    )
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Guardian MCP server for the gem-robot simulator (stdio)")
    parser.add_argument("--equipment-host", default="127.0.0.1")
    parser.add_argument("--equipment-port", type=int, required=True, help="模擬設備的 HSMS 連接埠")
    parser.add_argument("--policy", default=None, help="政策檔（預設是 repo 裡的 policy.yaml）")
    parser.add_argument("--store", required=True, help="請求儲存（SQLite）")
    parser.add_argument("--audit", required=True, help="稽核雜湊鏈（JSONL）")
    parser.add_argument("--stations", default=None, help="假機器人的設定檔，用來估行程耗電")
    args = parser.parse_args()

    if os.environ.get(PRIVATE_KEY_ENV):
        print(f"refusing to start: {PRIVATE_KEY_ENV} must not be present in this process", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    # 這些只在真的要連設備時才 import，讓 build_server 可以單獨拿去測
    from fake_robot import DEFAULT_CONFIG_PATH, RobotConfig  # noqa: PLC0415
    from guardian import GemHostPort  # noqa: PLC0415
    from history import History  # noqa: PLC0415
    from host_client import HostClient  # noqa: PLC0415
    from policy import DEFAULT_POLICY_PATH, load_policy  # noqa: PLC0415
    from request_store import RequestStore  # noqa: PLC0415

    policy = load_policy(args.policy or DEFAULT_POLICY_PATH)  # 只載入這一次
    robot = RobotConfig.from_file(args.stations or DEFAULT_CONFIG_PATH)
    public_key = os.environ.get(PUBLIC_KEY_ENV)

    host = HostClient(address=args.equipment_host, port=args.equipment_port, reply_timeout=3.0)
    host.start()
    if host.wait_communicating(5):
        # 主機端的例行動作：要求 ON-LINE、啟用事件報告。失敗也沒關係，之後每個請求都會各自被判定。
        try:
            host.go_online()
            host.enable_move_events()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("host session setup failed; requests will be rejected until it works")

    guardian = Guardian(
        policy,
        GemHostPort(host, call_timeout=4.0),
        History(args.audit),
        RequestStore(args.store),
        trip_cost=robot.drain_per_second * robot.move_seconds,
        approver_public_key=base64.b64decode(public_key) if public_key else None,
    )
    try:
        build_server(guardian).run("stdio")
    finally:
        sys.stdout.flush()
        os._exit(0)  # secsgem 的背景執行緒在對方還連著時不一定會結束（見 README 限制）
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
