"""端到端：simulate_day 灌進整條管線後檢查資料庫。

原規格的端到端測試需要 docker（mosquitto + TimescaleDB）。這台機器沒有 docker，
所以這裡用行程內的 loopback broker + SQLite，其餘（HSMS、設備、publisher、ingest、view）都是真的。
用 ``-m "not e2e"`` 可以跳過。
"""

from __future__ import annotations

import sqlite3

import pytest

from simulate_day import DEFAULT_START, EQUIPMENT_ID, run_simulation

pytestmark = pytest.mark.e2e

TABLES = ("message_log", "state_changes", "events", "alarms", "telemetry", "gaps", "ingest_state", "rejected")


def snapshot(path) -> dict[str, list]:
    db = sqlite3.connect(str(path))
    try:
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall() for table in TABLES}  # noqa: S608
    finally:
        db.close()


def test_simulated_day_daily_seconds_add_up_to_the_simulated_duration(tmp_path):
    path = tmp_path / "sim.sqlite"
    summary = run_simulation(path, hours=24)
    assert summary["history_ok"] is True
    assert summary["broker"]["duplicated"] == 1
    assert summary["ingest"]["duplicate"] == 1  # 重複投遞被冪等地忽略
    assert summary["ingest"]["rejected"] == 0
    assert summary["publisher"]["dropped"] > 0  # broker 斷線時丟了最舊的
    assert summary["publisher"]["gap_reports"] == 1
    assert summary["ingest"]["inserted"] == summary["publisher"]["enqueued"] - summary["publisher"]["dropped"]

    db = sqlite3.connect(str(path))
    try:
        days = db.execute(
            "SELECT day, productive_s, standby_s, unscheduled_down_s, offline_s, unknown_s, total_s, availability "
            "FROM v_daily_availability WHERE equipment_id = ? ORDER BY day",
            (EQUIPMENT_ID,),
        ).fetchall()
        # 06:00 起算 24 小時 -> 跨兩個 UTC 日：18 小時 + 6 小時
        assert [(day, total) for day, *_, total, _ in days] == [("2026-01-01", 64800.0), ("2026-01-02", 21600.0)]
        assert sum(row[6] for row in days) == summary["simulated_seconds"] == 86400.0
        for _, productive, standby, down, offline, unknown, total, ratio in days:
            assert productive + standby + down + offline + unknown == total
            assert ratio == productive / (total - offline - unknown)
            assert 0.0 < ratio < 1.0

        # 每個狀態都有出現；OFFLINE = 開頭 30 分鐘 + 結尾約 30 分鐘
        totals = [sum(row[i] for row in days) for i in range(1, 6)]
        productive, standby, down, offline, unknown = totals
        assert min(totals) > 0
        assert 3500 <= offline <= 3600
        assert down == 3 * 600.0  # 三次警報，各 10 分鐘後人工充電

        # 狀態區間首尾相接、涵蓋整段模擬
        first_us, last_us, covered = db.execute(
            "SELECT MIN(start_us), MAX(end_us), SUM(end_us - start_us) FROM v_state_intervals",
        ).fetchone()
        assert covered == last_us - first_us == 86400 * 1_000_000
        assert first_us == int(DEFAULT_START.timestamp()) * 1_000_000

        assert db.execute("SELECT SUM(set_count), SUM(cleared_count) FROM v_daily_alarm_stats").fetchone() == (3, 3)
        gaps = db.execute("SELECT source, seq_from, seq_to, missing FROM gaps ORDER BY source").fetchall()
        assert [g[0] for g in gaps] == ["publisher_reported", "seq_missing"]
        assert gaps[0][1:] == gaps[1][1:]  # publisher 自己回報的和 ingest 偵測到的缺號一致
        assert gaps[0][3] == summary["publisher"]["dropped"]
        # broker 斷線的 08:00–08:40（= 14:00–14:40Z）落在 UNKNOWN，且只有丟掉資料的那一段
        assert 0 < unknown <= 40 * 60
        completed = db.execute("SELECT duration_s FROM v_move_durations WHERE duration_s IS NOT NULL").fetchall()
        assert len(completed) > 200 and {row[0] for row in completed} == {120.0}
    finally:
        db.close()


def test_rerunning_the_simulation_into_the_same_database_changes_nothing(tmp_path):
    path = tmp_path / "sim.sqlite"
    first = run_simulation(path, hours=2)
    before = snapshot(path)
    second = run_simulation(path, hours=2)

    assert first["ingest"]["inserted"] > 1000
    assert second["ingest"]["inserted"] == 0
    assert second["ingest"]["duplicate"] == first["ingest"]["inserted"] + first["ingest"]["duplicate"]
    assert snapshot(path) == before
