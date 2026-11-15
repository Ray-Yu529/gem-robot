-- PostgreSQL / TimescaleDB 版資料表。欄位與 sql/schema.sql（SQLite）相同，ingest 用同一組 INSERT。
-- 時間一律 UTC：ts 是 ISO 8601 文字（...Z），ts_us 是同一時刻自 1970-01-01 起的整數微秒；
-- 時間差都用 ts_us 算。需要 timestamptz 時用 to_timestamp(ts_us / 1000000.0)。

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 每台設備每個 seq 只認一次（跨四種訊息），重複投遞靠這張表擋掉。
CREATE TABLE IF NOT EXISTS message_log (
    equipment_id TEXT   NOT NULL,
    seq          BIGINT NOT NULL,
    type         TEXT   NOT NULL CHECK (type IN ('state', 'event', 'alarm', 'telemetry')),
    PRIMARY KEY (equipment_id, seq)
);

CREATE TABLE IF NOT EXISTS state_changes (
    equipment_id TEXT    NOT NULL,
    seq          BIGINT  NOT NULL,
    site         TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    ts_us        BIGINT  NOT NULL,
    from_state   TEXT,
    to_state     TEXT    NOT NULL,
    heartbeat    INTEGER NOT NULL CHECK (heartbeat IN (0, 1)),
    PRIMARY KEY (equipment_id, seq)
);

CREATE TABLE IF NOT EXISTS events (
    equipment_id TEXT   NOT NULL,
    seq          BIGINT NOT NULL,
    site         TEXT   NOT NULL,
    ts           TEXT   NOT NULL,
    ts_us        BIGINT NOT NULL,
    event        TEXT   NOT NULL,
    station      TEXT,
    details      TEXT,
    PRIMARY KEY (equipment_id, seq)
);

CREATE TABLE IF NOT EXISTS alarms (
    equipment_id TEXT   NOT NULL,
    seq          BIGINT NOT NULL,
    site         TEXT   NOT NULL,
    ts           TEXT   NOT NULL,
    ts_us        BIGINT NOT NULL,
    alarm        TEXT   NOT NULL,
    action       TEXT   NOT NULL CHECK (action IN ('set', 'clear')),
    text         TEXT,
    PRIMARY KEY (equipment_id, seq)
);

-- telemetry 是 hypertable，以 ts_us（整數微秒）分區、每個 chunk 一天。
-- hypertable 的唯一索引必須包含分區欄位，所以主鍵是 (equipment_id, seq, ts_us)；
-- 「同一個 (equipment_id, seq) 只收一次」仍由 message_log 保證。
CREATE TABLE IF NOT EXISTS telemetry (
    equipment_id TEXT             NOT NULL,
    seq          BIGINT           NOT NULL,
    site         TEXT             NOT NULL,
    ts           TEXT             NOT NULL,
    ts_us        BIGINT           NOT NULL,
    battery      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (equipment_id, seq, ts_us)
);
SELECT create_hypertable('telemetry', 'ts_us', chunk_time_interval => 86400000000::bigint, if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS rejected (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payload_sha256 TEXT NOT NULL UNIQUE,
    topic          TEXT NOT NULL,
    payload        TEXT NOT NULL,
    reason         TEXT NOT NULL,
    received_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps (
    equipment_id TEXT   NOT NULL,
    seq_from     BIGINT NOT NULL,
    seq_to       BIGINT NOT NULL,
    missing      BIGINT NOT NULL,
    ts_from      TEXT   NOT NULL,
    ts_from_us   BIGINT NOT NULL,
    ts_to        TEXT   NOT NULL,
    ts_to_us     BIGINT NOT NULL,
    source       TEXT   NOT NULL CHECK (source IN ('seq_missing', 'publisher_reported')),
    PRIMARY KEY (equipment_id, seq_from, source)
);

CREATE TABLE IF NOT EXISTS ingest_state (
    equipment_id TEXT   PRIMARY KEY,
    last_seq     BIGINT NOT NULL,
    last_ts      TEXT   NOT NULL,
    last_ts_us   BIGINT NOT NULL
);

-- 狀態對應：E10 風格的簡化對應，作者自己的選擇，不是 SEMI E10 的標準實作。
CREATE TABLE IF NOT EXISTS state_map (
    raw_state TEXT PRIMARY KEY,
    e10_state TEXT NOT NULL
);
INSERT INTO state_map (raw_state, e10_state) VALUES
    ('MOVING',  'PRODUCTIVE'),
    ('IDLE',    'STANDBY'),
    ('ALARM',   'UNSCHEDULED_DOWN'),
    ('OFFLINE', 'OFFLINE'),
    ('UNKNOWN', 'UNKNOWN')
ON CONFLICT (raw_state) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_state_changes_ts ON state_changes (equipment_id, ts_us);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (equipment_id, ts_us);
CREATE INDEX IF NOT EXISTS idx_alarms_ts ON alarms (equipment_id, ts_us);
