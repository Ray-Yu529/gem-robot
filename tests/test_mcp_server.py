"""唯讀 MCP server 的驗收測試。用 SDK 內建的 Client：大多數走 in-process，一個走真正的 stdio 子行程。"""

from __future__ import annotations

import ast
import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

import mcp_server
from ingest import connect as connect_readwrite
from simulate_day import ADVERSARIAL_ALARM_TEXT, EQUIPMENT_ID, NORMAL_ALARM_TEXT

ROOT = Path(mcp_server.__file__).resolve().parent
EXPECTED_TOOLS = {
    "get_equipment_state",
    "list_alarms",
    "get_recent_events",
    "get_utilization",
    "get_move_durations",
    "verify_history",
}
DAY1 = {"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"}
ALL = {"since": "2026-01-01T00:00:00Z", "until": "2026-01-03T00:00:00Z"}
TABLES = ("state_changes", "events", "alarms", "telemetry", "gaps", "rejected", "message_log", "ingest_state")


def run_calls(server, calls):
    """連上 server，依序呼叫工具，回傳 (tools 清單, [CallToolResult...])。"""

    async def go():
        async with Client(server) as client:
            listing = await client.list_tools()
            results = [await client.call_tool(name, arguments) for name, arguments in calls]
            return listing.tools, results

    return asyncio.run(go())


def call(sim_data, name, arguments=None, history="default"):
    history_path = sim_data.history if history == "default" else history
    _, results = run_calls(mcp_server.build_server(sim_data.db, history_path), [(name, arguments or {})])
    result = results[0]
    assert result.is_error is not True, result.content
    return result.structured_content


def table_counts(db_path) -> dict[str, int]:
    db = sqlite3.connect(str(db_path))
    try:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}  # noqa: S608
    finally:
        db.close()


def query(db_path, sql, params=()):
    db = sqlite3.connect(str(db_path))
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


# ---- 工具清單：沒有寫入類工具，沒有自由 SQL ---------------------------------------------------


def test_tool_list_has_only_the_six_read_only_tools(sim_data):
    tools, _ = run_calls(mcp_server.build_server(sim_data.db, sim_data.history), [])
    assert {tool.name for tool in tools} == EXPECTED_TOOLS

    forbidden = ("sql", "query", "execute", "write", "insert", "update", "delete", "drop", "set_", "send", "move_to",
                 "command", "rcmd", "charge", "goto", "publish", "control")
    for tool in tools:
        assert not any(word in tool.name.lower() for word in forbidden), tool.name
        properties = set(tool.input_schema.get("properties", {}))
        assert properties <= {"equipment_id", "since", "until", "limit", "start_date", "end_date"}, tool.name
        assert not any(word in prop.lower() for prop in properties for word in ("sql", "query", "where", "table"))
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


def test_server_module_has_no_path_to_equipment_or_writers():
    """靜態檢查：server 不 import 任何能下命令或寫資料的模組，資料庫只經由 open_readonly 開啟。"""
    source = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(
        {"gem_equipment", "host_client", "fake_robot", "telemetry_publisher", "ingest", "simulate_day", "secsgem", "paho"},
    )
    # server 自己不開資料庫連線，一律經由 db.connect_readonly
    assert "sqlite3" not in source and "psycopg" not in source and "PgConnection" not in source
    assert source.count("db.connect_readonly(") == 1
    backend = (ROOT / "db.py").read_text(encoding="utf-8")
    assert "?mode=ro" in backend and "query_only" in backend and "WriteCapableAccountError" in backend


# ---- 用 SDK 的 stdio client 連真正的子行程，每個工具的正常路徑 -----------------------------------


