"""只追加的 JSONL 事件歷史，每筆帶前一筆的 SHA-256（雜湊鏈）。

每一行是一個 JSON 物件::

    {"seq": 0, "ts": "...", "kind": "...", "data": {...}, "prev": "<前一筆的 hash>", "hash": "<本筆的 hash>"}

``hash`` = SHA-256(去掉 ``hash`` 欄位後的 canonical JSON)。第一筆的 ``prev`` 是 64 個 0。
改掉任何一筆的任何欄位、刪掉或調換中間的行，``verify()`` 都會回 False。
注意：單純把檔尾幾筆整行截掉無法偵測（鏈沒有外部錨點）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

GENESIS_HASH = "0" * 64
_FIELDS = {"seq", "ts", "kind", "data", "prev", "hash"}


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(record_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record_without_hash).encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_detail(path: Path) -> tuple[str | None, int, str]:
    """走過整條鏈，回傳 (第一個問題的原因或 None, 問題之前完好的筆數, 最後一筆完好的 hash)。"""
    prev = GENESIS_HASH
    count = 0
    if not path.exists():
        return None, 0, prev
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            try:
                record = json.loads(line)
            except ValueError:
                return "not_json", count, prev
            if not isinstance(record, dict) or set(record) != _FIELDS:
                return "bad_fields", count, prev
            claimed = record.pop("hash")
            if record["seq"] != count:
                return "seq_mismatch", count, prev
            if record["prev"] != prev:
                return "prev_hash_mismatch", count, prev
            if _digest(record) != claimed:
                return "hash_mismatch", count, prev
            prev = claimed
            count += 1
    return None, count, prev


def _scan(path: Path) -> tuple[bool, int, str]:
    """走過整條鏈，回傳 (是否完好, 筆數, 最後一筆的 hash)。"""
    reason, count, prev = _scan_detail(path)
    return reason is None, count, prev


def verify_file(path: str | Path) -> bool:
    """驗證檔案的雜湊鏈；不存在或空檔視為完好。"""
    return _scan(Path(path))[0]


def verify_detail(path: str | Path) -> dict[str, Any]:
    """驗證並指出第一個壞掉的位置。

    回傳 ``valid``、``records_ok``（從頭算起完好的筆數）；鏈壞掉時另有
    ``first_bad_index``（0 起算，等於該筆應有的 seq）、``first_bad_line``（1 起算的行號）與 ``reason``。
    注意：被竄改的可能是這一筆，也可能是它的前一筆被整個重算過（那樣斷點會出現在下一筆的 prev）。
    """
    reason, count, _ = _scan_detail(Path(path))
    if reason is None:
        return {"valid": True, "records_ok": count, "first_bad_index": None, "first_bad_line": None, "reason": None}
    return {"valid": False, "records_ok": count, "first_bad_index": count, "first_bad_line": count + 1, "reason": reason}


class History:
    """執行緒安全的 append-only 歷史。只提供 ``append`` 與讀取，沒有修改或刪除。"""

    def __init__(self, path: str | Path, now: Callable[[], str] = _utc_now_iso) -> None:
        self.path = Path(path)
        self._now = now
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ok, self._count, self._last_hash = _scan(self.path)
        if not ok:
            raise ValueError(f"history file {self.path} fails hash-chain verification; refusing to append")

    def append(self, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """追加一筆並立即 flush。``data`` 必須可 JSON 序列化。"""
        with self._lock:
            record: dict[str, Any] = {
                "seq": self._count,
                "ts": self._now(),
                "kind": kind,
                "data": data or {},
                "prev": self._last_hash,
            }
            record["hash"] = _digest(record)
            line = _canonical(record)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
            self._last_hash = record["hash"]
            self._count += 1
            return record

    def verify(self) -> bool:
        with self._lock:
            return _scan(self.path)[0]

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
