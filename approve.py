"""人工核准用的獨立 CLI。**不是 MCP 工具，也不在 MCP server 的程序裡。**

    python approve.py keygen                                   # 產生一組金鑰（印出兩個環境變數的值）
    python approve.py list    --store guardian.sqlite          # 列出待核准的請求
    python approve.py approve --store guardian.sqlite REQUEST_ID
    python approve.py deny    --store guardian.sqlite REQUEST_ID

憑證：Ed25519 私鑰，從環境變數 ``GUARDIAN_APPROVER_PRIVATE_KEY``（base64）讀取。
閘門那邊只有對應的公鑰（``GUARDIAN_APPROVER_PUBLIC_KEY``），所以它驗得了、簽不出核准。
這支程式只寫一筆「已簽章的決定」進請求儲存；真正送不送命令仍由閘門在重新檢查前置條件後決定。
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from guardian import approval_message
from request_store import PENDING_APPROVAL, RequestStore

PRIVATE_KEY_ENV = "GUARDIAN_APPROVER_PRIVATE_KEY"
PUBLIC_KEY_ENV = "GUARDIAN_APPROVER_PUBLIC_KEY"


def generate_keypair() -> tuple[str, str]:
    """回傳 (私鑰, 公鑰)，都是 base64 的 32 位元組原始金鑰。"""
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw_private).decode(), base64.b64encode(raw_public).decode()


def sign_decision(store: RequestStore, request_id: str, decision: str, private_key_b64: str, now: float | None = None) -> str:
    """對一筆待核准的請求簽下 approve／deny 並寫進儲存。回傳說明文字；有問題丟 ValueError。"""
    if decision not in ("approve", "deny"):
        raise ValueError("decision must be approve or deny")
    record = store.get(request_id)
    if record is None:
        raise ValueError("no such request")
    if record["status"] != PENDING_APPROVAL:
        raise ValueError(f"request is not waiting for approval (status: {record['status']})")
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    signed_at = time.time() if now is None else now
    message = approval_message(
        record["request_id"], record["equipment_id"], record["station"], decision, record["policy_sha256"], signed_at,
    )
    signature = base64.b64encode(private.sign(message)).decode()
    if not store.add_approval(request_id, decision, signed_at, signature):
        raise ValueError("a decision for this request was already recorded")
    return f"{decision} recorded for {request_id} ({record['equipment_id']} -> {record['station']})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen")
    for name in ("list", "approve", "deny"):
        sub = commands.add_parser(name)
        sub.add_argument("--store", required=True)
        if name != "list":
            sub.add_argument("request_id")
    args = parser.parse_args()

    if args.command == "keygen":
        private, public = generate_keypair()
        print(f"{PRIVATE_KEY_ENV}={private}    # 只給核准者；不要放進 MCP server 的環境")
        print(f"{PUBLIC_KEY_ENV}={public}     # 給 mcp_guardian_server.py")
        return 0

    store = RequestStore(args.store)
    try:
        if args.command == "list":
            pending = store.with_status(PENDING_APPROVAL)
            if not pending:
                print("no requests are waiting for approval")
            for record in pending:
                print(f"{record['request_id']}  {record['equipment_id']} -> {record['station']}  policy {record['policy_sha256'][:12]}")
            return 0
        private_key = os.environ.get(PRIVATE_KEY_ENV)
        if not private_key:
            print(f"{PRIVATE_KEY_ENV} is not set; cannot sign a decision", file=sys.stderr)
            return 2
        try:
            print(sign_decision(store, args.request_id, args.command, private_key))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