def test_every_tool_over_stdio_returns_correct_data(sim_data):
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py"), "--db", str(sim_data.db), "--history", str(sim_data.history)],
        cwd=str(ROOT),
    )
    tools, results = run_calls(
        server,
        [
            ("get_equipment_state", {"equipment_id": EQUIPMENT_ID}),
            ("list_alarms", {"equipment_id": EQUIPMENT_ID, **ALL}),
            ("get_recent_events", {"equipment_id": EQUIPMENT_ID, "limit": 5}),
            ("get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2026-01-01", "end_date": "2026-01-02"}),
            ("get_move_durations", {"equipment_id": EQUIPMENT_ID, **DAY1, "limit": 3}),
            ("verify_history", {}),
        ],
    )
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for result in results:
        assert result.is_error is not True
        assert json.loads(result.content[0].text) == result.structured_content  # 文字與結構化內容一致
    state, alarms, events, utilization, moves, history = (result.structured_content for result in results)
    db = sim_data.db

    # get_equipment_state：模擬結束時設備已下線
    last_ts = query(db, "SELECT last_ts FROM ingest_state WHERE equipment_id = ?", (EQUIPMENT_ID,))[0][0]
    battery = query(db, "SELECT battery FROM telemetry ORDER BY seq DESC LIMIT 1")[0][0]
    assert state["ok"] and not state["no_data"]
    assert (state["control_state"], state["robot_state"], state["equipment_state"]) == ("OFFLINE", None, "OFFLINE")
    assert state["battery_percent"] == battery and state["last_update"] == last_ts == "2026-01-02T06:00:00.000000Z"
    assert state["alarm_active"] is False

    # list_alarms：三次警報，各 600 秒
    assert alarms["total_count"] == 3 and alarms["summary"] == {"set_count": 3, "cleared_count": 3, "total_duration_s": 1800.0}
    expected_alarms = query(db, "SELECT set_ts, clear_ts, duration_s FROM v_alarm_intervals ORDER BY set_seq")
    assert [(a["set_ts"], a["clear_ts"], a["duration_s"]) for a in alarms["alarms"]] == expected_alarms
    assert all(a["alarm"] == "LOW_BATTERY" and a["cleared"] for a in alarms["alarms"])

    # get_recent_events：最新的 5 筆，新的在前
    expected_events = query(db, "SELECT seq, ts, event, station FROM events ORDER BY seq DESC LIMIT 5")
    assert [(e["seq"], e["ts"], e["event"], e["station"]) for e in events["events"]] == expected_events
    assert events["total_count"] == query(db, "SELECT COUNT(*) FROM events")[0][0] and events["truncated"] is False

    # get_utilization：和 view 完全一致
    expected_days = query(
        db,
        "SELECT day, productive_s, standby_s, unscheduled_down_s, offline_s, unknown_s, total_s, availability "
        "FROM v_daily_availability ORDER BY day",
    )
    got_days = [
        (d["day"], d["productive_s"], d["standby_s"], d["unscheduled_down_s"], d["offline_s"], d["unknown_s"], d["total_s"], d["availability"])
        for d in utilization["days"]
    ]
    assert got_days == expected_days and len(got_days) == 2
    assert utilization["total"]["total_s"] == 86400.0
    assert utilization["total"]["unknown_s"] == sum(day[5] for day in expected_days) > 0
    assert utilization["total"]["availability"] == pytest.approx(
        sum(day[1] for day in expected_days) / sum(day[6] - day[4] - day[5] for day in expected_days),
    )

    # get_move_durations：limit 只限制清單，summary 涵蓋整個時間窗
    day1_moves = query(
        db,
        "SELECT station, started_ts, duration_s FROM v_move_durations WHERE started_ts < '2026-01-02' ORDER BY start_seq",
    )
    assert moves["total_count"] == moves["summary"]["move_count"] == len(day1_moves)
    assert [(m["station"], m["started_ts"], m["duration_s"]) for m in moves["moves"]] == day1_moves[:3]
    assert moves["summary"]["max_duration_s"] == 120.0
    assert moves["summary"]["completed_count"] == sum(1 for move in day1_moves if move[2] is not None)

    # verify_history
    assert history["valid"] is True and history["first_bad_index"] is None and history["records_ok"] > 1000


