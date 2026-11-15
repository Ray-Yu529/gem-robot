"""整合測試：真的 mosquitto + TimescaleDB + Grafana（docker compose）。

    docker compose up -d --wait
    python -m pytest -m integration

stack 沒起來（或沒有 .env）時整個檔案會 skip，所以一般的 ``pytest`` 不依賴 docker。
會清空 TimescaleDB 裡的資料表後重灌一次 24 小時模擬（約 1.5 分鐘）。只用模擬資料。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

import db as db_backend
import mcp_server
import payload_schema
import stack_env
from ingest import Ingestor, start_mqtt_ingest
from simulate_day import ADVERSARIAL_ALARM_TEXT, EQUIPMENT_ID, run_simulation, truncate_stack
from telemetry_publisher import MqttTransport

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

ROOT = Path(mcp_server.__file__).resolve().parent
AVAILABILITY_SQL = (
    "SELECT day, productive_s, standby_s, unscheduled_down_s, offline_s, unknown_s, total_s, availability "
    "FROM v_daily_availability WHERE equipment_id = ? ORDER BY day"
)


@pytest.fixture(scope="session")
def stack():
    env = stack_env.load_env()
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "READONLY_USER", "READONLY_PASSWORD", "MQTT_USERNAME", "MQTT_PASSWORD")
    if not all(env.get(key) for key in required):
        pytest.skip("no .env: copy .env.example to .env and run `docker compose up -d --wait`")
    ports = (int(env.get("POSTGRES_PORT", "5432")), int(env.get("MQTT_PORT", "1883")))
    if not all(stack_env.port_open(port) for port in ports):
        pytest.skip("docker compose stack is not running: `docker compose up -d --wait`")
    return {
        "env": env,
        "owner": stack_env.owner_dsn(env),
        "readonly": stack_env.readonly_dsn(env),
        "mqtt": stack_env.mqtt_params(env),
    }


@pytest.fixture(scope="session")
def stack_sim(stack, tmp_path_factory):
    """清空後，把一整天的模擬經由真的 broker 灌進 TimescaleDB。"""
    truncate_stack(stack["owner"])
    history = tmp_path_factory.mktemp("stack") / "sim.history.jsonl"
    summary = run_simulation(stack["owner"], hours=24, adversarial=True, mqtt=stack["mqtt"], history_path=history)
    return {**stack, "summary": summary, "history": history}


def pg_rows(dsn: str, sql: str, params=()) -> list[tuple]:
    connection = db_backend.PgConnection(dsn, autocommit=True)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---- 端到端 -------------------------------------------------------------------------------


def test_simulated_day_through_real_broker_adds_up_to_the_simulated_duration(stack_sim):
    summary = stack_sim["summary"]
    assert summary["transport"] == "mqtt" and summary["history_ok"] is True
    assert summary["ingest"] == {
        "inserted": summary["publisher"]["enqueued"] - summary["publisher"]["dropped"],
        "duplicate": 1,  # 重複投遞被冪等地忽略
        "rejected": 0,
    }
    assert summary["publisher"]["dropped"] > 0 and summary["publisher"]["gap_reports"] == 1

    days = pg_rows(stack_sim["readonly"], AVAILABILITY_SQL, (EQUIPMENT_ID,))
    assert [(day[0], day[6]) for day in days] == [("2026-01-01", 64800.0), ("2026-01-02", 21600.0)]
    assert sum(day[6] for day in days) == summary["simulated_seconds"] == 86400.0
    for _, productive, standby, down, offline, unknown, total, ratio in days:
        assert productive + standby + down + offline + unknown == total
        assert ratio == pytest.approx(productive / (total - offline - unknown), abs=1e-12)
    assert all(isinstance(value, float) for day in days for value in day[1:])

    gaps = pg_rows(stack_sim["readonly"], "SELECT source, seq_from, seq_to, missing FROM gaps ORDER BY source")
    assert [g[0] for g in gaps] == ["publisher_reported", "seq_missing"] and gaps[0][1:] == gaps[1][1:]
    assert gaps[0][3] == summary["publisher"]["dropped"]


def test_timescaledb_views_give_exactly_the_same_numbers_as_sqlite(stack_sim, sim_data):
    """同一份確定性模擬，分別走 loopback+SQLite 與 mosquitto+TimescaleDB，view 的結果必須一樣。"""
    sqlite_db = sqlite3.connect(str(sim_data.db))
    try:
        queries = {
            "availability": AVAILABILITY_SQL,
            "alarms": "SELECT set_ts, clear_ts, duration_s, text FROM v_alarm_intervals WHERE equipment_id = ? ORDER BY set_seq",
            "alarm_stats": "SELECT day, set_count, cleared_count, total_duration_s, avg_duration_s FROM v_daily_alarm_stats WHERE equipment_id = ? ORDER BY day",
            "moves": "SELECT start_seq, station, started_ts, arrived_ts, duration_s FROM v_move_durations WHERE equipment_id = ? ORDER BY start_seq",
            "intervals": "SELECT seq, raw_state, e10_state, start_us, end_us FROM v_state_intervals WHERE equipment_id = ? ORDER BY seq",
            "counts": "SELECT (SELECT COUNT(*) FROM state_changes WHERE equipment_id = ?), (SELECT COUNT(*) FROM telemetry), (SELECT COUNT(*) FROM events)",
        }
        for name, sql in queries.items():
            expected = sqlite_db.execute(sql, (EQUIPMENT_ID,)).fetchall()
            actual = pg_rows(stack_sim["readonly"], sql, (EQUIPMENT_ID,))
            assert len(actual) == len(expected) > 0, name
            for got, want in zip(actual, expected):
                assert tuple(got) == pytest.approx(tuple(want), abs=1e-9) if name in ("availability", "alarm_stats") else tuple(got) == tuple(want), name
    finally:
        sqlite_db.close()


def test_telemetry_is_a_hypertable(stack_sim):
    rows = pg_rows(stack_sim["owner"], "SELECT hypertable_name, num_chunks FROM timescaledb_information.hypertables")
    assert [row[0] for row in rows] == ["telemetry"]
    assert rows[0][1] >= 2  # 模擬跨兩個 UTC 日，每日一個 chunk


# ---- MQTT -> ingest -> TimescaleDB：schema、冪等、缺號 --------------------------------------


def test_mqtt_ingest_rejects_dedupes_and_detects_gaps(stack_sim):
    equipment = "IT-" + uuid.uuid4().hex[:12]
    topic = payload_schema.topic_for("itest", equipment, "telemetry")

    def message(seq: int, **override) -> bytes:
        body = {"schema_version": 1, "equipment_id": equipment, "seq": seq, "ts": f"2026-03-01T00:00:{seq:02d}Z", "battery": 50}
        body.update(override)
        return json.dumps(body).encode()

    ingestor = Ingestor(stack_sim["owner"])
    subscriber = start_mqtt_ingest(ingestor, client_id="it-ingest-" + equipment, **stack_sim["mqtt"])
    publisher = MqttTransport(client_id="it-pub-" + equipment, **stack_sim["mqtt"])
    try:
        assert subscriber.subscribed.wait(10) and publisher.wait_connected(10)
        bad = json.dumps({"schema_version": 1, "equipment_id": equipment, "seq": "x", "ts": "2026-03-01T00:00:00Z", "battery": 50}).encode()
        for payload in (message(0), message(1), message(1), bad, message(5)):  # 重複一筆、壞一筆、跳號
            assert publisher.publish(topic, payload, 1, False) is True
        assert wait_until(lambda: sum(ingestor.counts.values()) == 5)
    finally:
        publisher.close()
        subscriber.loop_stop()
        subscriber.disconnect()
        ingestor.close()

    assert ingestor.counts == {"inserted": 3, "duplicate": 1, "rejected": 1}
    owner = stack_sim["owner"]
    assert pg_rows(owner, "SELECT seq FROM telemetry WHERE equipment_id = ? ORDER BY seq", (equipment,)) == [(0,), (1,), (5,)]
    assert pg_rows(owner, "SELECT seq_from, seq_to, missing, source FROM gaps WHERE equipment_id = ?", (equipment,)) == [
        (2, 4, 3, "seq_missing"),
    ]
    rejected = pg_rows(owner, "SELECT reason FROM rejected WHERE topic = ?", (topic,))
    assert len(rejected) == 1 and "seq" in rejected[0][0]


def test_broker_rejects_wrong_credentials(stack_sim):
    bad = MqttTransport(client_id="it-bad", **{**stack_sim["mqtt"], "password": "definitely-wrong"})
    try:
        assert bad.wait_connected(3) is False
        assert bad.publish("fab/itest/X/telemetry", b"{}", 1, False) is False  # 連不上 -> publisher 會留在自己的佇列
    finally:
        bad.close()


# ---- 唯讀角色 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO events (equipment_id, seq, site, ts, ts_us, event) VALUES ('X', 1, 's', 't', 0, 'ARRIVED')",
        "UPDATE alarms SET text = 'changed'",
        "DELETE FROM telemetry",
        "TRUNCATE state_changes",
        "DROP TABLE events",
        "DROP VIEW v_daily_availability",
        "CREATE TABLE evil (x INTEGER)",
        "ALTER TABLE alarms ADD COLUMN evil TEXT",
        "SELECT * FROM rejected",  # 沒有授權的表連讀都不行
        "SELECT * FROM message_log",
    ],
)
def test_readonly_role_cannot_write_or_read_ungranted_tables(stack_sim, statement):
    before = pg_rows(stack_sim["owner"], "SELECT (SELECT COUNT(*) FROM events), (SELECT COUNT(*) FROM telemetry), (SELECT COUNT(*) FROM alarms)")
    connection = psycopg.connect(stack_sim["readonly"], autocommit=True)
    try:
        with pytest.raises(psycopg.Error):
            connection.execute(statement)
        # 把「交易預設唯讀」關掉也沒用：權限本身就沒有
        connection.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0  # 讀取正常
    finally:
        connection.close()
    after = pg_rows(stack_sim["owner"], "SELECT (SELECT COUNT(*) FROM events), (SELECT COUNT(*) FROM telemetry), (SELECT COUNT(*) FROM alarms)")
    assert after == before


def test_readonly_role_attributes(stack_sim):
    role = stack_sim["env"]["READONLY_USER"]
    attrs = pg_rows(
        stack_sim["owner"],
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolcanlogin FROM pg_roles WHERE rolname = ?",
        (role,),
    )
    assert attrs == [(False, False, False, False, True)]
    writable = pg_rows(
        stack_sim["owner"],
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = ? AND privilege_type <> 'SELECT'",
        (role,),
    )
    assert writable == []


# ---- MCP server 用唯讀角色 ------------------------------------------------------------------


def _mcp_calls(server, calls):
    from mcp import Client  # noqa: PLC0415

    async def go():
        async with Client(server) as client:
            return [(await client.call_tool(name, arguments)).structured_content for name, arguments in calls]

    return asyncio.run(go())


CALLS = [
    ("get_equipment_state", {"equipment_id": EQUIPMENT_ID}),
    ("list_alarms", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T00:00:00Z", "until": "2026-01-03T00:00:00Z"}),
    ("get_recent_events", {"equipment_id": EQUIPMENT_ID, "limit": 100}),
    ("get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2026-01-01", "end_date": "2026-01-02"}),
    ("get_move_durations", {"equipment_id": EQUIPMENT_ID, "since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"}),
    ("get_equipment_state", {"equipment_id": "ROBOT99"}),
    ("get_utilization", {"equipment_id": EQUIPMENT_ID, "start_date": "2031-05-01", "end_date": "2031-05-02"}),
    ("list_alarms", {"equipment_id": "'; DROP TABLE events;--", "since": "2026-01-01T00:00:00Z", "until": "2026-01-03T00:00:00Z"}),
]


def test_mcp_server_over_stdio_with_readonly_role_matches_sqlite_results(stack_sim, sim_data):
    from mcp import StdioServerParameters  # noqa: PLC0415

    stdio = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py"), "--history", str(stack_sim["history"])],
        env={"GEM_ROBOT_DB": stack_sim["readonly"]},  # DSN 經環境變數傳入，不出現在命令列
        cwd=str(ROOT),
    )
    from_postgres = _mcp_calls(stdio, [*CALLS, ("verify_history", {})])
    from_sqlite = _mcp_calls(mcp_server.build_server(sim_data.db, sim_data.history), CALLS)

    assert from_postgres[-1]["valid"] is True
    for (name, _), pg_result, sqlite_result in zip(CALLS, from_postgres, from_sqlite):
        assert pg_result == sqlite_result, name
    assert from_postgres[1]["alarms"][1]["alarm_text"] == ADVERSARIAL_ALARM_TEXT[:200]
    assert from_postgres[2]["truncated"] is True
    assert from_postgres[7]["error"]["code"] == "INVALID_EQUIPMENT_ID"
    assert pg_rows(stack_sim["owner"], "SELECT COUNT(*) FROM events")[0][0] > 0


def test_mcp_server_refuses_a_write_capable_database_account(stack_sim):
    results = _mcp_calls(mcp_server.build_server(stack_sim["owner"], None), CALLS[:4])
    for result in results:
        assert result == {
            "ok": False,
            "error": {
                "code": "WRITE_CAPABLE_ACCOUNT",
                "message": "the server is configured with a database account that can write; refusing to use it",
            },
        }
    with pytest.raises(db_backend.WriteCapableAccountError):
        mcp_server.open_readonly(stack_sim["owner"])


# ---- Grafana provisioning -----------------------------------------------------------------


def _grafana(stack, path: str, body: dict | None = None):
    env = stack["env"]
    token = base64.b64encode(f"{env['GRAFANA_ADMIN_USER']}:{env['GRAFANA_ADMIN_PASSWORD']}".encode()).decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{env.get('GRAFANA_PORT', '3000')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - 固定連本機
        return json.loads(response.read())


def test_grafana_datasource_and_dashboard_are_provisioned_and_every_panel_query_runs(stack_sim):
    if not stack_env.port_open(int(stack_sim["env"].get("GRAFANA_PORT", "3000"))):
        pytest.skip("grafana is not running")
    datasource = _grafana(stack_sim, "/api/datasources/uid/gemrobot-timescaledb")
    assert datasource["user"] == stack_sim["env"]["READONLY_USER"]  # Grafana 用的是唯讀角色
    assert datasource["readOnly"] is True  # 由檔案 provision，UI 不能改
    assert _grafana(stack_sim, "/api/datasources/uid/gemrobot-timescaledb/health")["status"] == "OK"

    board = _grafana(stack_sim, "/api/dashboards/uid/gem-robot-overview")
    assert board["meta"]["provisioned"] is True
    panels = board["dashboard"]["panels"]
    assert [panel["title"] for panel in panels] == [
        "狀態時間軸",
        "每日稼動率",
        "警報次數（每日）",
        "電量趨勢",
        "MOVE 任務耗時（MOVE_STARTED → ARRIVED）",
    ]

    # 實際透過 Grafana 執行每個面板的查詢（時間範圍 = 模擬的那 24 小時）
    for panel in panels:
        target = panel["targets"][0]
        sql = target["rawSql"].replace("$equipment", EQUIPMENT_ID)
        result = _grafana(
            stack_sim,
            "/api/ds/query",
            {
                "from": "1767247200000",  # 2026-01-01T06:00:00Z
                "to": "1767333600000",  # 2026-01-02T06:00:00Z
                "queries": [
                    {
                        "refId": "A",
                        "datasource": {"uid": "gemrobot-timescaledb"},
                        "rawSql": sql,
                        "format": target["format"],
                        "maxDataPoints": 2000,
                        "intervalMs": 60000,
                    },
                ],
            },
        )
        frames = result["results"]["A"].get("frames", [])
        assert "error" not in result["results"]["A"], (panel["title"], result["results"]["A"])
        rows = sum(len(frame["data"]["values"][0]) for frame in frames if frame["data"]["values"])
        assert rows > 0, panel["title"]
