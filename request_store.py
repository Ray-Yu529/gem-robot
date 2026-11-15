"""請求的持久儲存（SQLite）：冪等、狀態查詢、待核准佇列、核准紀錄。

兩個程序會開這個檔案：

* 閘門（guardian，在 MCP server 程序內）：讀寫 ``requests``，讀 ``approvals``。
* approve.py（獨立程序）：讀 ``requests`` 裡待核准的項目，寫 ``approvals``。

``approvals`` 裡的每一筆都帶 Ed25519 簽章；能寫這個檔案不等於能核准（見 guardian._verify_approval）。
這個檔案與遙測資料庫是分開的，唯讀 MCP server 碰不到它。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id    TEXT PRIMARY KEY,
    equipment_id  TEXT NOT NULL,
    station       TEXT NOT NULL,
    created_at    REAL NOT NULL,     -- 閘門的時鐘（秒）
    verdict       TEXT NOT NULL,     -- 閘門的判定：EXECUTE / REJECT / ESCALATE
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL,
    hcack         INTEGER,
    policy_sha256 TEXT NOT NULL,
    result        TEXT NOT NULL      -- 回給呼叫端的結果（JSON）；重送同一個 request_id 時原樣回傳
);
CREATE TABLE IF NOT EXISTS approvals (
    request_id TEXT PRIMARY KEY,
    decision   TEXT NOT NULL CHECK (decision IN ('approve', 'deny')),
    signed_at  REAL NOT NULL,
    signature  TEXT NOT NULL,        -- base64(Ed25519 簽章)
    processed  INTEGER NOT NULL DEFAULT 0
);
"""

PENDING_APPROVAL = "PENDING_APPROVAL"


class RequestStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- requests -----------------------------------------------------------

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT request_id, equipment_id, station, created_at, verdict, reason, status, hcack, policy_sha256, result "
                "FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        keys = ("request_id", "equipment_id", "station", "created_at", "verdict", "reason", "status", "hcack", "policy_sha256")
        record = dict(zip(keys, row[:9]))
        record["result"] = json.loads(row[9])
        return record

    def insert(self, record: dict[str, Any]) -> bool:
        """新增一筆；request_id 已存在回 False（不覆蓋）。"""
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO requests (request_id, equipment_id, station, created_at, verdict, reason, status, hcack, "
                "policy_sha256, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (request_id) DO NOTHING",
                (
                    record["request_id"],
                    record["equipment_id"],
                    record["station"],
                    record["created_at"],
                    record["verdict"],
                    record["reason"],
                    record["status"],
                    record.get("hcack"),
                    record["policy_sha256"],
                    json.dumps(record["result"], sort_keys=True),
                ),
            )
            return cursor.rowcount == 1

    def update(self, request_id: str, *, status: str, result: dict[str, Any], hcack: int | None = None,
               verdict: str | None = None, reason: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE requests SET status = ?, result = ?, hcack = COALESCE(?, hcack), "
                "verdict = COALESCE(?, verdict), reason = COALESCE(?, reason) WHERE request_id = ?",
                (status, json.dumps(result, sort_keys=True), hcack, verdict, reason, request_id),
            )

    def recent(self, since: float) -> list[tuple[str, str, str, float, str]]:
        """created_at >= since 的請求：(request_id, equipment_id, station, created_at, verdict)。"""
        with self._lock:
            return self._db.execute(
                "SELECT request_id, equipment_id, station, created_at, verdict FROM requests WHERE created_at >= ? "
                "ORDER BY created_at",
                (since,),
            ).fetchall()

    def with_status(self, status: str) -> list[dict[str, Any]]:
        with self._lock:
            ids = [row[0] for row in self._db.execute(
                "SELECT request_id FROM requests WHERE status = ? ORDER BY created_at", (status,),
            )]
        return [record for record in (self.get(request_id) for request_id in ids) if record is not None]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    # ---- approvals ----------------------------------------------------------

    def add_approval(self, request_id: str, decision: str, signed_at: float, signature: str) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO approvals (request_id, decision, signed_at, signature) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (request_id) DO NOTHING",
                (request_id, decision, signed_at, signature),
            )
            return cursor.rowcount == 1

    def unprocessed_approvals(self) -> list[tuple[str, str, float, str]]:
        with self._lock:
            return self._db.execute(
                "SELECT request_id, decision, signed_at, signature FROM approvals WHERE processed = 0 ORDER BY signed_at",
            ).fetchall()

    def mark_approval_processed(self, request_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE approvals SET processed = 1 WHERE request_id = ?", (request_id,))