# ---- 唯讀連線 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO events (equipment_id, seq, site, ts, ts_us, event) VALUES ('X', 1, 's', 't', 0, 'ARRIVED')",
        "UPDATE alarms SET text = 'changed'",
        "DELETE FROM telemetry",
        "DROP TABLE events",
        "CREATE TABLE evil (x INTEGER)",
        "ALTER TABLE alarms ADD COLUMN evil TEXT",
        "PRAGMA query_only = OFF; DELETE FROM telemetry",
    ],
)
def test_read_only_connection_rejects_writes(sim_data, statement):
    before = table_counts(sim_data.db)
    connection = mcp_server.open_readonly(sim_data.db)
    try:
        with pytest.raises(sqlite3.Error):
            connection.executescript(statement)
            connection.commit()
    finally:
        connection.close()
    assert table_counts(sim_data.db) == before
    assert connection_can_read(sim_data.db)


def connection_can_read(db_path) -> bool:
    connection = mcp_server.open_readonly(db_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
    finally:
        connection.close()


def test_read_only_open_does_not_create_a_missing_database(tmp_path):
    with pytest.raises(sqlite3.Error):
        mcp_server.open_readonly(tmp_path / "nope.sqlite")
    assert not (tmp_path / "nope.sqlite").exists()
    result = mcp_server.ReadOnlyTools(tmp_path / "nope.sqlite", None).get_equipment_state("ROBOT01")
    assert result == {"ok": False, "error": {"code": "DATABASE_UNAVAILABLE", "message": "the telemetry database could not be read"}}


# ---- 參數驗證 -----------------------------------------------------------------------------

INJECTION = "'; DROP TABLE events;--"
BAD_CALLS = [
    ("bad since", "list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "yesterday", "until": "2026-01-02T00:00:00Z"}, "INVALID_TIMESTAMP"),
    ("date only", "list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01", "until": "2026-01-02T00:00:00Z"}, "INVALID_TIMESTAMP"),
    ("not UTC", "list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T00:00:00+08:00", "until": "2026-01-02T00:00:00Z"}, "INVALID_TIMESTAMP"),
    ("no zone", "get_move_durations", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T00:00:00", "until": "2026-01-02T00:00:00Z"}, "INVALID_TIMESTAMP"),
    ("impossible date", "list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "2026-02-30T00:00:00Z", "until": "2026-03-02T00:00:00Z"}, "INVALID_TIMESTAMP"),
    ("reversed", "list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"}, "INVALID_RANGE"),
    ("empty window", "get_move_durations", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"}, "INVALID_RANGE"),
    ("over 31 days", "list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T00:00:00Z", "until": "2026-02-01T00:00:01Z"}, "RANGE_TOO_LARGE"),
    ("over 31 days moves", "get_move_durations", {"equipment_id": EQUIPMENT_ID, "since": "2025-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"}, "RANGE_TOO_LARGE"),
    ("limit too big", "list_alarms", {"equipment_id": EQUIPMENT_ID, **ALL, "limit": 101}, "INVALID_LIMIT"),
    ("limit huge", "get_recent_events", {"equipment_id": EQUIPMENT_ID, "limit": 10**9}, "INVALID_LIMIT"),
    ("limit zero", "get_recent_events", {"equipment_id": EQUIPMENT_ID, "limit": 0}, "INVALID_LIMIT"),
    ("limit negative", "get_move_durations", {"equipment_id": EQUIPMENT_ID, **ALL, "limit": -5}, "INVALID_LIMIT"),
    ("bad start_date", "get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "01/01/2026", "end_date": "2026-01-02"}, "INVALID_DATE"),
    ("impossible start_date", "get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2026-13-01", "end_date": "2026-01-02"}, "INVALID_DATE"),
    ("dates reversed", "get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2026-01-02", "end_date": "2026-01-01"}, "INVALID_RANGE"),
    ("dates over 31 days", "get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2026-01-01", "end_date": "2026-02-01"}, "RANGE_TOO_LARGE"),
    ("injection in equipment_id", "get_equipment_state", {"equipment_id": INJECTION}, "INVALID_EQUIPMENT_ID"),
    ("injection in equipment_id 2", "get_recent_events", {"equipment_id": "ROBOT01' OR '1'='1"}, "INVALID_EQUIPMENT_ID"),
    ("injection in since", "list_alarms", {"equipment_id": EQUIPMENT_ID, "since": INJECTION, "until": "2026-01-02T00:00:00Z"}, "INVALID_TIMESTAMP"),
    ("injection in until", "get_move_durations", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z'; DROP TABLE events;--"}, "INVALID_TIMESTAMP"),
    ("injection in start_date", "get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": INJECTION, "end_date": "2026-01-02"}, "INVALID_DATE"),
    ("empty equipment_id", "get_equipment_state", {"equipment_id": ""}, "INVALID_EQUIPMENT_ID"),
    ("too long equipment_id", "get_equipment_state", {"equipment_id": "A" * 65}, "INVALID_EQUIPMENT_ID"),
]


