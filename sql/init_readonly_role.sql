-- 【未執行、未驗證的參考稿】PostgreSQL／TimescaleDB 用的唯讀角色。
--
-- 原規格要在 docker compose 的初始化 SQL 建一個只有 SELECT 的角色給 MCP server 用。
-- 這台開發機沒有 docker 也沒有 PostgreSQL，目前的儲存是 SQLite，而 SQLite 沒有帳號與角色：
-- 實際生效、有測試的唯讀機制是 mcp_server.open_readonly()（mode=ro + PRAGMA query_only）。
--
-- 這個檔案只是把「搬到 PostgreSQL 時應該怎麼做」寫下來，從來沒有對任何資料庫跑過。
-- 使用前請自行檢查；密碼不要寫在這裡，改由初始化時的環境變數／secret 帶入。
--
-- 用法（放在 /docker-entrypoint-initdb.d/，以擁有者身分執行）：
--   psql -v ro_password="$MCP_READONLY_PASSWORD" -f init_readonly_role.sql

CREATE ROLE mcp_readonly LOGIN PASSWORD :'ro_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;

-- 先收回 PUBLIC 預設的權限，再只給需要的。
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;

-- 只有 SELECT：資料表與 view。
GRANT SELECT ON
    state_changes, events, alarms, telemetry, gaps, ingest_state, state_map,
    v_state_intervals, v_daily_state_seconds, v_daily_availability,
    v_alarm_intervals, v_daily_alarm_stats, v_move_durations
TO mcp_readonly;
-- 刻意不給：rejected（原始的不合格訊息）、message_log。

-- 多一層保險：這個角色的交易預設唯讀，並限制單一查詢的時間。
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
ALTER ROLE mcp_readonly SET statement_timeout = '5s';
