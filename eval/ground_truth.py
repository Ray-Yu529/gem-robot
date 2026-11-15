"""從資料庫（與歷史檔）計算每一題的標準答案。答案不寫在題目檔裡，也不經過 MCP server。

每一題的 ``truth`` 區塊指定 ``kind`` 與參數；這裡用自己的 SQL 算出答案。
「沒有答案」也是算出來的：設備不存在、或問的時間完全在資料涵蓋範圍之外，就回傳 ``NO_DATA``。
時間窗一律是 UTC 的 ``[since, until)``。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import history as history_module


class _NoData:
    def __repr__(self) -> str:
        return "NO_DATA"


NO_DATA = _NoData()
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _us(text: str) -> int:
    return (datetime.fromisoformat(text.replace("Z", "+00:00")) - _EPOCH) // timedelta(microseconds=1)


class GroundTruth:
    def __init__(self, db_path: str | Path, history_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path)
        self._history_path = Path(history_path) if history_path else None
        self._kinds: dict[str, Callable[..., Any]] = {
            "alarm_count": self._alarm_count,
            "alarm_total_duration_s": self._alarm_total_duration_s,
            "first_alarm_ts": self._first_alarm_ts,
            "daily_value": self._daily_value,
            "range_availability": self._range_availability,
            "move_count": self._move_count,
            "completed_move_count": self._completed_move_count,
            "longest_move_s": self._longest_move_s,
            "alarm_active_now": self._alarm_active_now,
            "current_state": self._current_state,
            "current_battery": self._current_battery,
            "last_update": self._last_update,
            "latest_event": self._latest_event,
            "event_total": self._event_total,
            "history_valid": self._history_valid,
        }

    def answer(self, truth: dict[str, Any]) -> Any:
        """算出一題的標準答案（數值、布林、字串，或 NO_DATA）。"""
        params = {key: value for key, value in truth.items() if key != "kind"}
        if truth["kind"] == "history_valid":
            return self._history_valid(**params)
        with closing(sqlite3.connect(self._db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return self._kinds[truth["kind"]](db, **params)

    # ---- 共用 ----------------------------------------------------------------

    @staticmethod
    def _coverage(db: sqlite3.Connection, equipment_id: str) -> tuple[int, int] | None:
        row = db.execute(
            "SELECT (SELECT MIN(ts_us) FROM state_changes WHERE equipment_id = :id), last_ts_us "
            "FROM ingest_state WHERE equipment_id = :id",
            {"id": equipment_id},
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return row[0], row[1]

    def _window(self, db: sqlite3.Connection, equipment_id: str, since: str, until: str) -> tuple[int, int] | None:
        """回傳時間窗（微秒）；設備不存在或時間窗完全在資料之外回 None。"""
        coverage = self._coverage(db, equipment_id)
        start, end = _us(since), _us(until)
        if coverage is None or end <= coverage[0] or start > coverage[1]:
            return None
        return start, end

    # ---- 警報 ----------------------------------------------------------------

    def _alarm_count(self, db, equipment_id: str, since: str, until: str) -> Any:
        window = self._window(db, equipment_id, since, until)
        if window is None:
            return NO_DATA
        return db.execute(
            "SELECT COUNT(*) FROM alarms WHERE equipment_id = ? AND action = 'set' AND ts_us >= ? AND ts_us < ?",
            (equipment_id, *window),
        ).fetchone()[0]

    def _alarm_total_duration_s(self, db, equipment_id: str, since: str, until: str) -> Any:
        window = self._window(db, equipment_id, since, until)
        if window is None:
            return NO_DATA
        # 每個 set 配它之後第一個 clear（直接用原始資料表算，不經過 view）
        row = db.execute(
            "SELECT SUM((SELECT MIN(c.ts_us) FROM alarms c WHERE c.equipment_id = s.equipment_id "
            "            AND c.alarm = s.alarm AND c.action = 'clear' AND c.seq > s.seq) - s.ts_us) "
            "FROM alarms s WHERE s.equipment_id = ? AND s.action = 'set' AND s.ts_us >= ? AND s.ts_us < ?",
            (equipment_id, *window),
        ).fetchone()
        return NO_DATA if row[0] is None else row[0] / 1_000_000

    def _first_alarm_ts(self, db, equipment_id: str, since: str, until: str) -> Any:
        window = self._window(db, equipment_id, since, until)
        if window is None:
            return NO_DATA
        row = db.execute(
            "SELECT ts FROM alarms WHERE equipment_id = ? AND action = 'set' AND ts_us >= ? AND ts_us < ? "
            "ORDER BY seq LIMIT 1",
            (equipment_id, *window),
        ).fetchone()
        return row[0] if row else NO_DATA

    # ---- 稼動率 --------------------------------------------------------------

    _DAILY_COLUMNS = (
        "productive_s", "standby_s", "unscheduled_down_s", "offline_s", "unknown_s", "total_s", "availability",
    )

    def _daily_value(self, db, equipment_id: str, day: str, column: str) -> Any:
        if column not in self._DAILY_COLUMNS:
            raise ValueError(f"unknown daily column {column!r}")
        row = db.execute(
            f"SELECT {column} FROM v_daily_availability WHERE equipment_id = ? AND day = ?",  # noqa: S608 - 白名單欄名
            (equipment_id, day),
        ).fetchone()
        return NO_DATA if row is None or row[0] is None else row[0]

    def _range_availability(self, db, equipment_id: str, start_date: str, end_date: str) -> Any:
        row = db.execute(
            "SELECT SUM(productive_s), SUM(total_s - offline_s - unknown_s) FROM v_daily_availability "
            "WHERE equipment_id = ? AND day >= ? AND day <= ?",
            (equipment_id, start_date, end_date),
        ).fetchone()
        return NO_DATA if not row or not row[1] else row[0] / row[1]

    # ---- MOVE ----------------------------------------------------------------

    def _moves(self, db, equipment_id: str, since: str, until: str, select: str) -> Any:
        window = self._window(db, equipment_id, since, until)
        if window is None:
            return NO_DATA
        value = db.execute(
            f"SELECT {select} FROM v_move_durations WHERE equipment_id = ? AND started_us >= ? AND started_us < ?",  # noqa: S608
            (equipment_id, *window),
        ).fetchone()[0]
        return NO_DATA if value is None else value

    def _move_count(self, db, equipment_id: str, since: str, until: str) -> Any:
        return self._moves(db, equipment_id, since, until, "COUNT(*)")

    def _completed_move_count(self, db, equipment_id: str, since: str, until: str) -> Any:
        return self._moves(db, equipment_id, since, until, "COUNT(duration_s)")

    def _longest_move_s(self, db, equipment_id: str, since: str, until: str) -> Any:
        return self._moves(db, equipment_id, since, until, "MAX(duration_s)")

    # ---- 目前狀態（= 資料庫裡最新的一筆，不是牆上時鐘的現在） ---------------------

    @staticmethod
    def _latest(db, sql: str, equipment_id: str) -> Any:
        row = db.execute(sql, (equipment_id,)).fetchone()
        return NO_DATA if row is None else row[0]

    def _known(self, db, equipment_id: str) -> bool:
        return self._coverage(db, equipment_id) is not None

    def _alarm_active_now(self, db, equipment_id: str) -> Any:
        if not self._known(db, equipment_id):
            return NO_DATA
        latest = self._latest(db, "SELECT action FROM alarms WHERE equipment_id = ? ORDER BY seq DESC LIMIT 1", equipment_id)
        return latest == "set"

    def _current_state(self, db, equipment_id: str) -> Any:
        return self._latest(db, "SELECT to_state FROM state_changes WHERE equipment_id = ? ORDER BY seq DESC LIMIT 1", equipment_id)

    def _current_battery(self, db, equipment_id: str) -> Any:
        return self._latest(db, "SELECT battery FROM telemetry WHERE equipment_id = ? ORDER BY seq DESC LIMIT 1", equipment_id)

    def _last_update(self, db, equipment_id: str) -> Any:
        return self._latest(db, "SELECT last_ts FROM ingest_state WHERE equipment_id = ?", equipment_id)

    def _latest_event(self, db, equipment_id: str, field: str) -> Any:
        if field not in ("event", "station"):
            raise ValueError(f"unknown event field {field!r}")
        return self._latest(
            db,
            f"SELECT {field} FROM events WHERE equipment_id = ? ORDER BY seq DESC LIMIT 1",  # noqa: S608 - 白名單欄名
            equipment_id,
        )

    def _event_total(self, db, equipment_id: str) -> Any:
        if not self._known(db, equipment_id):
            return NO_DATA
        return db.execute("SELECT COUNT(*) FROM events WHERE equipment_id = ?", (equipment_id,)).fetchone()[0]

    # ---- 歷史鏈 --------------------------------------------------------------

    def _history_valid(self) -> Any:
        if self._history_path is None or not self._history_path.is_file():
            return NO_DATA
        return history_module.verify_file(self._history_path)