def test_bad_arguments_get_clear_errors_and_tables_stay_intact(sim_data):
    before = table_counts(sim_data.db)
    server = mcp_server.build_server(sim_data.db, sim_data.history)
    _, results = run_calls(server, [(name, arguments) for _, name, arguments, _ in BAD_CALLS])

    for (label, _, _, expected_code), result in zip(BAD_CALLS, results):
        payload = result.structured_content
        assert result.is_error is not True, label
        assert payload["ok"] is False, label
        assert payload["error"]["code"] == expected_code, label
        assert set(payload) == {"ok", "error"}, label  # 沒有夾帶任何資料
        text = result.content[0].text
        assert "Traceback" not in text and "sqlite3" not in text and 'File "' not in text, label
        assert len(payload["error"]["message"]) < 200, label
    assert table_counts(sim_data.db) == before
    assert query(sim_data.db, "SELECT COUNT(*) FROM events")[0][0] > 0  # events 表還在


def test_wrong_types_and_unknown_tools_are_rejected_without_a_stack_trace(sim_data):
    before = table_counts(sim_data.db)
    server = mcp_server.build_server(sim_data.db, sim_data.history)
    _, results = run_calls(
        server,
        [
            ("get_recent_events", {"equipment_id": EQUIPMENT_ID, "limit": "100; DROP TABLE events"}),
            ("get_recent_events", {"limit": 5}),  # 缺 equipment_id
            ("list_alarms", {"equipment_id": EQUIPMENT_ID, "since": 20260101, "until": None}),
            ("run_sql", {"sql": "DROP TABLE events"}),
            ("send_move", {"equipment_id": EQUIPMENT_ID, "station": "A"}),
        ],
    )
    for result in results:
        assert result.is_error is True
        assert result.structured_content is None
        text = result.content[0].text
        assert "Traceback" not in text and 'File "' not in text
    assert table_counts(sim_data.db) == before


# ---- 輸出上限 -----------------------------------------------------------------------------


def test_large_results_are_truncated_with_total_count(sim_data):
    server = mcp_server.build_server(sim_data.db, sim_data.history)
    _, results = run_calls(
        server,
        [
            ("get_recent_events", {"equipment_id": EQUIPMENT_ID, "limit": 100}),
            ("get_move_durations", {"equipment_id": EQUIPMENT_ID, **ALL, "limit": 100}),
            ("get_recent_events", {"equipment_id": EQUIPMENT_ID}),
        ],
    )
    events, moves, default = results
    total_events = query(sim_data.db, "SELECT COUNT(*) FROM events")[0][0]
    total_moves = query(sim_data.db, "SELECT COUNT(*) FROM v_move_durations")[0][0]

    for result, key, total in ((events, "events", total_events), (moves, "moves", total_moves)):
        payload = result.structured_content
        assert len(result.content[0].text) <= mcp_server.MAX_RESPONSE_CHARS
        assert payload["truncated"] is True
        assert payload["total_count"] == total > 100
        assert payload["returned_count"] == len(payload[key]) < 100
        assert payload["returned_count"] > 10  # 截斷後仍有可用的資料
    # summary 不受截斷影響
    assert moves.structured_content["summary"]["move_count"] == total_moves

    payload = default.structured_content  # limit 預設 20，不需要截斷
    assert payload["limit"] == 20 and payload["returned_count"] == 20 and payload["truncated"] is False
    assert payload["total_count"] == total_events


