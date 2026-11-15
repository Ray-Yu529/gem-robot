#!/bin/sh
# 建立只有 SELECT 的唯讀角色（MCP server 與 Grafana 用）。
# 由 postgres 映像檔在資料卷第一次初始化時執行；帳號與密碼來自環境變數，不寫在 repo 裡。
set -eu

psql -v ON_ERROR_STOP=1 \
     -v ro_user="$READONLY_USER" \
     -v ro_password="$READONLY_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE :"ro_user" LOGIN PASSWORD :'ro_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;

-- 先收回 PUBLIC 的預設權限，再只給需要的。
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO :"ro_user";
GRANT USAGE ON SCHEMA public TO :"ro_user";

-- 只有 SELECT：資料表與 view。
GRANT SELECT ON
    state_changes, events, alarms, telemetry, gaps, ingest_state, state_map,
    v_state_intervals, v_daily_state_seconds, v_daily_availability,
    v_alarm_intervals, v_daily_alarm_stats, v_move_durations
TO :"ro_user";
-- 刻意不給：rejected（原始的不合格訊息）、message_log。

-- 多一層保險：交易預設唯讀，並限制單一查詢的時間。
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;
ALTER ROLE :"ro_user" SET statement_timeout = '5s';
SQL