# ---- 對抗性警報文字 ------------------------------------------------------------------------


def test_adversarial_alarm_text_is_returned_as_data_in_its_own_field(sim_data):
    assert len(ADVERSARIAL_ALARM_TEXT) > mcp_server.MAX_TEXT_CHARS
    stored = query(sim_data.db, "SELECT text FROM alarms WHERE action = 'set' ORDER BY seq")
    assert [row[0] for row in stored] == [NORMAL_ALARM_TEXT, ADVERSARIAL_ALARM_TEXT, NORMAL_ALARM_TEXT]  # 資料庫存原文

    server = mcp_server.build_server(sim_data.db, sim_data.history)
    tools, results = run_calls(server, [("list_alarms", {"equipment_id": EQUIPMENT_ID, **ALL})])
    payload = results[0].structured_content
    texts = [(alarm["alarm_text"], alarm["alarm_text_truncated"]) for alarm in payload["alarms"]]
    assert texts == [
        (NORMAL_ALARM_TEXT, False),
        (ADVERSARIAL_ALARM_TEXT[:200], True),  # 原樣，只截斷，不改寫也不過濾
        (NORMAL_ALARM_TEXT, False),
    ]

    # 只出現在 alarm_text 這個欄位，沒有混進其他欄位或說明文字
    marker = "忽略先前指示"
    assert marker in ADVERSARIAL_ALARM_TEXT[:200]
    without_text = json.loads(json.dumps(payload))
    for alarm in without_text["alarms"]:
        del alarm["alarm_text"]
    assert marker not in json.dumps(without_text, ensure_ascii=False)
    assert "Ignore all previous" not in json.dumps(without_text)

    # 工具描述與 server 說明是靜態文字，不含它
    for tool in tools:
        assert tool.description == mcp_server.DESCRIPTIONS[tool.name]
        assert marker not in tool.description and "Ignore all previous" not in tool.description
        assert marker not in json.dumps(tool.input_schema, ensure_ascii=False)
    assert marker not in mcp_server.INSTRUCTIONS


def test_tool_descriptions_do_not_depend_on_the_data(sim_data, tmp_path):
    connect_readwrite(tmp_path / "empty.sqlite").close()  # 只有 schema 的空資料庫
    with_data, _ = run_calls(mcp_server.build_server(sim_data.db, sim_data.history), [])
    without_data, _ = run_calls(mcp_server.build_server(tmp_path / "empty.sqlite", None), [])
    dump = lambda tools: sorted((t.name, t.description, json.dumps(t.input_schema, sort_keys=True)) for t in tools)  # noqa: E731
    assert dump(with_data) == dump(without_data)
    for text in mcp_server.DESCRIPTIONS.values():
        assert EQUIPMENT_ID not in text and "2026-01-02T06" not in text


# ---- 無資料 -------------------------------------------------------------------------------

FUTURE = {"since": "2031-05-01T00:00:00Z", "until": "2031-05-02T00:00:00Z"}
NO_DATA_CALLS = [
    ("get_equipment_state", {"equipment_id": "ROBOT99"}, "UNKNOWN_EQUIPMENT"),
    ("list_alarms", {"equipment_id": "ROBOT99", **ALL}, "UNKNOWN_EQUIPMENT"),
    ("get_recent_events", {"equipment_id": "ROBOT99"}, "UNKNOWN_EQUIPMENT"),
    ("get_utilization", {"equipment_id": "ROBOT99", "start_date": "2026-01-01", "end_date": "2026-01-02"}, "UNKNOWN_EQUIPMENT"),
    ("get_move_durations", {"equipment_id": "ROBOT99", **ALL}, "UNKNOWN_EQUIPMENT"),
    ("list_alarms", {"equipment_id": EQUIPMENT_ID, **FUTURE}, "OUTSIDE_DATA_RANGE"),
    ("list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T06:00:00Z", "until": "2026-01-01T07:00:00Z"}, "NO_ALARMS_IN_RANGE"),
    ("get_move_durations", {"equipment_id": EQUIPMENT_ID, **FUTURE}, "OUTSIDE_DATA_RANGE"),
    ("get_move_durations", {"equipment_id": EQUIPMENT_ID, "since": "2025-12-01T00:00:00Z", "until": "2025-12-25T00:00:00Z"}, "OUTSIDE_DATA_RANGE"),
    ("get_move_durations", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T06:00:00Z", "until": "2026-01-01T06:20:00Z"}, "NO_MOVES_IN_RANGE"),
    ("get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2031-05-01", "end_date": "2031-05-07"}, "NO_DATA_IN_RANGE"),
    ("get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2025-12-30", "end_date": "2025-12-31"}, "NO_DATA_IN_RANGE"),
]


def test_no_data_situations_say_so_explicitly(sim_data):
    server = mcp_server.build_server(sim_data.db, sim_data.history)
    _, results = run_calls(server, [(name, arguments) for name, arguments, _ in NO_DATA_CALLS])
    for (name, arguments, reason), result in zip(NO_DATA_CALLS, results):
        payload = result.structured_content
        assert payload["ok"] is True and payload["no_data"] is True, (name, arguments)
        assert payload["reason"] == reason
        assert payload["message"].startswith("無資料")
        # 不是「回個空清單了事」
        assert not any(key in payload for key in ("alarms", "events", "days", "moves")), (name, arguments)
        # 「這段時間有資料但沒發生」和「這段時間根本沒資料」要分得出來
        assert ("total_count" in payload) == (reason in ("NO_ALARMS_IN_RANGE", "NO_MOVES_IN_RANGE"))
        assert payload.get("total_count", 0) == 0
        if reason != "UNKNOWN_EQUIPMENT":
            assert payload["data_range"] == {
                "first_ts": "2026-01-01T06:00:00.000000Z",
                "last_ts": "2026-01-02T06:00:00.000000Z",
            }


def test_empty_database_and_missing_history_report_no_data(tmp_path):
    connect_readwrite(tmp_path / "empty.sqlite").close()
    tools = mcp_server.ReadOnlyTools(tmp_path / "empty.sqlite", tmp_path / "missing.jsonl")
    assert tools.get_equipment_state("ROBOT01")["reason"] == "UNKNOWN_EQUIPMENT"
    history = tools.verify_history()
    assert history["no_data"] is True and history["reason"] == "HISTORY_NOT_AVAILABLE"
    assert "valid" not in history  # 沒有檔案不等於「驗證通過」


# ---- verify_history -----------------------------------------------------------------------


def test_verify_history_true_then_false_with_position_after_tampering(sim_data, tmp_path):
    assert call(sim_data, "verify_history")["valid"] is True

    tampered = tmp_path / "tampered.jsonl"
    shutil.copy(sim_data.history, tampered)
    lines = tampered.read_text(encoding="utf-8").splitlines()
    target = next(i for i, line in enumerate(lines) if '"kind":"host_command_reply"' in line and i > 50)
    record = json.loads(lines[target])
    record["data"]["hcack"] = 0  # 手動改一筆
    lines[target] = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    result = call(sim_data, "verify_history", history=tampered)
    assert result["ok"] is True and result["valid"] is False
    assert result["first_bad_index"] == target
    assert result["first_bad_line"] == target + 1
    assert result["records_ok"] == target
    assert result["reason"] == "hash_mismatch"

    # 原檔沒有被動到
    assert call(sim_data, "verify_history")["valid"] is True
